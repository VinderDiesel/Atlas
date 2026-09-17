#!/usr/bin/env python3
"""绑定关系必须可见（ADR-0019 决策 ⑥ + ADR-0020 决策 ⑦，工作项 6；无 DB）。

失效形态（0019 背景节实测）：改造前 `/health` 只有 3 键
（`status` / `head_sha` / `snapshot_sha`），`snapshot_sha=null` 时**看不出服务到底
绑在哪份快照上**——是「HEAD 恰好没 meta 所以回退到最新已锁」还是「根本没绑定」，
两种情况的响应体一模一样。而决策 ① 之后运行时可以合法地绑在非 HEAD 快照上，
「绑在哪」从可选信息变成必需信息（代价 ③：非 HEAD 绑定的唯一约束就是回显）。

本文件锁四件事，逐条对应决策 ⑥ / ⑦：

1. **`/health` 八字段**（键集权威清单见 ADR-0022 决策 ① 的「`/api/v1/health` 响应体」
   段——它自己声明「不再增删键」，权威来源是 0019 决策 ⑥ + 0020 决策 ⑦；本批只增不改名）；
2. **回显值来自解析函数本身**，不是端点里另算一份——断言方式是「把解析函数换掉，
   响应必须跟着变」；这是本文件最重要的一条：否则 `data/identity.py` 与 `api.py`
   就成了两个事实源，正是决策 ② 花一整批消除的东西；
3. **`/ask` 回显本轮实际绑定**（`snapshot_sha` + `snapshot_bound_to_head` 与
   `explanation` **并列**，不进 explanation 的 13 个固定键）；
4. **`boot_id`**：同进程两次调用相同、形状是 uuid4().hex（32 位小写 hex）。

另三条反向/文案断言（都是本批登记过的残留）：

- `/health` 是**公开面**且**不得触发 agent 构造**（构造会连 Doris）；
- `SnapshotUnavailable` → 503 的前缀文案不得说「无法绑定评测数据」——绑错快照
  ≠ 无评测数据，且「评测数据」在 N6 语境特指 `eval/runner` 的 HEAD 严格路径
  （把 503 说成评测问题会把人引到错误的现场去查）；
- `status` 在无快照可绑时必须是 `degraded` 而非 `ok`（键数不变，值诚实）。
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from agent.factory import SnapshotUnavailable
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from data.identity import RuntimeSnapshot
from serving.api import create_app
from serving.audit import AuditLog
from serving.auth import sign_token

REPO = Path(__file__).resolve().parent.parent
SECRET = "identity-echo-test-secret"

# 契约 v2（ADR-0022 决策 ①②）：业务端点带 /api/v1 前缀；`/health` 双挂——
# 本文件保留根挂点调用（compose 探针路径，决策 ② 的兼容锚点），前缀挂点的
# 同 body 断言在 tests/test_api_contract_v2.py。
API = "/api/v1"

# 分写拼接：本文件下方有一条「api.py 不得出现该命令字面量」的检索断言，而
# ADR-0019 判据 5(a) 会全仓检索 `.py` 并断言命中集恰为「1 生产 + 2 独立预言机」。
# 在这两个文件里写出字面量就是自造命中（同 tests/test_container_identity.py 的既有
# 纪律：判据的误报方向也算判据缺陷，见 0019 判据 5(a) 登记）
_REV_PARSE = "rev" + "-parse"

# 0019 决策 ⑥ 的 7 键 + 0020 决策 ⑦ 的 boot_id（0022 决策 ① 全文照抄并声明不再增删键）
HEALTH_KEYS = {
    "status",
    "head_sha",
    "snapshot_sha",
    "snapshot_source",
    "snapshot_bound_to_head",
    "snapshot_created_at",
    "snapshot_tables",
    "boot_id",
}

FINANCE_Q = "按分支统计 2013 年佣金收入，列出前 5 名"

# 测试用绑定：不是真实快照 sha（真值来自 data/snapshots/，断言只锁「回显 == 注入」）
STUB_SNAPSHOT = RuntimeSnapshot(
    sha="abc1234",
    meta={"sha": "abc1234", "created_at": "2026-09-09T11:56:23+08:00", "row_counts": {}},
    source="latest",
    bound_to_head=False,
)

# 桩 agent 的最小预算（`_agent()` 与「双来源守卫」那组用例共用一份，避免两处口径漂）
_BUDGET = Budget(dialect="doris", max_rows=100)


class FakeExecutor:
    """确定性链路的假执行器（同 tests/test_api.py 的注入面）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
        self.calls.append(sql)
        return [("v",)], ["v"]


