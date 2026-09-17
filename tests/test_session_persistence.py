"""会话持久化契约测试（落地范围：ADR-0020 决策 ①~⑥ / 判据 2~8 / 文档判据 13~14）。

判据 8 分两面：**Agent 面**（新实例 + 同 DB → `SessionIdentityConflict`）在本文件；
**HTTP 面**（422 + 审计行）在 `tests/test_api_hardening.py`。

口径（与 0020 一致）：
- 全部走 tmp 目录里的 SQLite 文件 + fake 执行器，不碰 Doris，也**不碰仓库默认的
  `serving/state/`**——默认路径被测试写脏，「默认不落盘」这条承诺就失去证据；
- 「默认仍是 MemorySaver」是对既有构造点的兼容承诺（决策 ① 的理由 3；实测 `tests/`
  30 处 + `eval/` 9 处，计数口径见 0020 决策 ① 的实测注），所以两个方向都断：
  注入时**必须用传入实例**，不注入时**既是 MemorySaver 也不产生任何文件**；
- 序列化白名单按**内容**断言（构造参数 `allowed_msgpack_modules` 落在实例的私有属性
  `_allowed_msgpack_modules` 上，含 6 个自研 dataclass——`Plan` 的四个嵌套成员各自
  独立判定，见 0020 决策 ② 实测注），不按对象身份——「新建一个
  同内容的 serializer」与「漏传 serde」是可区分的两件事，
  后者才是决策 ② 要消灭的失效形态。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.compiler import (
    ComparisonSpec,
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
)
from agent.factory import (
    SnapshotUnavailable,
    checkpoint_saver_from_env,
    create_live_agent,
)
from agent.graph import DataAgent, SessionIdentityConflict, build_graph
from agent.planner import Planner
from agent.security.sql_guard import Budget
from agent.state import (
    ANALYSIS_RECORD_VERSION,
    TurnResult,
    decode_row_value,
    decode_rows,
    encode_row_value,
    encode_rows,
)
from serving.auth import claims_fingerprint

REPO = Path(__file__).resolve().parent.parent

# 锁定快照表白名单（与 tests/test_graph.py 同口径），再并上零售 4 表：判据 4 要跨域，
# 白名单必须同时容得下两个域（否则 retail 轮被 Guard 拒，测不到 thread 隔离本身）
_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
ALLOWED |= frozenset(
    {"atlas.dwd.store_sales", "atlas.dwd.date_dim", "atlas.dwd.dim_item", "atlas.dwd.dim_store"}
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
FINANCE_MODEL = SemanticModel()
RETAIL_MODEL = SemanticModel(REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml")

FINANCE_Q = "按分支统计 2013 年佣金收入，列出前 5 名"
# 取自 gold-151：可稳定解析出 `Plan.filters`，判据 7 的嵌套类型保真靠它
FINANCE_FILTERED_Q = "2013 年按分支统计佣金收入超过 1000 万的分支，列出前 5 名"
FINANCE_FOLLOWUP_Q = "那 2014 年呢"  # 同构追问：靠 last_plan 补全（ADR-0014 ②）
RETAIL_Q = "2000 年总销售额是多少？"

# ADR-0026 T06：分析父轮的问句（_begin_analysis 不解析问句，只作原问句记账）
ANALYSIS_Q = "多期间分析：2013 与 2014 年佣金收入变化归因"
ANALYSIS_Q2 = "多期间分析：2014 与 2015 年成交量变化归因"

# 决策 ② 的白名单内容：TurnState 里出现的自研 dataclass（(module, class) 对）。
# 六项而不是四项：`Filter` / `ComparisonSpec` 嵌在 `Plan` 内，**不会**随 `Plan`
# 一起放行——实测（langgraph 1.2.11）注册白名单非空时未注册类型是被 blocked 并
# 退化为 dict，连 permissive 默认模式也一样（详见 0020 决策 ② 的实测注）。
ALLOWLIST_PAIRS = frozenset(
    {
        ("agent.compiler", "TimeSpec"),
        ("agent.compiler", "OrderSpec"),
        ("agent.compiler", "Filter"),
        ("agent.compiler", "ComparisonSpec"),
        ("agent.compiler", "Plan"),
        ("agent.planner", "ClarificationRequest"),
    }
)


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集（同 tests/test_graph.py）。"""

    def __init__(self, rows: list[tuple[Any, ...]] = (("v",),), columns: list[str] | None = None):
        self.calls: list[str] = []
        self.rows = rows
        self.columns = list(columns or ["v"])

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], list(self.columns)


def sqlite_saver(path: Path) -> SqliteSaver:
    """按决策 ②③ 的构造方式建 SqliteSaver：自持连接 + serde 走构造参数 + setup 一次。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn=conn, serde=_serde_for_test())
    saver.setup()
    return saver


def _serde_for_test() -> Any:
    """测试侧 serializer：内容等于决策 ② 的白名单（不从 graph 私有常量抄身份）。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=tuple(sorted(ALLOWLIST_PAIRS)))


def _allowlist(saver: BaseCheckpointSaver | None) -> frozenset[tuple[str, str]]:
    """读 saver 的 serde 白名单**内容**（决策 ② 的可测形态）。

    为什么按内容而不是按行为：`JsonPlusSerializer()` 不传白名单时是 permissive——
    自研 dataclass 照样能往返，只是每次告警。于是「往返成功」证明不了白名单在位，
    能证明的只有这一项集合内容（行为面的补充证据是判据 7 的类型保持断言）。

    上游把这个属性建成三态（实测 langgraph 1.2.11 / checkpoint 4.2.0）：`True` =
    未传白名单且 permissive（一切放行 + 告警）、`None` = 严格模式默认（只放内置安全
    类型）、`set` = 显式白名单。**只有 set 态才是我们要的形态**，另两态一律显式报错
    ——`True` 会让「遍历」直接 TypeError，把「漏传 serde」这个契约失败伪装成实现崩溃。
    """
    if saver is None:  # 未注入 checkpointer（图不持久化）→ 视为空白名单
        return frozenset()
    serde = saver.serde
    _MISSING = object()
    raw = getattr(serde, "_allowed_msgpack_modules", _MISSING)
    if raw is _MISSING:  # pragma: no cover - 上游改名时才走到
        raise AssertionError(
            "JsonPlusSerializer 已无 `_allowed_msgpack_modules`：白名单测量口径失效，"
            "须重测 langgraph 版本对应形态（0020 推翻条件第 4 条），不得让本断言静默通过"
        )
    if raw is True:
        raise AssertionError(
            "serde 的白名单是字面量 True（permissive 全放行）= 构造时根本没传 "
            "allowed_msgpack_modules——决策 ② 的失效形态，不是可比较的集合"
        )
    if raw is None:
        return frozenset()  # 严格模式空白名单：按空集处理，让下面的缺项断言给出消息
    return frozenset(tuple(item) for item in raw)


def _why(result: TurnResult) -> str:
    """非 answer 时的最小诊断（kind + 三条原因字段），避免失败信息只剩 'clarify'。"""
    return (
        f"kind={result.kind} error={result.error} block_reason={result.block_reason} "
        f"clarification={result.clarification}"
    )


def _values(agent: DataAgent, model: SemanticModel, sid: str) -> dict[str, Any]:
    """读 (model, sid) 对应 thread 的状态（thread_id 前缀是决策 ④ 的约定）。"""
    snapshot = agent._graph.get_state({"configurable": {"thread_id": f"{model.name}:{sid}"}})
    return dict(snapshot.values or {})


def _config(model: SemanticModel, sid: str) -> dict[str, Any]:
    """(model, sid) 对应 thread 的 config（get_state / get_state_history 共用）。"""
    return {"configurable": {"thread_id": f"{model.name}:{sid}"}}


def _record_history(agent: DataAgent, model: SemanticModel, sid: str) -> list[dict[str, Any]]:
    """按**时间正序**返回各状态版本里的 analysis_record（仅含 dict 形态的版本）。

    用途：断言「崩溃遗留的 running 在下次同身份进入时先被标 interrupted」确实
    发生过——终态快照只能看到新 running，中间的 interrupted 版本只能从
    `get_state_history` 里找（无自动续跑、无 exactly-once 的审计口径）。
    """
    recs = [
        dict(snapshot.values["analysis_record"])
        for snapshot in agent._graph.get_state_history(_config(model, sid))
        if isinstance((snapshot.values or {}).get("analysis_record"), dict)
    ]
    return list(reversed(recs))


# 决策 ⑥ 的两种身份（verify_token 输出形态：sub/role/iat/exp/user_context）。
# iat/exp 参与指纹是**故意的**（旧的进程内实现同口径）：重签 token = 新身份 = 换会话。
HQ_CLAIMS: dict[str, object] = {
    "sub": "w9-user",
    "role": "hq_admin",
    "iat": 1,
    "exp": 4102444800,
    "user_context": {},
}
BRANCH_CLAIMS: dict[str, object] = {
    "sub": "w9-user",
    "role": "branch_manager",
    "iat": 1,
    "exp": 4102444800,
    "user_context": {"branch": "east"},
}


