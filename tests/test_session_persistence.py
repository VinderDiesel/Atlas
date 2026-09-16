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
import unittest
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
from agent.security.sql_guard import Budget
from agent.state import TurnResult

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