def _agent(snapshot: RuntimeSnapshot | None = None) -> DataAgent:
    """构造桩 agent：走 `snapshot=` 参数（生产 factory 同一条路），不事后改属性。

    事后 `agent.snapshot = x` 会让测试测到一条生产不存在的赋值路径。
    """
    if snapshot is None:
        return DataAgent(executor=FakeExecutor(), budget=_BUDGET)
    return DataAgent(executor=FakeExecutor(), budget=_BUDGET, snapshot=snapshot)


class EchoTestBase(unittest.TestCase):
    """共享注入面：fake agent 工厂 + tmp 审计目录 + JWT secret 恢复。"""

    def setUp(self) -> None:
        self._secret_was_set = "ATLAS_JWT_SECRET" in os.environ
        self._secret_orig = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-echo-")
        self.audit = AuditLog(Path(self._audit_tmp.name))
        self.agent = _agent(STUB_SNAPSHOT)
        self.built: list[str] = []

        def factory(model_name: str) -> DataAgent:
            self.built.append(model_name)
            return self.agent

        self.client = TestClient(create_app(agent_factory=factory, audit=self.audit))

    def tearDown(self) -> None:
        self.client.close()
        self._audit_tmp.cleanup()
        if self._secret_was_set:
            os.environ["ATLAS_JWT_SECRET"] = self._secret_orig or ""
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {sign_token('hq_admin', {}, secret=SECRET)}"}