class TestCheckpointerInjection(unittest.TestCase):
    """判据 3（决策 ①）：默认 MemorySaver，注入则原样使用；决策 ②：serde 不得被换掉。"""

    def _assert_allowlist(self, saver: BaseCheckpointSaver | None, where: str) -> None:
        """断言白名单**包含**四个自研 dataclass（缺项即列出缺了什么）。"""
        missing = ALLOWLIST_PAIRS - _allowlist(saver)
        self.assertFalse(
            missing,
            f"{where} 的 serde 白名单缺项：{sorted(missing)}——漏传 serde 时上游只是"
            "permissive 告警，退化不会自己现形（决策 ②）",
        )

    def test_default_checkpointer_is_memory_saver(self) -> None:
        """无参 `build_graph()` → checkpointer 恰是 MemorySaver（不是 SQLite 实现）。"""
        graph = build_graph(executor=FakeExecutor(), budget=BUDGET)
        self.assertIs(type(graph.checkpointer), MemorySaver)

    def test_default_memory_saver_carries_the_allowlist(self) -> None:
        """默认分支的 MemorySaver 也必须带白名单（决策 ② 对两条分支同样成立）。

        实测（0020 决策 ② 实测注）把两种失效分得很清，本用例锁的是**内容**这一种：
        - **漏传 serde** → 白名单是字面量 `True`（permissive 全放行），类型照样保住，
          只每次告警——所以行为面（「往返成功」）对它完全无感，未来上游一 block 才炸；
        - **白名单非空但少一项** → 未注册类型被 blocked 后**静默退化为 dict**，
          今天就在的多轮缺陷正是这一种（`Filter` 少列 → 追问被伪装成反问）。
        两种都必须红，故这里断言集合内容而非往返结果；行为面的补充证据在
        `TestCheckpointStateFidelity`（判据 7）。
        """
        graph = build_graph(executor=FakeExecutor(), budget=BUDGET)
        self._assert_allowlist(graph.checkpointer, "默认 MemorySaver")

    def test_injected_checkpointer_is_used_verbatim(self) -> None:
        """显式传入 → `compiled.checkpointer is X`（不被「再包一层」换掉实例）。"""
        injected = MemorySaver()
        graph = build_graph(executor=FakeExecutor(), budget=BUDGET, checkpointer=injected)
        self.assertIs(graph.checkpointer, injected)

    def test_falsy_looking_saver_is_still_used(self) -> None:
        """真值判定陷阱：实现 `__len__` 返回 0 的 saver 不得被当成「没传」。

        `checkpointer or MemorySaver(...)` 是本改造最省字的写法，也是会**静默退回
        内存态**的写法：存储实例刚建好时长度为 0 是完全正常的形态，`or` 会把它判成
        缺省值，于是部署方显式开了持久化、拿到的却是重启即失的 MemorySaver，且没有任何
        报错。判据与实现都只许用 `is None`（同 ADR-0019 决策 ⑥ 的「null 也要回显，
        不得靠真值省略键」同一族失效）。
        """

        class EmptySaver(MemorySaver):
            def __len__(self) -> int:
                return 0

        saver = EmptySaver()
        self.assertFalse(bool(saver))  # 前提：它确实是 falsy（否则本用例形同虚设）
        graph = build_graph(executor=FakeExecutor(), budget=BUDGET, checkpointer=saver)
        self.assertIs(graph.checkpointer, saver)

    def test_injected_sqlite_saver_is_used_and_keeps_allowlist(self) -> None:
        """注入真实 SqliteSaver → 实例原样生效，且调用方给的 serde 不被图换掉。

        决策 ② 的失效形态是「换了 saver 但漏了 serde」——这里断言的是**图所用实例**
        的白名单内容，因此任何在 `build_graph` 内部重新构造 serializer 的写法都会红。
        """
        with tempfile.TemporaryDirectory(prefix="atlas-ckpt-") as tmp:
            saver = sqlite_saver(Path(tmp) / "checkpoints.sqlite")
            graph = build_graph(executor=FakeExecutor(), budget=BUDGET, checkpointer=saver)
            self.assertIs(graph.checkpointer, saver)
            # 反向守卫：注入侧自己若丢了白名单，上面的实例断言就成了空断言
            self._assert_allowlist(saver, "注入的 SqliteSaver")

    def test_default_construction_creates_no_state_file(self) -> None:
        """判据 2 的代码面：默认构造**不得**在仓库 `serving/state/` 下落任何文件。

        「既有构造点零落盘」是决策 ① 的立论前提；它必须由断言守着，而不是靠
        「测试没看见文件」。
        """
        state_dir = REPO / "serving" / "state"
        before = set(state_dir.glob("*")) if state_dir.is_dir() else set()
        build_graph(executor=FakeExecutor(), budget=BUDGET)
        DataAgent(executor=FakeExecutor(), budget=BUDGET)
        after = set(state_dir.glob("*")) if state_dir.is_dir() else set()
        self.assertEqual(
            after - before,
            set(),
            f"默认（未设 ATLAS_CHECKPOINT_DB）构造落盘了：{sorted(p.name for p in after - before)}",
        )

    def test_state_dir_is_git_ignored(self) -> None:
        """判据 2 的后半句「`git status` 无新增未跟踪文件」的可执行形态。

        为什么不断言字面的 `git status` 输出：那会把**别人**的未跟踪文件也算到本用例
        头上（测一次挂一次，且挂的原因与被测物无关）。`git check-ignore` 只问一件事：
        这个路径会不会被忽略——正落在判据的语义上。
        `check-ignore` 不要求路径存在（实测），故无需真的往仓库里写文件。
        带一条反向对照：已跟踪的 `serving/api.py` 必须**不**被忽略，否则「命令跑成功
        = 全绿」会让这条断言变成空断言。
        """
        probe = "serving/state/checkpoints.sqlite"

        def _ignored(path: str) -> bool | None:
            proc = subprocess.run(
                ["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True
            )
            if proc.returncode not in (0, 1):  # 非仓库 / 无 git：口径不成立
                return None
            return proc.returncode == 0

        state_ignored, api_ignored = _ignored(probe), _ignored("serving/api.py")
        if state_ignored is None or api_ignored is None:
            self.skipTest("git check-ignore 在本环境不可用（非 git 检出）——口径不成立")
        self.assertTrue(state_ignored, f"{probe} 未进 .gitignore——落盘的会话轨迹可被误提交")
        self.assertFalse(api_ignored, "git check-ignore 口径异常（跟踪文件也被判为忽略）")

    def test_data_agent_forwards_checkpointer(self) -> None:
        """DataAgent 必须把注入透传到图（工厂按 env 建 saver，Agent 是唯一的通路）。"""
        saver = MemorySaver()
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET, checkpointer=saver)
        self.assertIs(agent._graph.checkpointer, saver)

    def test_data_agent_default_is_memory_saver(self) -> None:
        """不传 checkpointer 的 DataAgent（评测与全部既有测试路径）零变化。"""
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)
        self.assertIs(type(agent._graph.checkpointer), MemorySaver)


class TestCheckpointStateFidelity(unittest.TestCase):
    """判据 7：状态经 checkpoint 往返后，嵌套类型必须仍是自研 dataclass。

    这一条是决策 ② 的**行为面**证据，与 `_allowlist()` 的内容面互补：内容断言能抓
    「漏传 serde」，但抓不到「serde 传了、白名单少一项」——后者恰好是实测发现的形态
    （`Filter` / `ComparisonSpec` 嵌在 `Plan` 里，注册 `Plan` 并不放行其成员）。
    """

    _PLAN = Plan(
        metric="commission_revenue",
        dimensions=("Branch",),
        time=TimeSpec("year", 2013),
        filters=(Filter("commission_revenue", ">", 10_000_000),),
        order_by=(OrderSpec("commission_revenue", True),),
        limit=5,
        comparison=ComparisonSpec("yoy"),
    )

    def test_nested_plan_types_survive_the_default_serde(self) -> None:
        """用**图真正拿到的那个** serde 往返（不抄私有常量，也不新建同内容实例）。"""
        graph = build_graph(executor=FakeExecutor(), budget=BUDGET)
        serde = graph.checkpointer.serde
        back = serde.loads_typed(serde.dumps_typed({"plan": self._PLAN}))["plan"]
        self.assertIsInstance(back, Plan)
        for label, got, want in (
            ("time", back.time, TimeSpec),
            ("filters[0]", back.filters[0], Filter),
            ("order_by[0]", back.order_by[0], OrderSpec),
            ("comparison", back.comparison, ComparisonSpec),
        ):
            self.assertIs(
                type(got),
                want,
                f"plan.{label} 往返后退化成 {type(got).__name__}：该类型不在 checkpoint "
                "白名单里，多轮读回的是 dict 而不是 dataclass（决策 ②/代价 ⑤）",
            )

    def test_multiturn_followup_keeps_the_filter_predicate(self) -> None:
        """同一形态走真图：第 2 轮靠跨轮 `last_plan` 补全，谓词必须还在。

        只用 serde 断言会漏掉「图到底有没有把 plan 过一遍序列化」这个问题——本用例
        不构造 thread_id（那是工作项 8 的 `f"{model}:{sid}"` 约定），只经 `ask` 公开
        路径，因此对 thread 命名空间的改动不敏感。
        """
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        first = agent.ask(FINANCE_FILTERED_Q, session_id="w7-fidelity")
        self.assertEqual(first.kind, "answer", _why(first))
        second = agent.ask(FINANCE_FOLLOWUP_Q, session_id="w7-fidelity")
        self.assertEqual(second.kind, "answer", _why(second))
        self.assertIn(
            "10000000",
            second.sql or "",
            f"追问轮的 SQL 丢了阈值谓词（last_plan.filters 跨轮读回后退化）：{second.sql}",
        )


