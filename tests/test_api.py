"""HTTP API 契约测试（ADR-0012 / URL 契约 v2 见 ADR-0022，serving/api.py）：fake agent_factory 注入，无 DB。

口径（与 tests/test_graph.py 同源注入模式）：
- 执行器/预算 fake 注入，不碰网络与 Doris；Agent 真实确定性链路（engine=stub）；
- 认证走 serving/auth.py 同源 sign_token（模块级固定测试密钥，与 env 无关）；
- 序列化契约：rows 的 Decimal → str（保精度，不进浮点）、datetime → ISO8601；
- 单例 Agent 语义：session_id 复用 → turns_in_session 递增（轮数来自 Agent
  checkpoint 的状态字段 `turns`，ADR-0020 决策 ⑤，与 CLI 一致）；
- 路由契约 v2：业务端点一律 `API = /api/v1` 前缀（ADR-0022 决策 ② 硬切；
  旧无前缀路径 404 的断言在 tests/test_api_contract_v2.py）。

覆盖：health 字段 / plan 两 kind / compile 200+422×2 / ask answer·clarify·blocked /
session 自动生成与续接 / 401 三种形态 / 问句超长 422 / 快照缺失 503。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from agent.compiler import SemanticModel
from agent.factory import SnapshotUnavailable
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from serving.api import create_app
from serving.audit import AuditLog
from serving.auth import sign_token

REPO = Path(__file__).resolve().parent.parent

# 锁定快照表白名单（与 tests/test_graph.py 同口径：只能触碰已锁快照的表）
_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
# 7d48dcb 是 TPC-DI 单源历史快照（无零售表）；P7 多模型路由测试需要零售域
# SQL 过 Guard（白名单语义不变，仅补零售 4 物理表模拟双源快照，与 1e5d35b
# 一致：零售时间维物理表名 date_dim——语义名 dim_date，见 gold-051 SQL）
ALLOWED |= frozenset(
    {"atlas.dwd.store_sales", "atlas.dwd.date_dim", "atlas.dwd.dim_item", "atlas.dwd.dim_store"}
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
MODEL = SemanticModel()
RETAIL_MODEL = SemanticModel(REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml")

SECRET = "api-contract-test-secret"

# 契约 v2 前缀（ADR-0022 决策 ②硬切）：业务端点一律带前缀，根 /health 双挂保留
API = "/api/v1"

GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"
AMBIGUOUS_Q = "最近交易情况怎么样？"  # gold-104：相对时间 → 反问
RETAIL_Q = "2000 年总销售额是多少？"  # gold-051：零售 year 聚合

# gold-102 的 Plan JSON（/plan 响应的同构输入，见 agent/cli.py docstring）
PLAN_102 = {
    "metric": "commission_revenue",
    "dimensions": ["Branch"],
    "time": {"granularity": "year", "value": 2013},
    "order_by": [{"column": "commission_revenue", "desc": True}],
    "limit": 5,
}
# 零售 year 聚合 Plan JSON（compile model=retail 输入）
PLAN_RETAIL = {
    "metric": "total_sales_price",
    "time": {"granularity": "year", "value": 2000},
}


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集（同 tests/test_graph.py）。"""

    def __init__(
        self,
        rows: list[tuple[object, ...]] = (("v",),),
        columns: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.rows = rows
        self.columns = list(columns or ["v"])

    def __call__(self, sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], list(self.columns)


class ApiContractTest(unittest.TestCase):
    """共享注入面：真实确定性 Agent + fake 执行器（每例独立，防会话计数串扰）。

    agent_factory 按域返回：finance=默认 self.agent、retail=self.retail_agent
    （P7 多模型路由注入面）；各域 fake 执行器独立记录，路由断言靠 SQL 落盘区分。
    """

    def setUp(self) -> None:
        # 记住外层环境原值，tearDown 按原状态恢复（不污染同进程后续测试，如 demo RLS）
        self._secret_was_set = "ATLAS_JWT_SECRET" in os.environ
        self._secret_orig = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self.executor = FakeExecutor()
        self.retail_executor = FakeExecutor()
        self.agent = DataAgent(executor=self.executor, budget=BUDGET)
        self.retail_agent = DataAgent(
            model=RETAIL_MODEL, executor=self.retail_executor, budget=BUDGET
        )
        # 审计注入 tmp 目录（C2 起每业务请求一行；测试不碰仓库 serving/audit/）
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-audit-")
        self.audit = AuditLog(Path(self._audit_tmp.name))

        def factory(model_name: str) -> DataAgent:
            return self.retail_agent if model_name == "retail" else self.agent

        self.client = TestClient(create_app(agent_factory=factory, audit=self.audit))

    def tearDown(self) -> None:
        self.client.close()
        self._audit_tmp.cleanup()
        if self._secret_was_set:
            os.environ["ATLAS_JWT_SECRET"] = self._secret_orig or ""
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)

    def _auth(self, secret: str = SECRET) -> dict[str, str]:
        return {"Authorization": f"Bearer {sign_token('hq_admin', {}, secret=secret)}"}

    # ---- /health ----

    def test_health_fields(self) -> None:
        """公开面：status=head_sha 必在；snapshot_sha 随 HEAD 快照存在性（str 或 None）。"""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIsInstance(body["head_sha"], str)
        self.assertTrue(body["head_sha"])
        self.assertTrue(body["snapshot_sha"] is None or isinstance(body["snapshot_sha"], str))

    # ---- /plan ----

    def test_plan_kind_plan_metric(self) -> None:
        """有效 token 200：gold-102 命中 → kind=plan 且 metric/time/order 正确。"""
        resp = self.client.post(f"{API}/plan", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "plan")
        self.assertIsNone(body["clarification"])
        plan = body["plan"]
        self.assertEqual(plan["metric"], "commission_revenue")
        self.assertEqual(plan["dimensions"], ["Branch"])
        self.assertEqual(plan["time"], {"granularity": "year", "value": 2013})
        self.assertEqual(plan["order_by"], [{"column": "commission_revenue", "desc": True}])
        self.assertEqual(plan["limit"], 5)

    def test_plan_ambiguous_kind_clarify(self) -> None:
        """歧义问句：CLI exit 1 语义 HTTP 化为 200 + kind=clarify（含 reasons，不猜）。"""
        resp = self.client.post(f"{API}/plan", json={"question": AMBIGUOUS_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "clarify")
        self.assertIsNone(body["plan"])
        clarification = body["clarification"]
        self.assertIsInstance(clarification["reasons"], list)
        self.assertTrue(clarification["reasons"])
        self.assertIn("question", clarification)

    # ---- /compile ----

    def test_compile_valid_sql_has_limit(self) -> None:
        """合法 Plan → 只读 SQL（含 LIMIT 强制上限形态）。"""
        resp = self.client.post(f"{API}/compile", json=PLAN_102, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        sql = resp.json()["sql"]
        self.assertIsInstance(sql, str)
        self.assertIn("LIMIT", sql.upper())
        self.assertNotIn("INSERT", sql.upper())

    def test_compile_invalid_structure_422(self) -> None:
        """结构非法（缺 metric）→ 422（pydantic 层，不落编译器）。"""
        resp = self.client.post(f"{API}/compile", json={"dimensions": ["Branch"]}, headers=self._auth())
        self.assertEqual(resp.status_code, 422)

    def test_compile_unknown_metric_422(self) -> None:
        """结构合法但指标不在语义层 → 编译期错误也 422（与非法结构同语义）。"""
        bad = dict(PLAN_102, metric="no_such_metric")
        resp = self.client.post(f"{API}/compile", json=bad, headers=self._auth())
        self.assertEqual(resp.status_code, 422)
        self.assertIn("编译失败", resp.json()["detail"])

    # ---- /ask ----

    def test_ask_answer_rows_decimal_string(self) -> None:
        """answer 序列化契约：Decimal → str（保精度，不进浮点）；datetime → ISO8601。"""
        self.executor.rows = [
            (Decimal("1234567890.12"), date(2013, 12, 31)),
            (Decimal("0.10"), datetime(2014, 1, 1, 8, 30)),
        ]
        self.executor.columns = ["commission_revenue", "day"]
        resp = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer")
        self.assertEqual(body["metric"], "commission_revenue")
        self.assertIsInstance(body["sql"], str)
        self.assertEqual(body["row_count"], 2)
        self.assertEqual(body["rows"][0], ["1234567890.12", "2013-12-31"])
        self.assertEqual(body["rows"][1][0], "0.10")
        self.assertEqual(body["rows"][1][1], "2014-01-01T08:30:00")
        self.assertIsNone(body["clarification"])
        self.assertIsNone(body["block_reason"])

    def test_ask_clarify_no_execution(self) -> None:
        """歧义问句经状态机 → kind=clarify；SQL 不达执行器（不猜答）。"""
        resp = self.client.post(f"{API}/ask", json={"question": AMBIGUOUS_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "clarify")
        self.assertIsNotNone(body["clarification"])
        self.assertTrue(body["clarification"]["reasons"])
        self.assertEqual(self.executor.calls, [])

    def test_ask_blocked_guard_no_execution(self) -> None:
        """Guard 拒绝语义经 HTTP 透传：kind=blocked + block_reason，SQL 不执行。"""
        deny_budget = Budget(
            dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dwd.other"})
        )
        agent = DataAgent(executor=self.executor, budget=deny_budget)
        client = TestClient(create_app(agent_factory=lambda _domain: agent, audit=self.audit))
        try:
            resp = client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        finally:
            client.close()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "blocked")
        self.assertIn("UnsafeQuery", body["block_reason"] or "")
        self.assertEqual(self.executor.calls, [], "被 Guard 拒绝的 SQL 不得执行")

    def test_ask_session_id_autogenerated(self) -> None:
        """不带 session_id：自动生成会话键且每次不同（单轮语义）。"""
        r1 = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        r2 = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        sid1 = r1.json()["session_id"]
        sid2 = r2.json()["session_id"]
        self.assertTrue(sid1)
        self.assertTrue(sid2)
        self.assertNotEqual(sid1, sid2)

    def test_ask_multi_turn_session_continues(self) -> None:
        """同 session_id 多轮：turns_in_session 递增（轮数在 Agent checkpoint 里，与 CLI 一致）。

        会话绑定首个 claims 指纹（C2 硬化；指纹同样存 checkpoint，ADR-0020 决策 ⑥）：
        多轮请求必须复用同一 token（换 token = 新身份 = 需新 session_id，见 422 冲突测试）。
        """
        sid = "api-sess-1"
        headers = self._auth()  # 同一 bearer 复用于同会话（指纹稳定）
        r1 = self.client.post(
            f"{API}/ask", json={"question": AMBIGUOUS_Q, "session_id": sid}, headers=headers
        )
        r2 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=headers
        )
        self.assertEqual(r1.json()["kind"], "clarify")
        self.assertEqual(r1.json()["turns_in_session"], 1)
        self.assertEqual(r2.json()["kind"], "answer")
        self.assertEqual(r2.json()["session_id"], sid)
        self.assertEqual(r2.json()["turns_in_session"], 2)

    def test_ask_question_too_long_422(self) -> None:
        """问句超 500 字符 → 422（HTTP 面粗限；Guard 细限不变，不落图）。"""
        resp = self.client.post(f"{API}/ask", json={"question": "问" * 501}, headers=self._auth())
        self.assertEqual(resp.status_code, 422)

    # ---- 认证（401 三种形态 + 有效 token）----

    def test_401_missing_token(self) -> None:
        """无 Authorization 头 → 401。"""
        resp = self.client.post(f"{API}/plan", json={"question": GOLD102_Q})
        self.assertEqual(resp.status_code, 401)

    def test_401_malformed_token(self) -> None:
        """坏 token（非 JWT 形态）→ 401。"""
        resp = self.client.post(
            f"{API}/plan",
            json={"question": GOLD102_Q},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_401_forged_signature(self) -> None:
        """伪造签名（错误密钥签发）→ 401。"""
        forged = sign_token("hq_admin", {}, secret="wrong-secret")
        resp = self.client.post(
            f"{API}/plan",
            json={"question": GOLD102_Q},
            headers={"Authorization": f"Bearer {forged}"},
        )
        self.assertEqual(resp.status_code, 401)

    # ---- 快照缺失（503）----

    def test_ask_snapshot_unavailable_503(self) -> None:
        """agent_factory 抛 SnapshotUnavailable → 503（CLI SystemExit 语义的 HTTP 化）。"""

        def raiser(_domain: str) -> DataAgent:
            raise SnapshotUnavailable("当前 HEAD 无锁定快照 meta（测试）")

        client = TestClient(create_app(agent_factory=raiser, audit=self.audit))
        try:
            resp = client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        finally:
            client.close()
        self.assertEqual(resp.status_code, 503)
        # 前缀可以改（ADR-0019 决策 ⑥ 的文案残留：原「无法绑定评测数据」会把人引到
        # 评测现场），但原始异常消息必须原样带出，否则用户无从定位——键集与文案细则
        # 由 tests/test_identity_echo.py::Test503Wording 锁住
        self.assertIn("当前 HEAD 无锁定快照 meta（测试）", resp.json()["detail"])

    # ---- 多模型路由（P7，2026-09-05）----

    def test_plan_model_retail_chinese(self) -> None:
        """model=retail：中文零售问句命中零售指标（gold-051 问句形态）。"""
        resp = self.client.post(
            f"{API}/plan", json={"question": RETAIL_Q, "model": "retail"}, headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "plan")
        plan = body["plan"]
        self.assertEqual(plan["metric"], "total_sales_price")
        self.assertEqual(plan["time"], {"granularity": "year", "value": 2000})

    def test_plan_model_retail_english_auto_locale(self) -> None:
        """model=retail + 英文问句：语言自动检测（P6 locale 化，无需显式参数）。"""
        resp = self.client.post(
            f"{API}/plan",
            json={"question": "What were total sales in 1999?", "model": "retail"},
            headers=self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "plan")
        self.assertEqual(body["plan"]["metric"], "total_sales_price")
        self.assertEqual(body["plan"]["time"], {"granularity": "year", "value": 1999})

    def test_plan_default_finance_isolates_domains(self) -> None:
        """域隔离：零售问句在缺省 finance 模型 → clarify（不跨域猜测）。"""
        resp = self.client.post(f"{API}/plan", json={"question": RETAIL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "clarify")
        self.assertIsNone(body["plan"])

    def test_plan_model_retail_question_mismatch_clarifies(self) -> None:
        """反向隔离：金融问句在 retail 模型 → clarify。"""
        resp = self.client.post(
            f"{API}/plan", json={"question": GOLD102_Q, "model": "retail"}, headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "clarify")

    def test_plan_unknown_model_422(self) -> None:
        """model 不在白名单 → 422（不落到语义层）。"""
        resp = self.client.post(
            f"{API}/plan",
            json={"question": GOLD102_Q, "model": "no_such_domain"},
            headers=self._auth(),
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("未知模型", resp.json()["detail"])

    def test_compile_model_retail_sql_references_retail_tables(self) -> None:
        """model=retail：Plan JSON → 零售事实表 SQL（store_sales/dim_date）。"""
        resp = self.client.post(
            f"{API}/compile", json=dict(PLAN_RETAIL, model="retail"), headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        sql = resp.json()["sql"]
        self.assertIn("store_sales", sql)
        self.assertIn("dim_date", sql)

    def test_compile_retail_metric_on_finance_422(self) -> None:
        """零售指标 Plan 落在缺省 finance 模型 → 编译失败 422（路由先于编译）。"""
        resp = self.client.post(f"{API}/compile", json=dict(PLAN_RETAIL), headers=self._auth())
        self.assertEqual(resp.status_code, 422)
        self.assertIn("编译失败", resp.json()["detail"])

    def test_ask_model_retail_routes_to_retail_agent(self) -> None:
        """/ask model=retail → 零售 Agent 单例：SQL 落零售 executor，金融 executor 零调用。"""
        resp = self.client.post(
            f"{API}/ask", json={"question": RETAIL_Q, "model": "retail"}, headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer")
        self.assertEqual(body["metric"], "total_sales_price")
        self.assertTrue(self.retail_executor.calls, "零售问句必须到达零售 Agent 的执行器")
        self.assertIn("store_sales", self.retail_executor.calls[0])
        self.assertEqual(self.executor.calls, [], "零售问句不得落到金融 Agent")

    def test_ask_model_finance_default_backward_compat(self) -> None:
        """/ask 缺省（无 model）= finance：既有行为零变化（显式 finance 同路径）。"""
        for payload in ({"question": GOLD102_Q}, {"question": GOLD102_Q, "model": "finance"}):
            with self.subTest(model=payload.get("model", "<default>")):
                self.executor.calls.clear()
                resp = self.client.post(f"{API}/ask", json=payload, headers=self._auth())
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["kind"], "answer")
                self.assertTrue(self.executor.calls)
                self.assertEqual(self.retail_executor.calls, [])

    def test_ask_session_scoped_per_model(self) -> None:
        """session_id 按模型隔离：同键 finance/retail 各自独立会话计数。

        每模型会话绑定各自首个 token 指纹（fingerprint 存各域 Agent 的 checkpoint，
        thread_id 带模型前缀——ADR-0020 决策 ④⑥，跨模型不串指纹）。
        """
        sid = "model-scoped-sess"
        finance_headers = self._auth()  # finance 会话两轮复用同一 token
        r1 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=finance_headers
        )
        r2 = self.client.post(
            f"{API}/ask",
            json={"question": RETAIL_Q, "session_id": sid, "model": "retail"},
            headers=self._auth(),
        )
        self.assertEqual(r1.json()["turns_in_session"], 1)
        self.assertEqual(r2.json()["turns_in_session"], 1)  # retail 会话首轮
        r3 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=finance_headers
        )
        self.assertEqual(r3.json()["turns_in_session"], 2)  # finance 会话续接


if __name__ == "__main__":
    unittest.main()