class TestHealthEcho(EchoTestBase):
    """决策 ⑥：`/health` 是公开面，必须说清「绑在哪、为什么」。"""

    def test_health_has_exactly_eight_keys(self) -> None:
        body: dict[str, Any] = self.client.get("/health").json()
        self.assertEqual(
            set(body),
            HEALTH_KEYS,
            f"键集与 0019 决策 ⑥ + 0020 决策 ⑦ 不符：多 {set(body) - HEALTH_KEYS} "
            f"缺 {HEALTH_KEYS - set(body)}",
        )
        self.assertEqual(
            len(body), 8, f"设计页 §5 逐批复验表 P-1 行断言 /health = 8 键，实测 {len(body)}"
        )

    def test_health_does_not_build_agents(self) -> None:
        """公开面不得触发 agent 构造（构造 = 连 Doris，健康探针不该有副作用）。"""
        self.client.get("/health")
        self.assertEqual(
            self.built, [], "/health 触发了 agent 工厂——探针变成重路径，且可被用作打库放大器"
        )

    def test_echo_values_come_from_the_resolver_not_a_second_copy(self) -> None:
        """本文件最重要的一条：换掉解析函数，响应必须跟着变。

        如果 `/health` 自己再算一遍 sha / source / created_at，那它和
        `data/identity.py` 就是两个事实源——本批前面所有工作项消除的正是这种东西，
        而只有「注入不同值」才能证明端点没有自己的算法。
        """
        injected = RuntimeSnapshot(
            sha="eeeeeee",
            meta={
                "sha": "eeeeeee",
                "created_at": "2026-01-02T03:04:05+08:00",
                "row_counts": {"dwd": {"a": 1}, "tpcdi": {"b": 2, "c": 3}},
            },
            source="env",
            bound_to_head=True,
            head="eeeeeee",
        )
        with mock.patch("serving.api.resolve_runtime_snapshot", return_value=injected):
            body = self.client.get("/health").json()
        self.assertEqual(body["snapshot_sha"], "eeeeeee")
        self.assertEqual(body["snapshot_source"], "env")
        self.assertTrue(body["snapshot_bound_to_head"], "bound_to_head 必须是 JSON 布尔")
        self.assertEqual(body["snapshot_created_at"], "2026-01-02T03:04:05+08:00")
        self.assertEqual(body["snapshot_tables"], 3, "表数 = row_counts 展开后的表条目数")
        # head_sha 也在「来自解析函数」的范围内：端点若自己再跑一次 git（变异 M1），
        # 本例注入的 sha 与真实 HEAD 不同，只有断言它才抓得住——而 `head` 必须来自
        # 解析结果，否则它与 bound_to_head 是两次读取，中间 commit 即自相矛盾
        self.assertEqual(body["head_sha"], "eeeeeee")
        self.assertEqual(body["status"], "ok")

    def test_snapshot_tables_is_whitelist_size(self) -> None:
        """表数必须与 Guard 白名单同口径（回显 29 而白名单 25 就是误导）。"""
        with mock.patch("serving.api.resolve_runtime_snapshot", return_value=STUB_SNAPSHOT):
            body = self.client.get("/health").json()
        self.assertEqual(body["snapshot_tables"], 0, "meta.row_counts 空 → 0 张表")
        # 真实快照：与 build_budget 的 allowed_tables 元素数一致
        from data.identity import resolve_runtime_snapshot
        from eval.runner import build_budget

        snap = resolve_runtime_snapshot()
        budget = build_budget(snap.meta)
        real: dict[str, Any] = self.client.get("/health").json()
        self.assertEqual(real["snapshot_tables"], len(budget.allowed_tables))

    def test_status_degraded_when_nothing_to_bind(self) -> None:
        """无 meta 可绑时不得报 ok（回显的存在意义就是别说假话）。"""
        with mock.patch(
            "serving.api.resolve_runtime_snapshot",
            side_effect=SnapshotUnavailable("tmp 目录无锁定快照 meta（测试注入）"),
        ):
            resp = self.client.get("/health")
        body = resp.json()
        self.assertEqual(resp.status_code, 200, "存活面不因数据面缺配置而 5xx（探针语义）")
        self.assertEqual(body["status"], "degraded")
        self.assertIsNone(body["snapshot_sha"])
        self.assertEqual(set(body), HEALTH_KEYS, "降级路径的键集必须与正常路径相同")

    def test_bound_to_head_agrees_with_head_sha_vs_snapshot_sha(self) -> None:
        """两键必须自洽：`bound_to_head` 就是「sha == head」这一件事。

        若 `/health` 自己另调一次 HEAD 解析（改造前的写法），两次读取之间 commit
        一次就能让 `head_sha` 与 `snapshot_sha` 相等而 `bound_to_head=false`。
        """
        body = self.client.get("/health").json()
        self.assertEqual(
            body["snapshot_bound_to_head"],
            body["snapshot_sha"] == body["head_sha"],
            f"回显自相矛盾：{body}",
        )

    def test_head_sha_stays_str_or_none_contract(self) -> None:
        """向后兼容（决策 ⑥）：既有两键保留原名与原类型，只增不改名。"""
        body = self.client.get("/health").json()
        self.assertIsInstance(body["head_sha"], str)
        self.assertTrue(body["head_sha"])
        self.assertTrue(body["snapshot_sha"] is None or isinstance(body["snapshot_sha"], str))


class TestTableCountSingleCaliber(unittest.TestCase):
    """`snapshot_tables` 的口径必须等于 Guard 白名单（回显 29 / 放行 25 就是误导）。

    `RuntimeSnapshot.table_count`（身份模块）与 `eval.runner.build_budget`（Guard
    白名单）是同一份 `meta["row_counts"]` 的两次推导——不能靠注释保证一致，只能逐份
    真实 meta 断言（0019 判据 5 的一贯做法：按行为检索等价，不按文本）。
    """

    def test_matches_guard_whitelist_on_all_real_metas(self) -> None:
        import json

        from data.identity import SNAPSHOT_DIR
        from eval.runner import build_budget

        metas = sorted(SNAPSHOT_DIR.glob("*.meta.json"))
        self.assertTrue(metas, "真实 data/snapshots/ 无 meta，本判据失去对象")
        for path in metas:
            meta = json.loads(path.read_text(encoding="utf-8"))
            snap = RuntimeSnapshot(sha=path.name[:7], meta=meta, source="head", bound_to_head=True)
            with self.subTest(snapshot=path.name[:7]):
                self.assertEqual(snap.table_count, len(build_budget(meta).allowed_tables))

    def test_malformed_row_counts_raises_not_zero(self) -> None:
        """缺 row_counts 或形态不对 → 抛错；静默给 0 会让回显说「这快照没表」。"""
        for meta in ({"sha": "deadbee"}, {"sha": "deadbee", "row_counts": {"dwd": 5}}):
            with (
                self.subTest(meta=meta),
                mock.patch(
                    "serving.api.resolve_runtime_snapshot",
                    return_value=RuntimeSnapshot(
                        sha="deadbee", meta=meta, source="head", bound_to_head=True
                    ),
                ),
            ):
                client = TestClient(create_app(agent_factory=lambda _m: _agent(None)))
                body = client.get("/health").json()
            self.assertEqual(body["status"], "degraded", f"坏 meta 仍报 ok：{body}")
            self.assertIsNone(body["snapshot_tables"])