class TestThreadIdNamespace(unittest.TestCase):
    """判据 4（决策 ④）：共用同一 SQLite 文件时，同 `sid` 必须落在按域隔离的 thread。

    为什么必须**共用一个 saver**：今天的跨域隔离是「两个 Agent 各持一个 MemorySaver」
    侥幸得来的（0020 背景节）。持久化后两个域写同一个文件，`thread_id` 只含 `sid`
    就会互相覆写——所以本类的两个 Agent 共享同一实例，复刻生产形态（CLI 与 HTTP 同
    文件，代价 ④）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-thread-")
        self.saver = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(self.saver.conn.close)
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _thread(model: SemanticModel, sid: str) -> str:
        return f"{model.name}:{sid}"

    def test_same_sid_lands_on_per_model_threads(self) -> None:
        sid = "w8-shared-sid"
        fin = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=self.saver
        )
        ret = DataAgent(
            model=RETAIL_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=self.saver
        )
        r_fin = fin.ask(FINANCE_Q, session_id=sid)
        r_ret = ret.ask(RETAIL_Q, session_id=sid)
        self.assertEqual(r_fin.kind, "answer", r_fin.kind)
        self.assertEqual(r_ret.kind, "answer", r_ret.kind)
        # 前提守卫：两个域问的是不同问句，否则下面的「读到本域那轮」是空断言
        self.assertNotEqual(FINANCE_Q, RETAIL_Q)
        # 对外契约不变：TurnResult.session_id 仍是原始 sid（命名空间只活在 checkpoint 里）
        self.assertEqual((r_fin.session_id, r_ret.session_id), (sid, sid))
        for agent, model, want_q, want_metric in (
            (fin, FINANCE_MODEL, FINANCE_Q, r_fin.metric),
            (ret, RETAIL_MODEL, RETAIL_Q, r_ret.metric),
        ):
            values = _values(agent, model, sid)
            self.assertEqual(
                values.get("question"),
                want_q,
                f"{model.name} 的 thread 里读不到本域那轮（拿到 {values.get('question')!r}）"
                "——thread_id 少了模型前缀就会互相覆写（决策 ④）",
            )
            self.assertEqual(values["last_plan"].metric, want_metric)
        # 判据 4 原文写的是「两个 thread 的 last_plan 互不可见」——这条**按字面在共享
        # saver 上不可执行**：两个 thread 存在同一份文件里，任何一方按对方 thread_id
        # 都查得到（我最初把它写成「在 retail 图上查 finance 的 thread 应为空」，实测
        # 实现正确时它也红）。判据已按下列两件可证伪的事纠正，见 0020 判据 4 落地注：
        # (a) 存储层确实落了两条带模型前缀的 thread（去掉前缀 → 这里只剩一条）。
        # 为什么查表而不走 `saver.list()`：实测 `SqliteSaver.list` 的 config 是必填
        # 位置参数（`list()` 直接 TypeError），而本条要问的恰恰是「文件里有几条 thread」。
        thread_rows = self.saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
        threads = sorted(row[0] for row in thread_rows)
        self.assertEqual(
            threads,
            sorted([self._thread(FINANCE_MODEL, sid), self._thread(RETAIL_MODEL, sid)]),
            f"checkpoint 文件里的 thread 集不对：{threads}",
        )
        # (b) 行为层的「互不可见」：finance 的追问只能补全 finance 那轮。后写者赢，
        # 所以必须测**先问**的那一侧——裸 sid 时它的 thread 已被 retail 覆写，
        # 追问会补出 retail 的指标。
        third = fin.ask(FINANCE_FOLLOWUP_Q, session_id=sid)
        self.assertEqual(third.kind, "answer", _why(third))
        self.assertEqual(
            third.metric,
            r_fin.metric,
            f"finance 追问补到了别的域：{third.metric} != {r_fin.metric}"
            "（thread 被 retail 覆写 = 决策 ④ 要消灭的跨域串话）",
        )

    def test_new_agent_on_same_db_continues_the_session(self) -> None:
        """判据 6 的代码面：换实例 + 新连接 + 同一文件 + 同 sid → 追问补全仍生效。"""
        sid = "w8-restart"
        first = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=self.saver
        )
        r1 = first.ask(FINANCE_Q, session_id=sid)
        self.assertEqual(r1.kind, "answer", r1.kind)
        # 新 saver = 新 sqlite3.Connection，等价于「进程重启后重新打开同一文件」
        revived = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(revived.conn.close)
        second = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=revived
        )
        r2 = second.ask(FINANCE_FOLLOWUP_Q, session_id=sid)
        self.assertEqual(
            r2.kind, "answer", f"新实例没续上会话（last_plan 未跨文件存活）：{r2.kind}"
        )
        self.assertEqual(r2.metric, r1.metric)


class TestCheckpointDbEnv(unittest.TestCase):
    """决策 ① 末段 + 决策 ③：`ATLAS_CHECKPOINT_DB` 是持久化的唯一开关，落点在工厂。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-env-")
        # 刻意多嵌一层：决策 ③ 要求目录不存在时 mkdir(parents=True, exist_ok=True)
        self.db = Path(self._tmp.name) / "nested" / "checkpoints.sqlite"
        self._saved = os.environ.get("ATLAS_CHECKPOINT_DB")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("ATLAS_CHECKPOINT_DB", None)
        else:
            os.environ["ATLAS_CHECKPOINT_DB"] = self._saved
        self._tmp.cleanup()

    def test_unset_or_blank_env_means_no_saver(self) -> None:
        """未设 / 空串 / 纯空白都算「不持久化」（空 = 显式关，不是笔误回退）。"""
        os.environ.pop("ATLAS_CHECKPOINT_DB", None)
        self.assertIsNone(checkpoint_saver_from_env())
        for value in ("", "   "):
            with self.subTest(value=value):
                os.environ["ATLAS_CHECKPOINT_DB"] = value
                self.assertIsNone(checkpoint_saver_from_env())
        self.assertFalse(self.db.parent.exists(), "不持久化时不得创建任何目录")

    def test_env_path_creates_schema_and_keeps_allowlist(self) -> None:
        """设了路径 → SqliteSaver + 建表 + 白名单内容齐全（决策 ② 对注入分支同样成立）。"""
        os.environ["ATLAS_CHECKPOINT_DB"] = str(self.db)
        saver = checkpoint_saver_from_env()
        self.assertIsInstance(saver, SqliteSaver)
        assert saver is not None  # mypy：上一行已排除 None
        self.addCleanup(saver.conn.close)
        self.assertTrue(self.db.is_file(), f"父目录不存在时应 mkdir：{self.db}")
        table_rows = saver.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in table_rows}
        self.assertTrue(
            {"checkpoints", "writes"} <= tables,
            f"setup() 未调用则表不存在（from_conn_string 的 with 语义会关连接）：{sorted(tables)}",
        )
        missing = ALLOWLIST_PAIRS - _allowlist(saver)
        self.assertFalse(
            missing, f"工厂构造的 saver 白名单缺项：{sorted(missing)}（决策 ② 的 serde 漏传形态）"
        )

    def test_live_agent_wires_env_into_graph(self) -> None:
        """`create_live_agent()` 必须真的把 env 变成图的 checkpointer——工厂是唯一通路。"""
        os.environ["ATLAS_CHECKPOINT_DB"] = str(self.db)
        try:
            agent = create_live_agent()
        except SnapshotUnavailable as exc:  # pragma: no cover - 宿主无 meta 时才走到
            self.skipTest(f"宿主无可用锁定快照（与本判据无关）：{exc}")
        self.addCleanup(agent.checkpointer.conn.close)
        self.assertIs(type(agent._graph.checkpointer), SqliteSaver)

    def test_live_agent_blank_env_keeps_memory_saver(self) -> None:
        """留空即零变化：生产工厂构造出的 agent 仍是 MemorySaver，且不落任何文件。"""
        os.environ["ATLAS_CHECKPOINT_DB"] = ""
        try:
            agent = create_live_agent()
        except SnapshotUnavailable as exc:  # pragma: no cover
            self.skipTest(f"宿主无可用锁定快照（与本判据无关）：{exc}")
        self.assertIs(type(agent._graph.checkpointer), MemorySaver)
        self.assertFalse(self.db.parent.exists())