class TestAgentBindingSingleSource(unittest.TestCase):
    """回显的两个键与预算必须来自**同一份**绑定（决策 ② 的构造期落点）。

    `DataAgent` 同时收 `snapshot`（完整绑定）与 `snapshot_meta`（只有内容）：若两者
    的 meta 不一致而构造成功，则 Guard 白名单来自 A、`/ask` 回显的 sha 来自 B——
    响应会自信地指错快照。本类锁三件事：不一致必须抛、一致（或只给一个）必须放过、
    只给 meta 时 `snapshot` 为 None（回显 null，而不是假称绑在 HEAD 上）。
    """

    def test_conflicting_meta_raises(self) -> None:
        other = {"sha": "fffffff", "created_at": "x", "row_counts": {}}
        with self.assertRaises(ValueError) as ctx:
            DataAgent(
                executor=FakeExecutor(), budget=_BUDGET, snapshot=STUB_SNAPSHOT, snapshot_meta=other
            )
        msg = str(ctx.exception)
        self.assertIn("两个来源", msg, f"消息须点根因，只说参数名等于让人猜：{msg}")
        self.assertIn("abc1234", msg, "消息须带出冲突的 sha，否则排查要重跑一遍")

    def test_matching_meta_is_accepted(self) -> None:
        """守卫不得过宽：同一份 meta 传两次是合法调用形态（不因此抛错）。

        过宽的守卫会把「外部只拿到 meta 又想显式说明」的调用一并拒掉，而那没有
        第二事实源可言。
        """
        agent = DataAgent(
            executor=FakeExecutor(),
            budget=_BUDGET,
            snapshot=STUB_SNAPSHOT,
            snapshot_meta=STUB_SNAPSHOT.meta,
        )
        self.assertIs(agent.snapshot, STUB_SNAPSHOT)
        self.assertIs(agent.snapshot_meta, STUB_SNAPSHOT.meta, "预算与回显必须同源")

    def test_meta_only_agent_has_no_runtime_binding(self) -> None:
        agent = DataAgent(
            executor=FakeExecutor(),
            budget=_BUDGET,
            snapshot_meta={"sha": "abcdefg", "row_counts": {}},
        )
        self.assertIsNone(agent.snapshot, "未经运行时解析构造的 agent 不得假称有绑定来源")


class TestBootId(EchoTestBase):
    """决策 ⑦（0020）：重启可见。"""

    def test_boot_id_stable_within_process_and_hex32(self) -> None:
        first = self.client.get("/health").json()["boot_id"]
        second = self.client.get("/health").json()["boot_id"]
        self.assertEqual(first, second, "同进程内 boot_id 必须恒定（否则无法判重启）")
        self.assertRegex(first, r"^[0-9a-f]{32}$", f"应为 uuid4().hex 形态，实测 {first!r}")

    def test_boot_id_changes_per_app_construction_module_constant(self) -> None:
        """`BOOT_ID` 是模块级常量：新进程才换值（本例只断言它是模块属性）。

        用 `assertEqual` 而非 `assertIs`：JSON 反序列化出来的是内容相同但
        身份不同的 str 对象，`assertIs` 测的是解释器驻留策略而非本判据。
        """
        import serving.api as api_module

        self.assertEqual(api_module.BOOT_ID, self.client.get("/health").json()["boot_id"])

    def test_boot_id_is_recomputed_per_module_evaluation(self) -> None:
        """0020 判据 9 的后半句「新进程不同」——不起子进程也能证的那个机制。

        跨进程取值要 spawn 两个解释器，那属真链侧；本例用 `importlib.reload` 重跑
        模块体，断言值随之改变。这测的是**唯一能造成跨进程差异的机制**（值在模块求值
        时随机生成）：若有人把它改成读环境变量、写进缓存文件、或做成类属性常驻，
        reload 就会给出同一个值，本例变红。
        """
        import importlib

        import serving.api as api_module

        before = api_module.BOOT_ID
        try:
            self.assertNotEqual(importlib.reload(api_module).BOOT_ID, before)
        finally:
            importlib.reload(api_module)  # 恢复，避免污染同批其他用例