class TestTurnsSingleSourceOfTruth(unittest.TestCase):
    """判据 5（决策 ⑤）：轮数的唯一事实源是 checkpoint 状态里的 `turns` 字段。

    旧实现记在 `DataAgent._session_turns`（进程内 dict，`sessions` property 是其只读
    视图）：实例一换就归零，而会话本体（`last_plan`）已落盘——「第几轮」与「记得什么」
    分家。本类只认 checkpoint：状态里有 `turns`、新实例接着数、`sessions` 不存在。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-turns-")
        self.db = Path(self._tmp.name) / "checkpoints.sqlite"
        self.saver = sqlite_saver(self.db)
        self.addCleanup(self.saver.conn.close)
        self.addCleanup(self._tmp.cleanup)

    def _agent(self, saver: SqliteSaver | None = None) -> DataAgent:
        return DataAgent(
            model=FINANCE_MODEL,
            executor=FakeExecutor(),
            budget=BUDGET,
            checkpointer=saver or self.saver,
        )

    def test_three_turns_count_from_one_to_three(self) -> None:
        """同 `sid` 连续 3 轮 → 1/2/3（第 2 轮是残句追问，走的正是跨轮状态）。"""
        agent = self._agent()
        sid = "w9-turns-1"
        results = [
            agent.ask(FINANCE_Q, session_id=sid),
            agent.ask(FINANCE_FOLLOWUP_Q, session_id=sid),
            agent.ask(FINANCE_Q, session_id=sid),
        ]
        for result in results:
            self.assertEqual(result.kind, "answer", _why(result))
        self.assertEqual([r.turns_in_session for r in results], [1, 2, 3])

    def test_turns_visible_in_checkpoint_and_survives_new_agent(self) -> None:
        """3 轮后换实例 + 新连接、同文件 → 第 4 轮（轮数来自 checkpoint 而非进程内 dict）。"""
        sid = "w9-turns-2"
        agent = self._agent()
        for _ in range(3):
            agent.ask(FINANCE_Q, session_id=sid)
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertEqual(
            values.get("turns"), 3, "状态里没有 turns：轮数还在进程内记账（决策 ⑤ 未落地）"
        )
        revived = sqlite_saver(self.db)  # 新连接 = 「重启后重开同一文件」
        self.addCleanup(revived.conn.close)
        r4 = self._agent(revived).ask(FINANCE_FOLLOWUP_Q, session_id=sid)
        self.assertEqual(r4.kind, "answer", _why(r4))
        self.assertEqual(r4.turns_in_session, 4)

    def test_agent_has_no_in_process_session_table(self) -> None:
        """`sessions` property 随决策 ⑤ 一并删除（破坏性变更，0020 代价 ⑦）。

        判据 5 原文写的是 `assertNotHasattr`——unittest 没有这个方法（实测
        AttributeError），等价写法是 `assertFalse(hasattr(…))`。
        """
        self.assertFalse(hasattr(DataAgent, "sessions"), "sessions property 仍在（决策 ⑤）")


class TestIdentityFingerprintInCheckpoint(unittest.TestCase):
    """判据 8 的 Agent 面（决策 ⑥）：指纹存进 checkpoint，跨实例（≈重启）仍能校验。

    为什么必须持久化：指纹原存在 `serving/api.py` 的 `holder["sessions"]`（进程内
    字典）。会话一旦落盘续接，重启后指纹表却是空的——「会话续上了、身份校验却重置了」
    正是决策 ⑥ 要消灭的不一致。本类用「新连接 + 新 Agent + 同一文件」复刻重启。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-identity-")
        self.db = Path(self._tmp.name) / "checkpoints.sqlite"
        self.saver = sqlite_saver(self.db)
        self.addCleanup(self.saver.conn.close)
        self.addCleanup(self._tmp.cleanup)

    def _agent(self, executor: FakeExecutor, saver: SqliteSaver | None = None) -> DataAgent:
        return DataAgent(
            model=FINANCE_MODEL, executor=executor, budget=BUDGET, checkpointer=saver or self.saver
        )

    def test_conflict_raised_before_execution(self) -> None:
        """首轮绑定后换身份：抛冲突，且该轮不执行 SQL、不写状态（校验必须早于 invoke）。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        sid = "w9-id-1"
        r1 = agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(r1.kind, "answer", _why(r1))
        self.assertEqual(len(executor.calls), 1)
        with self.assertRaises(SessionIdentityConflict):
            agent.ask(FINANCE_Q, session_id=sid, identity=BRANCH_CLAIMS)
        self.assertEqual(len(executor.calls), 1, "冲突轮必须停在 invoke 之前（不得执行 SQL）")
        self.assertEqual(
            _values(agent, FINANCE_MODEL, sid).get("turns"),
            1,
            "冲突轮推进了轮数：校验被放进了图里（裁决 = ask 在 invoke 前抛，决策 ⑥）",
        )

    def test_same_claims_continue_without_false_conflict(self) -> None:
        """同身份第二轮（新 dict 对象、内容相同）→ 续接，无假冲突（按内容不按对象身份）。"""
        agent = self._agent(FakeExecutor())
        sid = "w9-id-2"
        r1 = agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        r2 = agent.ask(FINANCE_FOLLOWUP_Q, session_id=sid, identity=dict(HQ_CLAIMS))
        self.assertEqual((r1.kind, r2.kind), ("answer", "answer"), _why(r2))
        self.assertEqual(r2.turns_in_session, 2)

    def test_stored_fingerprint_is_a_digest_and_stable(self) -> None:
        """存的是哈希不是 claims 本体（0011 不外泄身份细节），且跨轮稳定。"""
        agent = self._agent(FakeExecutor())
        sid = "w9-id-3"
        agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        first = _values(agent, FINANCE_MODEL, sid).get("session_fingerprint")
        self.assertRegex(str(first), r"^[0-9a-f]{64}$", "指纹不是 sha256 摘要（决策 ⑥）")
        self.assertNotIn("hq_admin", str(first), "角色名进了 checkpoint：身份细节未哈希（0011）")
        agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(
            _values(agent, FINANCE_MODEL, sid).get("session_fingerprint"),
            first,
            "第二轮改写了指纹：跨轮不稳定会在重启后变成假冲突",
        )

    def test_conflict_survives_new_agent_on_same_db(self) -> None:
        """判据 8 的跨重启面：新连接 + 新 Agent + 同文件 → 换身份仍冲突、同身份仍续接。"""
        sid = "w9-id-4"
        self._agent(FakeExecutor()).ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        revived = sqlite_saver(self.db)  # 新连接 = 「重启后重开同一文件」
        self.addCleanup(revived.conn.close)
        stranger = self._agent(FakeExecutor(), revived)
        with self.assertRaises(
            SessionIdentityConflict,
            msg="重启后指纹丢失 = 身份校验被重置（决策 ⑥ 的失效形态）",
        ):
            stranger.ask(FINANCE_Q, session_id=sid, identity=BRANCH_CLAIMS)
        r2 = stranger.ask(FINANCE_FOLLOWUP_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(r2.kind, "answer", _why(r2))
        self.assertEqual(r2.turns_in_session, 2)

    def test_identityless_round_writes_no_binding(self) -> None:
        """无身份轮（CLI 语义）不落指纹；之后带身份的首轮正常采用该会话（不炸）。"""
        agent = self._agent(FakeExecutor())
        sid = "w9-id-5"
        r1 = agent.ask(FINANCE_Q, session_id=sid)
        self.assertEqual(r1.kind, "answer", _why(r1))
        self.assertNotIn(
            "session_fingerprint",
            _values(agent, FINANCE_MODEL, sid),
            "无身份轮落了指纹：CLI 会话会被伪造绑定，之后换身份反而冲突",
        )
        r2 = agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(r2.kind, "answer", _why(r2))
        bound = _values(agent, FINANCE_MODEL, sid).get("session_fingerprint")
        self.assertRegex(str(bound), r"^[0-9a-f]{64}$")


class TestWorkers1RationaleNarrowed(unittest.TestCase):
    """文档判据 13/14 的机器可判面：workers=1 的理由副本已收窄（ADR-0020 决策 ⑧）。

    「漏一个即新的 N2」，故把副本清单固化进断言：判据 13 的 7 处里 5 处是改写点
    （Makefile / compose / Dockerfile / api.py / README），另 2 处（graph.py /
    test_api.py）已随工作项 9 同步、这里只锁不回退；按行为检索又找到 11 处同类
    失真（README 行内引用与 KL #25、英文 README 两段、ADR-0011 落地注记、
    ADR-0018 决策 ⑦、前端计划两处、audit.py docstring、cli.py 与 demo_e2e.py 的
    会话键注释），一并纳入。

    两层口径：
    - 旧形态符号 `_session_turns`（工作项 9 删除的进程内记账表）不得回流——
      任何现行文件再把它当机制引用即复辟进程内记账；
    - 承载理由的文件必须出现收窄后的三项（限流桶 / 审计写 / SQLite 单写者）；
      英文副本按对应词检查（中英口径分裂同属失真）。
    """

    RATIONALE_FILES: tuple[str, ...] = (
        "Makefile",
        "docker-compose.yml",
        "infra/docker/api/Dockerfile",
        "serving/api.py",
        "serving/audit.py",
        "README.md",
        "infra/adr/0011-security-layering.md",
        "infra/adr/0018-frontend-console.md",
        "docs/design/frontend-console-plan.md",
    )
    RATIONALE_FILES_EN: tuple[str, ...] = ("README.en.md",)
    MECHANISM_FILES: tuple[str, ...] = (
        "agent/graph.py",
        "agent/cli.py",
        "tests/test_api.py",
        "tests/test_demo_e2e.py",
    )

    def _read(self, rel: str) -> str:
        return (REPO / rel).read_text(encoding="utf-8")

    def test_stale_accounting_symbol_absent(self) -> None:
        files = (*self.RATIONALE_FILES, *self.RATIONALE_FILES_EN, *self.MECHANISM_FILES)
        for rel in files:
            with self.subTest(rel):
                self.assertNotIn(
                    "_session_turns",
                    self._read(rel),
                    f"{rel} 引用了已删除的 `_session_turns`（工作项 9 删表，决策 ⑤）",
                )

    def test_narrowed_reason_present(self) -> None:
        for rel in self.RATIONALE_FILES:
            text = self._read(rel)
            with self.subTest(rel):
                for token in ("限流", "审计", "单写者"):
                    self.assertIn(token, text, f"{rel} 缺少收窄后理由关键词：{token}（决策 ⑧）")

    def test_narrowed_reason_present_en(self) -> None:
        for rel in self.RATIONALE_FILES_EN:
            text = self._read(rel).lower()
            with self.subTest(rel):
                for token in ("rate-limit", "audit", "single-writer"):
                    self.assertIn(token, text, f"{rel} lacks narrowed keyword: {token}")


# ============================================================================
# ADR-0026 T06：父会话状态、身份与中断恢复记账（决策 ④）
# ============================================================================


class TestAnalysisRowValueEncoding(unittest.TestCase):
    """检查单 5：原始 rows 标量带类型编码往返——类型与值均保持，可供复算。

    为什么必须带类型：checkpoint 只保证 JSON/msgpack 原生形态的保真，Decimal /
    bool 直接塞进 msgpack 会分别丢精度（退化为 float/str）与丢类型（bool 是 int
    子类，True 退化成 1 后「是/否」维度值全错）。编码产物必须是 JSON 原生形态，
    T07/T09 才能在 checkpoint 与评测器两侧复用同一套工具。
    """

    def test_decimal_roundtrips_exactly(self) -> None:
        """Decimal 经字符串精确保真（数据库金额不该在记账层丢精度）。"""
        for raw in (Decimal("0.1"), Decimal("1234567890.123456789"), Decimal("-7"), Decimal(0)):
            with self.subTest(raw=str(raw)):
                decoded = decode_row_value(encode_row_value(raw))
                self.assertIsInstance(decoded, Decimal)
                self.assertEqual(decoded, raw)

    def test_string_int_none_roundtrip(self) -> None:
        """str / int / None 三类互可区分且各自保真（检查单 5 的四类标量）。"""
        for raw in ("佣金收入", "", 42, -1, 0, None):
            with self.subTest(raw=repr(raw)):
                decoded = decode_row_value(encode_row_value(raw))
                self.assertIs(type(decoded), type(raw))
                self.assertEqual(decoded, raw)

    def test_bool_stays_bool_and_differs_from_int(self) -> None:
        """bool 必须与 int 可区分（bool 是 int 子类——判定顺序不能反）。

        `encode(True) != encode(1)` 是形态面的硬断言：tagged dict 若把 bool 并进
        int 分支，本条立刻红；行为面再补「往返后仍是 bool 且不是 1」。
        """
        self.assertNotEqual(encode_row_value(True), encode_row_value(1))
        for raw in (True, False):
            with self.subTest(raw=raw):
                decoded = decode_row_value(encode_row_value(raw))
                self.assertIsInstance(decoded, bool, f"{raw!r} 往返后丢了 bool 类型")
                self.assertIs(decoded, raw)
        one = decode_row_value(encode_row_value(1))
        self.assertIsInstance(one, int)
        self.assertNotIsInstance(one, bool)
        self.assertEqual(one, 1)

    def test_float_roundtrips_exactly(self) -> None:
        """float 走 repr 精确保真（超出检查单最低要求的超集：执行器可能返回浮点）。"""
        for raw in (3.5, -0.25):
            with self.subTest(raw=raw):
                decoded = decode_row_value(encode_row_value(raw))
                self.assertIsInstance(decoded, float)
                self.assertEqual(decoded, raw)

    def test_unsupported_type_raises(self) -> None:
        """容器/复数等不支持的类型显式 TypeError——静默丢类型会让复算出错值。"""
        for raw in ({"a": 1}, [1], 1.5j, object()):
            with (
                self.subTest(raw=type(raw).__name__),
                self.assertRaises(TypeError),
            ):
                encode_row_value(raw)

    def test_decode_rejects_malformed_payload(self) -> None:
        """未知标记 / 非 tagged 形态 ValueError——数据损坏不得被静默读成别的值。"""
        with self.assertRaises(ValueError):
            decode_row_value({"t": "nope", "v": 1})
        with self.assertRaises(ValueError):
            decode_row_value("not-a-tagged-dict")
        with self.assertRaises(ValueError):
            decode_row_value({"v": 1})

    def test_encoded_form_is_json_native(self) -> None:
        """编码产物是 JSON 原生形态（进 checkpoint / 跨进程复算的前提）。"""
        encoded = encode_row_value(Decimal("12.5"))
        self.assertEqual(json.loads(json.dumps(encoded)), encoded)

    def test_rows_roundtrip_keeps_shape_and_types(self) -> None:
        """行集合级往返：encode → JSON 原生嵌套 list；decode → tuple[tuple] 形态。"""
        rows = (("east", Decimal("10.5"), True, None, 3), ("west", Decimal("9.5"), False, "x", 4))
        encoded = encode_rows(rows)
        self.assertIsInstance(encoded, list)
        self.assertIsInstance(encoded[0], list)
        decoded = decode_rows(encoded)
        self.assertEqual(decoded, rows)
        self.assertIsInstance(decoded[0], tuple)
        self.assertIsInstance(decoded[0][1], Decimal)
        self.assertIs(decoded[0][2], True)
        self.assertIsNone(decoded[0][3])
        self.assertEqual(decode_rows(encode_rows(())), ())


class TestAnalysisParentTurnLifecycle(unittest.TestCase):
    """检查单 1（MemorySaver / SQLite 双 saver）：开始+子步记录+结束只推进一个用户轮。

    next 始终为空、无父 SQL；`_write_analysis_state` 拒绝 turns 键（子步与终态
    写入都不得推进用户轮数——决策 ④ ③「子步只更新本轮证据不增 turns」）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-t06-lc-")
        self.addCleanup(self._tmp.cleanup)
        self.sqlite = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(self.sqlite.conn.close)

    def _cases(self):
        """双 saver 参数化：memory（DataAgent 默认 MemorySaver）与注入的 SqliteSaver。"""
        for label, saver in (("memory", None), ("sqlite", self.sqlite)):
            executor = FakeExecutor()
            agent = DataAgent(
                model=FINANCE_MODEL, executor=executor, budget=BUDGET, checkpointer=saver
            )
            yield label, agent, executor

    def test_begin_step_end_advances_one_user_turn_without_parent_sql(self) -> None:
        """完整生命周期：turns 恰好 0→1，next 恒为空，父 SQL 为零。"""
        for label, agent, executor in self._cases():
            with self.subTest(saver=label):
                sid = f"t06-lc-{label}"
                record = agent._begin_analysis(sid, ANALYSIS_Q, None)
                config = _config(FINANCE_MODEL, sid)
                snapshot = agent._graph.get_state(config)
                self.assertEqual(snapshot.next, (), "分析开始写入后图不得待继续")
                self.assertEqual(
                    record,
                    {
                        "schema_version": ANALYSIS_RECORD_VERSION,
                        "status": "running",
                        "question": ANALYSIS_Q,
                        "identity_fingerprint": None,
                        "steps": [],
                    },
                    "running 记录必须只有有界事实（版本/状态/问句/身份指纹/steps）",
                )
                values = _values(agent, FINANCE_MODEL, sid)
                self.assertEqual(values.get("turns"), 1)
                self.assertEqual(values.get("question"), ANALYSIS_Q)
                self.assertTrue(values.get("analysis_followup_blocked"))
                # 子步写入：不推进轮数，next 仍为空
                agent._write_analysis_state(
                    sid, {"analysis_record": {"steps": [{"index": 1, "status": "ok"}]}}
                )
                self.assertEqual(agent._graph.get_state(config).next, ())
                values = _values(agent, FINANCE_MODEL, sid)
                self.assertEqual(
                    values.get("turns"), 1, "子步写入推进了轮数（决策 ④ ③：子步不增 turns）"
                )
                self.assertEqual(values["analysis_record"]["steps"], [{"index": 1, "status": "ok"}])
                # 结束：明确终态，仍不推进轮数
                agent._write_analysis_state(sid, {"analysis_record": {"status": "completed"}})
                self.assertEqual(agent._graph.get_state(config).next, ())
                values = _values(agent, FINANCE_MODEL, sid)
                self.assertEqual(values["analysis_record"]["status"], "completed")
                self.assertEqual(values.get("turns"), 1, "整个分析生命周期推进了不止一个用户轮")
                self.assertEqual(executor.calls, [], "分析记账不得产生父 SQL")

    def test_write_analysis_state_rejects_turns_key(self) -> None:
        """`_write_analysis_state` 携带 turns 键 → ValueError，且轮数未被改写。"""
        for label, agent, _ in self._cases():
            with self.subTest(saver=label):
                sid = f"t06-guard-{label}"
                agent._begin_analysis(sid, ANALYSIS_Q, None)
                with self.assertRaises(ValueError):
                    agent._write_analysis_state(sid, {"turns": 99})
                self.assertEqual(
                    _values(agent, FINANCE_MODEL, sid).get("turns"),
                    1,
                    "被拒绝的写入仍改了 turns：先校验后写入的顺序被破坏",
                )


class TestAnalysisRecordBounds(unittest.TestCase):
    """检查单 3 + 5 的记账边界：开始清理单轮残留、记录有界事实不写贡献率/叙事/claims；
    last_plan 保留但挂 blocked；旧成功状态不渗透进后续普通轮；白名单不含分析 dataclass。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-t06-bounds-")
        self.addCleanup(self._tmp.cleanup)
        self.sqlite = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(self.sqlite.conn.close)

    def test_begin_flushes_single_turn_residuals_but_keeps_last_plan(self) -> None:
        """分析开始：plan/sql/rows 等单轮残留清零；last_plan 原样保留但追问被挂起。"""
        executor = FakeExecutor()
        agent = DataAgent(model=FINANCE_MODEL, executor=executor, budget=BUDGET)
        sid = "t06-bounds-flush"
        first = agent.ask(FINANCE_FILTERED_Q, session_id=sid)
        self.assertEqual(first.kind, "answer", _why(first))
        before = _values(agent, FINANCE_MODEL, sid)
        self.assertIsInstance(before.get("last_plan"), Plan)
        calls_after_ask = len(executor.calls)
        agent._begin_analysis(sid, ANALYSIS_Q, None)
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertEqual(values.get("turns"), 2)
        self.assertEqual(
            values.get("last_plan"),
            before["last_plan"],
            "分析开始污染了 last_plan（决策 ④ ⑥：子步骤不改父 last_plan）",
        )
        self.assertTrue(values.get("analysis_followup_blocked"))
        for key in ("plan", "sql", "rows", "columns", "clarification", "candidates", "explanation"):
            self.assertIsNone(values.get(key), f"分析开始未清理单轮残留 {key}（决策 ④ ③）")
        self.assertEqual(values.get("row_count"), 0)
        self.assertEqual(len(executor.calls), calls_after_ask, "分析开始不得执行 SQL")
        record = values["analysis_record"]
        self.assertEqual(
            set(record),
            {"schema_version", "status", "question", "identity_fingerprint", "steps"},
            f"记录键集超出有界事实（贡献率/叙事/claims 都不得入 checkpoint）：{sorted(record)}",
        )

    def test_encoded_rows_survive_sqlite_checkpoint_for_replay(self) -> None:
        """带类型编码的 rows 经 SqliteSaver 落盘往返后类型与值均保持（可供复算）。"""
        agent = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=self.sqlite
        )
        sid = "t06-bounds-rows"
        agent._begin_analysis(sid, ANALYSIS_Q, dict(HQ_CLAIMS))
        rows = (("east", Decimal("10.5"), True, None, 3),)
        agent._write_analysis_state(
            sid,
            {
                "analysis_record": {
                    "steps": [
                        {
                            "index": 1,
                            "status": "ok",
                            "sql": "SELECT 1",
                            "columns": ["branch", "amt", "flag", "note", "n"],
                            "rows": encode_rows(rows),
                            "latency_ms": 1.0,
                        }
                    ]
                }
            },
        )
        stored = _values(agent, FINANCE_MODEL, sid)["analysis_record"]["steps"][0]["rows"]
        self.assertEqual(stored, encode_rows(rows), "编码形态在 checkpoint 往返后失真")
        decoded = decode_rows(stored)
        self.assertEqual(decoded, rows)
        self.assertIsInstance(decoded[0][1], Decimal, "Decimal 往返丢类型")
        self.assertIs(decoded[0][2], True, "bool 往返退化成 int")
        self.assertIsNone(decoded[0][3])

    def test_stale_success_state_does_not_leak_into_next_normal_turn(self) -> None:
        """一轮分析（含 completed 成功状态）之后再普通问数：不携带旧 analysis_record。"""
        agent = DataAgent(model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET)
        sid = "t06-bounds-stale"
        agent._begin_analysis(sid, ANALYSIS_Q, None)
        agent._write_analysis_state(
            sid, {"analysis_record": {"status": "completed", "totals": {"delta": 0.42}}}
        )
        result = agent.ask(FINANCE_Q, session_id=sid)
        self.assertEqual(result.kind, "answer", _why(result))
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertIsNone(
            values.get("analysis_record"), "普通轮仍携带旧 analysis_record 的成功状态"
        )
        self.assertFalse(values.get("analysis_followup_blocked"))
        self.assertEqual(result.turns_in_session, 2)
        self.assertIsNotNone(result.sql, "普通轮没有产出本轮自己的 SQL（旧状态渗透）")

    def test_checkpoint_allowlist_excludes_analysis_dataclasses(self) -> None:
        """序列化白名单不得出现 AnalysisResult / Attribution（决策 ④ ⑦：不进 checkpoint）。"""
        graph = build_graph(executor=FakeExecutor(), budget=BUDGET)
        pairs = _allowlist(graph.checkpointer)
        self.assertNotIn(("agent.analysis", "AnalysisResult"), pairs)
        self.assertFalse(
            any(class_name == "Attribution" for _, class_name in pairs),
            f"Attribution 进了 msgpack 白名单：{sorted(pairs)}",
        )


class TestAnalysisIdentityTransfers(unittest.TestCase):
    """检查单 2 的身份面（决策 ④ ②）：冲突在任何状态写入前发生（零写入零 SQL）。

    与普通入口的对照（决策 ④ ②「普通入口不改变转移规则」）：ask 沿用 ADR-0020
    决策 ⑥ 的宽松语义（匿名先行仍可绑定；已绑定会话的匿名 ask 放行），分析入口
    才使用更严的按记录校验。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-t06-id-")
        self.addCleanup(self._tmp.cleanup)
        self.sqlite = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(self.sqlite.conn.close)

    def _agent(self, executor: FakeExecutor) -> DataAgent:
        return DataAgent(
            model=FINANCE_MODEL, executor=executor, budget=BUDGET, checkpointer=self.sqlite
        )

    def test_bound_session_rejects_anonymous_analysis(self) -> None:
        """已绑定会话收到匿名 analyze → 冲突，零写入零 SQL（决策 ④ ②）。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        sid = "t06-id-anon"
        result = agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(result.kind, "answer", _why(result))
        before = _values(agent, FINANCE_MODEL, sid)
        with self.assertRaises(SessionIdentityConflict):
            agent._begin_analysis(sid, ANALYSIS_Q, None)
        self.assertEqual(
            _values(agent, FINANCE_MODEL, sid),
            before,
            "冲突进入改写了状态：校验必须先于一切写入",
        )
        self.assertEqual(len(executor.calls), 1, "冲突进入执行了 SQL")

    def test_bound_session_rejects_foreign_identity_analysis(self) -> None:
        """已绑定会话收到另一身份 analyze → 冲突，零写入。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        sid = "t06-id-foreign"
        agent.ask(FINANCE_Q, session_id=sid, identity=HQ_CLAIMS)
        before = _values(agent, FINANCE_MODEL, sid)
        with self.assertRaises(SessionIdentityConflict):
            agent._begin_analysis(sid, ANALYSIS_Q, BRANCH_CLAIMS)
        self.assertEqual(_values(agent, FINANCE_MODEL, sid), before)

    def test_anonymous_session_accepts_identity_analysis_first_binding(self) -> None:
        """匿名会话带身份 analyze → 沿用首次绑定语义（绑定指纹 + 记录身份指纹）。"""
        agent = self._agent(FakeExecutor())
        sid = "t06-id-bind"
        record = agent._begin_analysis(sid, ANALYSIS_Q, dict(HQ_CLAIMS))
        expected = claims_fingerprint(dict(HQ_CLAIMS))
        self.assertEqual(record["identity_fingerprint"], expected)
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertEqual(values.get("session_fingerprint"), expected)
        self.assertEqual(values.get("turns"), 1)

    def test_running_anonymous_record_rejects_identity_entry(self) -> None:
        """匿名分析 running 中收到实名进入 → 拒绝（先前匿名仅可匿名接续），零写入。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        sid = "t06-id-anon-run"
        agent._begin_analysis(sid, ANALYSIS_Q, None)
        before = _values(agent, FINANCE_MODEL, sid)
        with self.assertRaises(SessionIdentityConflict):
            agent._begin_analysis(sid, ANALYSIS_Q2, dict(HQ_CLAIMS))
        self.assertEqual(
            _values(agent, FINANCE_MODEL, sid),
            before,
            "running 恢复的身份拒绝未做到零写入",
        )

    def test_running_identified_record_rejects_anonymous_entry(self) -> None:
        """实名分析 running 中收到匿名进入 → 拒绝（实名亦然），零写入。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        sid = "t06-id-id-run"
        agent._begin_analysis(sid, ANALYSIS_Q, dict(HQ_CLAIMS))
        before = _values(agent, FINANCE_MODEL, sid)
        with self.assertRaises(SessionIdentityConflict):
            agent._begin_analysis(sid, ANALYSIS_Q2, None)
        self.assertEqual(_values(agent, FINANCE_MODEL, sid), before)

    def test_running_record_rejects_mismatched_fingerprint(self) -> None:
        """running 恢复时指纹不符 → 拒绝（仅接受与记录一致的身份），零写入。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        sid = "t06-id-mismatch"
        agent._begin_analysis(sid, ANALYSIS_Q, dict(HQ_CLAIMS))
        before = _values(agent, FINANCE_MODEL, sid)
        with self.assertRaises(SessionIdentityConflict):
            agent._begin_analysis(sid, ANALYSIS_Q2, dict(BRANCH_CLAIMS))
        self.assertEqual(_values(agent, FINANCE_MODEL, sid), before)

    def test_normal_paths_keep_the_lenient_identity_rules(self) -> None:
        """ask 的既有身份语义不被分析规则误伤：匿名先行可绑定、已绑定匿名 ask 放行。"""
        agent = self._agent(FakeExecutor())
        sid = "t06-id-lenient"
        r1 = agent.ask(FINANCE_Q, session_id=sid)
        self.assertEqual(r1.kind, "answer", _why(r1))
        r2 = agent.ask(FINANCE_FOLLOWUP_Q, session_id=sid, identity=dict(HQ_CLAIMS))
        self.assertEqual(r2.kind, "answer", _why(r2))
        self.assertEqual(r2.turns_in_session, 2)
        r3 = agent.ask(FINANCE_Q, session_id=sid)
        self.assertEqual(r3.kind, "answer", _why(r3))


class TestAnalysisInterruption(unittest.TestCase):
    """检查单 4：注入中断——旧 running 在下次同身份进入时标 interrupted（不增旧轮计数）；
    重启不自动续跑不发 SQL；持久化失败不得返回伪成功；重启不污染 last_plan。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-t06-int-")
        self.addCleanup(self._tmp.cleanup)
        self.sqlite = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(self.sqlite.conn.close)

    def _savers(self):
        """memory 与 sqlite 双 saver 参数化（memory 用显式实例复刻「重启后同一存储」）。"""
        yield "memory", MemorySaver()
        yield "sqlite", self.sqlite

    def test_restart_keeps_running_then_marks_interrupted_on_next_entry(self) -> None:
        """开始后崩溃：重启仍 running；同身份再进入 → running→interrupted→running。"""
        for label, saver in self._savers():
            with self.subTest(saver=label):
                sid = f"t06-int-{label}"
                executor = FakeExecutor()
                agent = DataAgent(
                    model=FINANCE_MODEL, executor=executor, budget=BUDGET, checkpointer=saver
                )
                agent._begin_analysis(sid, ANALYSIS_Q, dict(HQ_CLAIMS))
                # 注入中断：重新构造 Agent（同一存储 = 重启续接的复刻）
                executor2 = FakeExecutor()
                revived = DataAgent(
                    model=FINANCE_MODEL, executor=executor2, budget=BUDGET, checkpointer=saver
                )
                values = _values(revived, FINANCE_MODEL, sid)
                self.assertEqual(
                    values["analysis_record"]["status"],
                    "running",
                    "重启即自动改写终态 = 无中生有的续跑语义（决策 ④ ⑤：崩溃保留 running）",
                )
                self.assertEqual(executor2.calls, [], "重启后不得自动发 SQL")
                # 下次同身份进入：先标 interrupted（不增旧轮计数），再接纳新一轮
                record = revived._begin_analysis(sid, ANALYSIS_Q2, dict(HQ_CLAIMS))
                self.assertEqual((record["status"], record["question"]), ("running", ANALYSIS_Q2))
                values = _values(revived, FINANCE_MODEL, sid)
                self.assertEqual(
                    values.get("turns"),
                    2,
                    "恢复进入应恰好 +1：中断标记不得另计一轮（不增旧轮计数）",
                )
                history = _record_history(revived, FINANCE_MODEL, sid)
                self.assertEqual(
                    [r["status"] for r in history],
                    ["running", "interrupted", "running"],
                    f"中断标记未发生或顺序不对：{[r['status'] for r in history]}",
                )
                self.assertEqual(
                    history[1]["question"],
                    ANALYSIS_Q,
                    "interrupted 版本丢了旧轮问句（标记必须保留旧事实）",
                )
                self.assertEqual(executor2.calls, [], "恢复进入不得执行任何 SQL")

    def test_interrupted_marking_after_a_partial_step_keeps_step_facts(self) -> None:
        """任一步之后崩溃：interrupted 标记保留已写入的子步事实；新一轮 steps 重置。"""
        sid = "t06-int-step"
        saver = self.sqlite
        agent = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=saver
        )
        agent._begin_analysis(sid, ANALYSIS_Q, dict(HQ_CLAIMS))
        agent._write_analysis_state(
            sid, {"analysis_record": {"steps": [{"index": 1, "status": "ok"}]}}
        )
        revived = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=saver
        )
        revived._begin_analysis(sid, ANALYSIS_Q2, dict(HQ_CLAIMS))
        history = _record_history(revived, FINANCE_MODEL, sid)
        # 每次状态写入都是独立历史版本：begin(running) → 子步写入(running+steps)
        # → interrupted（同身份再进入）→ 新一轮 begin(running)
        self.assertEqual(
            [r["status"] for r in history],
            ["running", "running", "interrupted", "running"],
            f"中断标记未发生或顺序不对：{[r['status'] for r in history]}",
        )
        self.assertEqual(
            history[1]["steps"],
            [{"index": 1, "status": "ok"}],
            "子步写入的版本丢了 steps",
        )
        self.assertEqual(
            history[2]["steps"],
            [{"index": 1, "status": "ok"}],
            "中断标记抹掉了已写入的子步事实",
        )
        self.assertEqual(
            _values(revived, FINANCE_MODEL, sid)["analysis_record"]["steps"],
            [],
            "新一轮 running 记录没有重置 steps（旧轮步骤渗入新轮）",
        )

    def test_persistence_failure_returns_no_fake_success(self) -> None:
        """持久化失败：异常如实上抛、状态零写入——不得静默返回伪成功。"""

        class FailingSaver(MemorySaver):
            """put / put_writes 一律失败的 saver（注入磁盘故障）。"""

            def put(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("注入的持久化故障（T06 测试）")

            def put_writes(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("注入的持久化故障（T06 测试）")

        saver = FailingSaver()
        executor = FakeExecutor()
        agent = DataAgent(model=FINANCE_MODEL, executor=executor, budget=BUDGET, checkpointer=saver)
        sid = "t06-int-fail"
        with self.assertRaises(OSError):
            agent._begin_analysis(sid, ANALYSIS_Q, None)
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertNotIn("analysis_record", values, "持久化失败仍写入了 running 记录（伪成功）")
        self.assertNotIn("turns", values, "持久化失败仍推进了轮数")
        self.assertEqual(executor.calls, [])

    def test_restart_and_recovery_do_not_pollute_last_plan(self) -> None:
        """完成门槛：失败与重启不污染 last_plan，也不静默承接旧指标（无 SQL）。"""
        executor = FakeExecutor()
        agent = DataAgent(
            model=FINANCE_MODEL, executor=executor, budget=BUDGET, checkpointer=self.sqlite
        )
        sid = "t06-int-plan"
        result = agent.ask(FINANCE_FILTERED_Q, session_id=sid)
        self.assertEqual(result.kind, "answer", _why(result))
        plan_before = _values(agent, FINANCE_MODEL, sid)["last_plan"]
        agent._begin_analysis(sid, ANALYSIS_Q, None)
        revived = DataAgent(
            model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET, checkpointer=self.sqlite
        )
        revived._begin_analysis(sid, ANALYSIS_Q2, None)
        values = _values(revived, FINANCE_MODEL, sid)
        self.assertEqual(
            values.get("last_plan"),
            plan_before,
            "中断恢复改写了 last_plan（完成门槛：重启不污染 last_plan）",
        )


class TestAnalysisFollowupBlocked(unittest.TestCase):
    """检查单 3 尾：analysis_followup_blocked 阻止残句追问偷接旧 Plan（明确提示完整
    重述）；只有一次明确的普通 Plan 执行成功才解除；被拦轮零 SQL、不改 last_plan。
    """

    def _blocked_agent(self) -> tuple[DataAgent, FakeExecutor, str, Plan]:
        """问一轮 → 开始分析 → 写 completed 终态：得到挂 blocked 的会话。"""
        executor = FakeExecutor()
        agent = DataAgent(model=FINANCE_MODEL, executor=executor, budget=BUDGET)
        sid = "t06-followup"
        first = agent.ask(FINANCE_FILTERED_Q, session_id=sid)
        self.assertEqual(first.kind, "answer", _why(first))
        plan_before = _values(agent, FINANCE_MODEL, sid)["last_plan"]
        agent._begin_analysis(sid, ANALYSIS_Q, None)
        agent._write_analysis_state(sid, {"analysis_record": {"status": "completed"}})
        return agent, executor, sid, plan_before

    def test_residual_followup_gets_clarification_not_the_old_plan(self) -> None:
        """blocked 下的残句追问 → 明确反问完整重述，不偷接旧 Plan、零 SQL。"""
        agent, executor, sid, plan_before = self._blocked_agent()
        calls_before = len(executor.calls)
        blocked = agent.ask(FINANCE_FOLLOWUP_Q, session_id=sid)
        self.assertEqual(blocked.kind, "clarify", f"残句追问偷接了旧 Plan 口径（{_why(blocked)}）")
        self.assertIsNotNone(blocked.clarification)
        joined = " ".join(blocked.clarification.reasons)
        self.assertIn("完整重述", joined, f"反问未明确提示完整重述：{joined}")
        self.assertEqual(len(executor.calls), calls_before, "被拦轮执行了 SQL")
        # ask(1) → begin(2) → 被拦追问(3)：被拦轮同样是一轮（plan 节点 +1）
        self.assertEqual(blocked.turns_in_session, 3)
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertEqual(values.get("last_plan"), plan_before, "被拦轮改写了 last_plan")
        self.assertTrue(values.get("analysis_followup_blocked"), "clarify 轮不得解除 blocked")

    def test_explicit_plan_execution_success_unblocks(self) -> None:
        """明确的普通 Plan（run_plan 直执）成功 → explain 回写解除 blocked。"""
        agent, _, sid, plan_before = self._blocked_agent()
        result = agent.run_plan(plan_before, session_id=sid, question="显式重述的普通计划")
        self.assertEqual(result.kind, "answer", _why(result))
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertFalse(
            values.get("analysis_followup_blocked"),
            "明确的普通 Plan 执行成功后仍处于 blocked（解除条件错了）",
        )
        self.assertIsNone(values.get("analysis_record"))


class TestAnalysisWindowSerialization(unittest.TestCase):
    """检查单 2 的并发面与覆盖面：分析四步与普通请求在同一实例 RLock 内串行
    （屏障证明普通请求不能插入分析中间）；新会话从 1 计轮；跨模型同 sid 互不串话。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-t06-win-")
        self.addCleanup(self._tmp.cleanup)
        self.sqlite = sqlite_saver(Path(self._tmp.name) / "checkpoints.sqlite")
        self.addCleanup(self.sqlite.conn.close)

    def test_normal_request_cannot_interleave_an_in_flight_analysis(self) -> None:
        """屏障 + 事件：分析持锁停在同一子步中间时，普通请求必须等在锁外。"""
        gate = threading.Event()
        barrier = threading.Barrier(2)

        class GatedExecutor(FakeExecutor):
            """第一次执行即与主线程会合、随后等待放行（模拟长查询卡在子步中间）。

            只会合第一次调用：后续普通轮的执行（gate 放行后发生）直接通过，
            否则它会卡在已消费的屏障上超时，而不是被锁正确串行化的结果。
            """

            def __init__(self, bar: threading.Barrier, rel: threading.Event) -> None:
                super().__init__()
                self._barrier = bar
                self._gate = rel
                self._gated = False

            def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
                self.calls.append(sql)
                if not self._gated:
                    self._gated = True
                    self._barrier.wait(timeout=10)
                    self._gate.wait(timeout=10)
                return [tuple(r) for r in self.rows], list(self.columns)

        executor = GatedExecutor(barrier, gate)
        agent = DataAgent(model=FINANCE_MODEL, executor=executor, budget=BUDGET)
        sid = "t06-barrier"
        plan = Planner(FINANCE_MODEL).plan(FINANCE_Q)
        assert isinstance(plan, Plan)  # 前提守卫：确定性命中才有子步可执行

        errors: list[Exception] = []

        def _analysis() -> None:
            try:
                with agent._lock:
                    record = agent._begin_analysis(sid, ANALYSIS_Q, None)
                    agent._run_analysis_step(plan, identity=None)
                    agent._write_analysis_state(
                        sid, {"analysis_record": {**record, "status": "completed"}}
                    )
            except Exception as exc:  # noqa: BLE001 - 线程内失败经 errors 透出
                errors.append(exc)

        results: list[TurnResult] = []

        def _normal() -> None:
            results.append(agent.ask(FINANCE_Q, session_id=sid))

        worker = threading.Thread(target=_analysis)
        worker.start()
        barrier.wait(timeout=10)  # 与子步内的执行器会合：分析正持锁停在中间
        waiter = threading.Thread(target=_normal)
        waiter.start()
        time.sleep(0.25)  # 给普通线程足够的窗口：若锁失效它此刻已完成
        self.assertTrue(
            waiter.is_alive(),
            "普通请求在分析持锁期间就完成了——实例锁失效，普通请求插入了分析中间",
        )
        self.assertEqual(len(executor.calls), 1, "普通请求在分析中间执行了 SQL（锁失效）")
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertEqual(values.get("turns"), 1, "普通请求在分析中间推进了轮数")
        self.assertEqual(values["analysis_record"]["status"], "running", "普通请求打断了分析记账")
        gate.set()
        worker.join(timeout=10)
        waiter.join(timeout=10)
        self.assertEqual(errors, [], f"分析线程失败：{errors}")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "answer", _why(results[0]))
        self.assertEqual(results[0].turns_in_session, 2)
        final = _values(agent, FINANCE_MODEL, sid)
        self.assertIsNone(final.get("analysis_record"))
        self.assertFalse(final.get("analysis_followup_blocked"))
        self.assertEqual(len(executor.calls), 2)

    def test_analysis_on_fresh_session_counts_its_own_first_turn(self) -> None:
        """分析新会话：没有任何普通轮的 thread 上，开始分析 → turns == 1。"""
        agent = DataAgent(model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET)
        sid = "t06-fresh"
        agent._begin_analysis(sid, ANALYSIS_Q, None)
        self.assertEqual(_values(agent, FINANCE_MODEL, sid).get("turns"), 1)

    def test_same_sid_across_models_stays_isolated(self) -> None:
        """跨模型同 sid（共享同一 SQLite 文件）：零售普通轮不得碰金融的分析记账。"""
        fin_executor = FakeExecutor()
        ret_executor = FakeExecutor()
        fin = DataAgent(
            model=FINANCE_MODEL, executor=fin_executor, budget=BUDGET, checkpointer=self.sqlite
        )
        ret = DataAgent(
            model=RETAIL_MODEL, executor=ret_executor, budget=BUDGET, checkpointer=self.sqlite
        )
        sid = "t06-cross"
        fin._begin_analysis(sid, ANALYSIS_Q, None)
        result = ret.ask(RETAIL_Q, session_id=sid)
        self.assertEqual(result.kind, "answer", _why(result))
        fin_values = _values(fin, FINANCE_MODEL, sid)
        self.assertEqual(fin_values.get("turns"), 1)
        self.assertEqual(fin_values["analysis_record"]["question"], ANALYSIS_Q)
        self.assertEqual(fin_executor.calls, [], "零售轮动了金融的 thread（跨模型串话）")
        ret_values = _values(ret, RETAIL_MODEL, sid)
        self.assertEqual(ret_values.get("turns"), 1)
        self.assertIsNone(ret_values.get("analysis_record"))


class TestAnalysisFollowupBlockedLegacyCheckpoint(unittest.TestCase):
    """决策 ④ ⑥ 的兼容面：未含新字段的旧 checkpoint 仍按原追问逻辑读取。

    旧 checkpoint（T06 之前落盘）里既没有 analysis_followup_blocked 也没有
    analysis_record——`state.get` 返回 None（falsy），残句追问必须照旧走
    last_plan 补全，不得被新守卫误拦。
    """

    def test_legacy_checkpoint_without_new_fields_keeps_followup(self) -> None:
        agent = DataAgent(model=FINANCE_MODEL, executor=FakeExecutor(), budget=BUDGET)
        # 先在临时 sid 上跑一轮真图，取一个真实的 Plan 作为旧 last_plan
        scratch = agent.ask(FINANCE_FILTERED_Q, session_id="t06-legacy-scratch")
        self.assertEqual(scratch.kind, "answer", _why(scratch))
        plan = _values(agent, FINANCE_MODEL, "t06-legacy-scratch")["last_plan"]
        # 手工构造「旧时代」thread：只有旧字段，绝无新字段
        sid = "t06-legacy"
        config = _config(FINANCE_MODEL, sid)
        agent._graph.update_state(
            config,
            {"question": FINANCE_Q, "session_id": sid, "turns": 1, "last_plan": plan},
            as_node="explain",
        )
        values = _values(agent, FINANCE_MODEL, sid)
        self.assertNotIn("analysis_followup_blocked", values)
        self.assertNotIn("analysis_record", values)
        result = agent.ask(FINANCE_FOLLOWUP_Q, session_id=sid)
        self.assertEqual(result.kind, "answer", _why(result))
        self.assertIn("10000000", result.sql or "", "旧 checkpoint 的追问补全丢了谓词")