class TestAskEcho(EchoTestBase):
    """决策 ⑥：响应里也要有本轮实际绑定，不能只在 /health。"""

    def test_answer_carries_binding_beside_explanation(self) -> None:
        resp = self.client.post(f"{API}/ask", json={"question": FINANCE_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200, resp.text[:400])
        body = resp.json()
        self.assertEqual(body["kind"], "answer")
        self.assertEqual(body["snapshot_sha"], "abc1234")
        self.assertIs(body["snapshot_bound_to_head"], False)

    def test_binding_is_not_inside_explanation(self) -> None:
        """explanation 的 13 个固定键不得被塞进新键（0019 决策 ⑥ 的语义归属裁定）。"""
        body = self.client.post(
            f"{API}/ask", json={"question": FINANCE_Q}, headers=self._auth()
        ).json()
        self.assertNotIn("snapshot_sha", body["explanation"])
        self.assertNotIn("snapshot_bound_to_head", body["explanation"])

    def test_payload_key_count_is_23(self) -> None:
        """设计页 §5 / dev-plan 计数格：19 + 2 + 1(chart) + 1(analysis, ADR-0026) = 23。"""
        body = self.client.post(
            f"{API}/ask", json={"question": FINANCE_Q}, headers=self._auth()
        ).json()
        self.assertEqual(len(body), 23, f"_turn_payload 应 23 键，实测 {len(body)}：{sorted(body)}")

    def test_unbound_agent_echoes_null_keys_not_absent(self) -> None:
        """字段全集恒定：没绑定也要出现这两个键且为 null，不得静默消失。"""
        unbound = _agent(None)
        client = TestClient(create_app(agent_factory=lambda _m: unbound, audit=self.audit))
        body = client.post(f"{API}/ask", json={"question": FINANCE_Q}, headers=self._auth()).json()
        self.assertIn("snapshot_sha", body)
        self.assertIn("snapshot_bound_to_head", body)
        self.assertIsNone(body["snapshot_sha"])
        self.assertIsNone(body["snapshot_bound_to_head"])
        # analysis 键恒在（ADR-0026 决策 ⑥）：非分析请求值为 null，键不消失
        self.assertIn("analysis", body)
        self.assertIsNone(body["analysis"])
        self.assertEqual(len(body), 23, "未绑定的键集仍须与正常路径相同（23 键）")

    def test_answer_carries_chart_spec(self) -> None:
        """ADR-0025 决策 ②：chart 挂独立键——answer 轮为 spec、由后端决定图型。"""
        body = self.client.post(
            f"{API}/ask", json={"question": FINANCE_Q}, headers=self._auth()
        ).json()
        self.assertEqual(body["kind"], "answer")
        chart = body["chart"]
        self.assertIsInstance(chart, dict)
        self.assertIn(chart["type"], {"bar", "line", "table"})
        self.assertIn("sql_sha256", chart)

    def test_chart_key_present_and_null_on_clarify(self) -> None:
        """判据 7：kind != answer 时 chart 为 null 而非缺失（字段全集稳定输出）。"""
        body = self.client.post(
            f"{API}/ask", json={"question": "最近交易情况怎么样？"}, headers=self._auth()
        ).json()
        self.assertEqual(body["kind"], "clarify")
        self.assertIn("chart", body)
        self.assertIsNone(body["chart"])

    def test_clarify_turn_also_echoes_binding(self) -> None:
        """反问轮同样要回显：绑定是回合级事实，与 kind 无关。"""
        body = self.client.post(
            f"{API}/ask", json={"question": "最近交易情况怎么样？"}, headers=self._auth()
        ).json()
        self.assertEqual(body["kind"], "clarify")
        self.assertEqual(body["snapshot_sha"], "abc1234")


class Test503Wording(EchoTestBase):
    """登记残留：503 前缀文案把「绑错快照」说成「无法绑定评测数据」。"""

    def test_503_detail_names_snapshot_not_evaluation_data(self) -> None:
        def boom(_model_name: str) -> DataAgent:
            raise SnapshotUnavailable("绑定快照 a11d779（source=latest）缺少语义模型所需表：x")

        client = TestClient(create_app(agent_factory=boom, audit=self.audit))
        resp = client.post(f"{API}/ask", json={"question": FINANCE_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 503)
        detail = resp.json()["detail"]
        self.assertIn("a11d779", detail, "原始解析消息必须带出，否则用户无从定位")
        self.assertNotIn(
            "评测数据",
            detail,
            "「评测数据」在 N6 语境特指 eval/runner 的 HEAD 严格路径，会把人引偏",
        )


class TestCliAskEcho(unittest.TestCase):
    """决策 ⑥ 的 CLI 面（0019 实施裁定 7 的落点清单未列 `atlas ask`，本类补该缺口）。"""

    def test_ask_prints_binding_line(self) -> None:
        """绑定行必须落在 stderr（与 `cmd_query` 同格式同流）。"""
        from agent import cli

        agent = _agent(STUB_SNAPSHOT)
        err, out = io.StringIO(), io.StringIO()

        def fake_create(_model_path: Path | None = None) -> DataAgent:
            return agent

        with (
            mock.patch.object(cli, "create_live_agent", fake_create),
            redirect_stderr(err),
            redirect_stdout(out),
        ):
            code = cli.main(["ask", FINANCE_Q])
        self.assertEqual(code, 0, (err.getvalue() + out.getvalue())[:400])
        self.assertIn("sha=abc1234", err.getvalue(), "ask 未回显绑定行（裁定 7 缺口）")
        self.assertRegex(err.getvalue(), r"source=\w+.*bound_to_head=(true|false)")
        self.assertNotIn(
            "sha=abc1234",
            out.getvalue(),
            "绑定行走 stderr 是为了不污染可机读的 stdout；串流即破坏该前提",
        )


class TestNoSecondSourceOfTruth(unittest.TestCase):
    """结构判据：`serving/api.py` 不得自带 HEAD 解析或快照目录拼接。"""

    def test_api_imports_identity_helpers(self) -> None:
        text = (REPO / "serving" / "api.py").read_text(encoding="utf-8")
        self.assertIn("resolve_runtime_snapshot", text, "/health 必须走解析函数")
        self.assertNotIn(
            _REV_PARSE, text, "api.py 出现第二条 HEAD 解析路径（决策 ② 的副本归零纪律）"
        )
        self.assertNotIn(
            "SNAPSHOT_DIR /",
            text,
            "api.py 不得自己拼快照路径：那等于绕过 `_load_meta` 的两处身份核对",
        )

    def test_head_sha_comes_from_identity(self) -> None:
        text = (REPO / "serving" / "api.py").read_text(encoding="utf-8")
        self.assertRegex(
            text, r"from data\.identity import[\s\S]{0,200}?git_short_sha", "身份单一事实源"
        )
        self.assertNotRegex(
            text, r"from eval\.runner import[^\n]*git_short_sha", "旧 re-export 路径不得复用"
        )


class TestComposeProbeStillParsesHealthcheck(unittest.TestCase):
    """healthcheck 只探 URL：扩键不影响它，但键集变化必须仍返回 8 键。"""

    def test_probe_url_only(self) -> None:
        text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("/health", text)
        self.assertNotIn("snapshot_sha", text, "compose 探针不得开始解析 body（决策 ⑥ 的兼容前提）")


class TestBootIdShapeStandalone(unittest.TestCase):
    """不依赖 HTTP：BOOT_ID 必须与 uuid4 形态一致且不是从 env 读的（env 可伪造身份）。"""

    def test_not_read_from_env(self) -> None:
        text = (REPO / "serving" / "api.py").read_text(encoding="utf-8")
        match = re.search(r"^BOOT_ID\s*=\s*(.+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "BOOT_ID 必须是模块级常量")
        self.assertNotIn("environ", match.group(1) if match else "")


if __name__ == "__main__":
    unittest.main()
