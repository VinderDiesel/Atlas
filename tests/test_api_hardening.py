"""HTTP 服务面硬化契约测试（C2：身份下推 + 会话指纹 + 审计 + 限流；URL 契约 v2 见 ADR-0022）。

口径（serving/api.py 同源注入模式，仿 tests/test_api.py）：
- fake agent_factory + fake 执行器（SQL 落盘断言 = 身份注入面）；
- 审计 AuditLog 注入 tmp 目录（不碰仓库 serving/audit/）；限流 RateLimiter
  注入小窗口（不碰真实 .env）；JWT 用模块级固定测试密钥。
- 会话指纹绑定语义：多轮/续接请求必须复用同一 token（换 token = 新身份）；
  指纹自 ADR-0020 决策 ⑥ 起存在 **Agent 的 checkpoint** 里（不再是 api 的进程内
  字典），故 fake 工厂必须**按域各建一个 Agent**——真实 `_live_agent` 也是按域各一，
  共用一个实例会让两个域撞在同一张图里。
- 401 在 require_bearer 先拒（不限流不审计）；/health 公开（不限流不审计）。
- 契约 v2（ADR-0022）：业务端点带 `/api/v1` 前缀；审计行带 bucket 字段
  （业务面 "business"；治理面 "governance" 见 tests/test_api_contract_v2.py）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent.compiler import SemanticModel
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from serving.api import create_app
from serving.audit import AUDIT_FILENAME, AuditLog
from serving.auth import sign_token
from serving.ratelimit import RateLimiter

REPO = Path(__file__).resolve().parent.parent

# 契约 v2 前缀（ADR-0022 决策 ②硬切）：业务端点一律带前缀
API = "/api/v1"

# 锁定快照表白名单（与 tests/test_api.py 同口径；P7 起补零售 4 表模拟双源）
_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
ALLOWED |= frozenset(
    {"atlas.dwd.store_sales", "atlas.dwd.date_dim", "atlas.dwd.dim_item", "atlas.dwd.dim_store"}
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
MODEL = SemanticModel()

SECRET = "api-harden-test-secret"
GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"
AMBIGUOUS_Q = "最近交易情况怎么样？"  # gold-104：相对时间 → 反问
# 与 serving/rls_verify.py RETAIL_QUESTION / api_acceptance A7 同串（载体同源），
# 防问句漂移——hardening 不 import rls_verify（避免 eval.runner 重链）
RETAIL_Q = "2000 年按门店城市和品类统计销售额，列出前 3 名"


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集（同 tests/test_api.py）。"""

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


def _token(role: str, context: dict | None = None, *, subject: str = "harden-user") -> str:
    return sign_token(role, context or {}, secret=SECRET, subject=subject)


class ApiHardeningTest(unittest.TestCase):
    """共享注入面：审计 tmp 目录 + 宽限流（限流/429 用例单独建小窗口 client）。"""

    def setUp(self) -> None:
        self._secret_was_set = "ATLAS_JWT_SECRET" in os.environ
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self.executor = FakeExecutor()
        self._agents: dict[str, DataAgent] = {}

        def factory(model_name: str) -> DataAgent:
            # 按域各建一个（真实 _live_agent 同形）：轮数与身份指纹都存进各自图的
            # checkpoint（ADR-0020 决策 ⑤⑥），共用实例会让 finance 的绑定误判 retail
            if model_name not in self._agents:
                self._agents[model_name] = DataAgent(executor=self.executor, budget=BUDGET)
            return self._agents[model_name]

        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-audit-")
        self.audit = AuditLog(Path(self._audit_tmp.name))
        self.client = TestClient(
            create_app(
                agent_factory=factory,
                audit=self.audit,
                rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self._audit_tmp.cleanup()
        if self._secret_was_set:
            os.environ["ATLAS_JWT_SECRET"] = SECRET
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)

    def _auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or _token('hq_admin')}"}

    def _lines(self) -> list[dict]:
        path = Path(self._audit_tmp.name) / AUDIT_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    # ---- ① 身份下推：claims → Guard Policy 注入（fake 注入面 = SQL 落盘） ----

    def test_ask_branch_manager_injects_predicate_and_visibility(self) -> None:
        """branch_manager{east} token /ask → 执行 SQL 含分支谓词 + 生效句可见。"""
        resp = self.client.post(
            f"{API}/ask",
            json={"question": GOLD102_Q},
            headers=self._auth(_token("branch_manager", {"branch": "east"})),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer")
        self.assertIn("= 'east'", self.executor.calls[0])  # 行级谓词已注入执行 SQL
        effect = str(body["explanation"]["policy_effect"])
        self.assertIn("行级策略已生效", effect)
        self.assertIn("branch_manager", effect)
        self.assertIn("rp_branch_visible", effect)
        self.assertNotIn("east", effect)  # 条件值不外泄（0011 口径）

    def test_ask_hq_admin_injects_1eq1(self) -> None:
        """hq_admin token /ask → 谓词 1=1（无行过滤语义，enforce 如实注入）。"""
        resp = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "answer")
        self.assertIn("1 = 1", self.executor.calls[0])

    def test_ask_cross_domain_identity_rejected_no_execution(self) -> None:
        """region_manager（零售策略角色）× finance model → kind=error，零 SQL（判据 12）。

        旧行为是 Guard 兜底拒绝（blocked，谓词列无 join 路径）；ADR-0021 起身份层
        按域拒绝（域不匹配），先于 Guard——不执行任何 SQL，审计行 kind 同步 error。
        """
        resp = self.client.post(
            f"{API}/ask",
            json={"question": GOLD102_Q},
            headers=self._auth(_token("region_manager", {"region": "TN"})),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "error")
        self.assertIn("域不匹配", str(body["error"]))
        self.assertEqual(self.executor.calls, [])  # Guard 之前即拒：零 SQL 达执行器
        row = self._lines()[0]  # 审计行与回合 kind 同步
        self.assertEqual(row["endpoint"], f"{API}/ask")
        self.assertEqual(row["kind"], "error")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["claims"]["role"], "region_manager")

    def test_ask_retail_hq_admin_policy_name_follows_domain(self) -> None:
        """零售域 hq_admin /ask → 策略名报 rp_dept_visible（判据 11，跨域误报回归锁）。

        旧 ROLE_DIRECTORY 把 hq_admin 的策略名写死 rp_branch_visible：零售域谓词
        1=1 无过滤故结果对、归因错；ADR-0021 起策略名按模型 default_row_policy
        解析（域 → 策略），审计行 kind 同步 answer。
        """
        retail = SemanticModel(REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml")
        executor = FakeExecutor()
        client = TestClient(
            create_app(
                agent_factory=lambda _m: DataAgent(model=retail, executor=executor, budget=BUDGET),
                audit=self.audit,
                rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            )
        )
        try:
            resp = client.post(
                f"{API}/ask",
                json={"question": RETAIL_Q, "model": "retail"},
                headers=self._auth(),
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["kind"], "answer")
            effect = str(body["explanation"]["policy_effect"])
            self.assertIn("hq_admin", effect)
            self.assertIn("rp_dept_visible", effect)
            self.assertNotIn("rp_branch_visible", effect)  # 跨域策略名误报回归锁
            row = self._lines()[0]
            self.assertEqual(row["kind"], "answer")
            self.assertEqual(row["claims"]["role"], "hq_admin")
            self.assertEqual(row["status"], 200)
        finally:
            client.close()

    # ---- ② 会话身份指纹：同 session 换身份 → 422 ----

    def test_ask_session_identity_conflict_422(self) -> None:
        """同 session_id 换 token（branch_manager → hq_admin）→ 422 冲突。"""
        sid = "harden-sess-1"
        headers = self._auth(_token("branch_manager", {"branch": "east"}))
        r1 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=headers
        )
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=self._auth()
        )
        self.assertEqual(r2.status_code, 422)
        self.assertIn("会话身份冲突", r2.json()["detail"])

    def test_ask_same_token_session_continues(self) -> None:
        """同 session 同 token（同身份）→ 多轮续接，无假冲突。"""
        sid = "harden-sess-2"
        headers = self._auth(_token("branch_manager", {"branch": "east"}))
        for _ in range(2):
            resp = self.client.post(
                f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=headers
            )
            self.assertEqual(resp.status_code, 200)
        # 同身份不产生冲突审计行
        self.assertEqual([l for l in self._lines() if l["kind"] == "conflict"], [])

    def test_ask_session_conflict_scoped_per_model(self) -> None:
        """指纹键含 model：同 sid 跨模型不串指纹（retail 会话可换新 token 首启）。"""
        sid = "harden-sess-3"
        branch = self._auth(_token("branch_manager", {"branch": "east"}))
        r1 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=branch
        )
        self.assertEqual(r1.status_code, 200)
        # finance 会话已绑 branch 指纹 → 换 hq token 冲突
        r2 = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=self._auth()
        )
        self.assertEqual(r2.status_code, 422)
        # retail 同 sid 落在另一个 Agent（按域各一）的独立线程上：hq token 首启不冲突
        # （指纹进 checkpoint 后，跨域隔离靠 thread_id 的模型前缀，见决策 ④）
        r3 = self.client.post(
            f"{API}/ask",
            json={"question": GOLD102_Q, "session_id": sid, "model": "retail"},
            headers=self._auth(),
        )
        self.assertEqual(r3.status_code, 200)
        # 但 retail 会话随后换 token 同样冲突
        r4 = self.client.post(
            f"{API}/ask",
            json={"question": GOLD102_Q, "session_id": sid, "model": "retail"},
            headers=self._auth(_token("branch_manager", {"branch": "west"})),
        )
        self.assertEqual(r4.status_code, 422)

    def test_ask_without_session_id_no_fingerprint(self) -> None:
        """不带 session_id（单轮自动会话）：换 token 无冲突（无绑定可冲突）。"""
        for _ in range(2):
            resp = self.client.post(
                f"{API}/ask",
                json={"question": GOLD102_Q},
                headers=self._auth(_token("branch_manager", {"branch": "east"})),
            )
            self.assertEqual(resp.status_code, 200)

    # ---- ③ 限流：per-token 共享桶 → 429 + Retry-After ----

    def _small_client(self, max_requests: int) -> tuple[TestClient, AuditLog, tempfile.TemporaryDirectory]:
        """小窗口限流 client + 独立 tmp 审计目录（调用方负责 cleanup）。"""
        tmp = tempfile.TemporaryDirectory(prefix="atlas-rl-")
        audit = AuditLog(Path(tmp.name))
        client = TestClient(
            create_app(
                agent_factory=lambda _domain: DataAgent(executor=self.executor, budget=BUDGET),
                audit=audit,
                rate_limiter=RateLimiter(max_requests=max_requests, window_seconds=60),
            )
        )
        return client, audit, tmp

    def test_rate_limit_429_with_retry_after(self) -> None:
        """per-token 超限：第 N+1 请求 429 + Retry-After 秒数（HTTP 标准头）。"""
        client, audit, tmp = self._small_client(max_requests=2)
        try:
            headers = self._auth()
            for _ in range(2):
                resp = client.post(f"{API}/plan", json={"question": GOLD102_Q}, headers=headers)
                self.assertEqual(resp.status_code, 200)
            resp = client.post(f"{API}/plan", json={"question": GOLD102_Q}, headers=headers)
            self.assertEqual(resp.status_code, 429)
            self.assertIn("Retry-After", resp.headers)
            self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)
            self.assertIn("过于频繁", resp.json()["detail"])
            # 429 命中也是审计事件（kind=rate_limited）
            path = Path(tmp.name) / AUDIT_FILENAME
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            rl = [r for r in rows if r["kind"] == "rate_limited"]
            self.assertEqual(len(rl), 1)
            self.assertEqual(rl[0]["status"], 429)
            self.assertEqual(rl[0]["endpoint"], f"{API}/plan")
            self.assertEqual(rl[0]["claims"]["role"], "hq_admin")
        finally:
            client.close()
            tmp.cleanup()

    def test_rate_limit_shared_bucket_across_endpoints(self) -> None:
        """全业务端点共享桶：/plan /compile /ask 同 token 计数不分开。"""
        client, _audit, tmp = self._small_client(max_requests=3)
        try:
            headers = self._auth()
            hits = [
                client.post(f"{API}/plan", json={"question": GOLD102_Q}, headers=headers),
                client.post(
                    f"{API}/compile",
                    json={
                        "metric": "commission_revenue",
                        "dimensions": ["Branch"],
                        "time": {"granularity": "year", "value": 2013},
                        "limit": 5,
                    },
                    headers=headers,
                ),
                client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=headers),
            ]
            for resp in hits:
                self.assertEqual(resp.status_code, 200, resp.text[:120])
            fourth = client.post(f"{API}/plan", json={"question": GOLD102_Q}, headers=headers)
            self.assertEqual(fourth.status_code, 429)
        finally:
            client.close()
            tmp.cleanup()

    def test_rate_limit_per_token_independent(self) -> None:
        """per-token 单维：token A 超限不影响 token B（sub 不同）。"""
        client, _audit, tmp = self._small_client(max_requests=1)
        try:
            r1 = client.post(
                f"{API}/plan",
                json={"question": GOLD102_Q},
                headers=self._auth(_token("hq_admin", subject="user-a")),
            )
            r2 = client.post(
                f"{API}/plan",
                json={"question": GOLD102_Q},
                headers=self._auth(_token("hq_admin", subject="user-b")),
            )
            r3 = client.post(
                f"{API}/plan",
                json={"question": GOLD102_Q},
                headers=self._auth(_token("hq_admin", subject="user-a")),
            )
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r3.status_code, 429)
        finally:
            client.close()
            tmp.cleanup()

    def test_rate_limit_health_and_401_exempt(self) -> None:
        """/health（公开）与 401（无效 token）不进限流计数。"""
        client, _audit, tmp = self._small_client(max_requests=1)
        try:
            # 两个 /health + 一个 401（无/坏 token）都不消耗配额
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/health").status_code, 200)
            resp = client.post(f"{API}/plan", json={"question": GOLD102_Q})
            self.assertEqual(resp.status_code, 401)
            # 配额仍可用 → 有效 token 请求放行（若 401 被计数则应 429）
            resp = client.post(
                f"{API}/plan", json={"question": GOLD102_Q}, headers=self._auth()
            )
            self.assertEqual(resp.status_code, 200)
        finally:
            client.close()
            tmp.cleanup()

    def test_rate_limiter_window_reset(self) -> None:
        """固定窗口过期自动重置：注时推进一窗口 → 计数清零（类级单测）。"""
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        base = 1_700_000_000.0
        self.assertTrue(limiter.check("u", now=base)[0])
        self.assertFalse(limiter.check("u", now=base + 1)[0])
        # 跨窗口（now 进入下一窗口起点）→ 重置放行
        self.assertTrue(limiter.check("u", now=base + 60)[0])
        self.assertFalse(limiter.check("u", now=base + 61)[0])

    def test_rate_limiter_disabled_zero_max(self) -> None:
        """max=0 → 关闭（恒放行，不计数）。"""
        limiter = RateLimiter(max_requests=0, window_seconds=60)
        for _ in range(3):
            self.assertTrue(limiter.check("u")[0])

    # ---- ④ 审计：JSONL 每业务请求一行（tmp 目录 + 字段集断言） ----

    def test_audit_row_fieldset_stable(self) -> None:
        """审计行字段全集恒定（9 键含 bucket + claims{role,sub}）；不含 SQL 与条件值。"""
        self.client.post(f"{API}/plan", json={"question": GOLD102_Q}, headers=self._auth())
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            set(row),
            {
                "ts",
                "claims",
                "endpoint",
                "session_id",
                "kind",
                "row_count",
                "latency_ms",
                "status",
                "bucket",
            },
        )
        self.assertEqual(set(row["claims"]), {"role", "sub"})
        self.assertNotIn("sql", row)  # SQL 细节由 OTel span 承担，不入审计
        self.assertNotIn("user_context", row["claims"])  # 条件值不外泄
        self.assertEqual(row["endpoint"], f"{API}/plan")
        self.assertEqual(row["kind"], "plan")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["bucket"], "business")  # 契约 v2 决策 ⑥：行标桶名
        self.assertEqual(row["claims"]["role"], "hq_admin")

    def test_audit_plan_clarify_and_compile_kinds(self) -> None:
        """/plan 歧义 /compile 成功·失败 → 各自 kind 与 status 如实落行。"""
        self.client.post(f"{API}/plan", json={"question": AMBIGUOUS_Q}, headers=self._auth())
        self.client.post(
            f"{API}/compile",
            json={
                "metric": "commission_revenue",
                "dimensions": ["Branch"],
                "time": {"granularity": "year", "value": 2013},
                "limit": 5,
            },
            headers=self._auth(),
        )
        self.client.post(
            f"{API}/compile",
            json={"metric": "no_such_metric"},
            headers=self._auth(),
        )
        rows = self._lines()
        kinds = {r["kind"]: r for r in rows}
        self.assertEqual(kinds["clarify"]["endpoint"], f"{API}/plan")
        self.assertEqual(kinds["clarify"]["status"], 200)
        self.assertEqual(kinds["compiled"]["status"], 200)
        self.assertEqual(kinds["compile_error"]["endpoint"], f"{API}/compile")
        self.assertEqual(kinds["compile_error"]["status"], 422)

    def test_audit_ask_answer_row_fields(self) -> None:
        """/ask answer 行：kind/row_count/latency_ms/session_id 语义如实。"""
        sid = "audit-sess-1"
        resp = self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        row = self._lines()[0]
        self.assertEqual(row["kind"], "answer")
        self.assertEqual(row["session_id"], sid)
        self.assertEqual(row["row_count"], 1)  # fake 返回 1 行
        self.assertIsInstance(row["latency_ms"], (int, float))
        self.assertEqual(row["status"], 200)

    def test_audit_conflict_row_recorded(self) -> None:
        """422 会话身份冲突也落审计行（kind=conflict，status=422）。"""
        sid = "audit-sess-2"
        headers_a = self._auth(_token("branch_manager", {"branch": "east"}))
        self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=headers_a
        )
        self.client.post(
            f"{API}/ask", json={"question": GOLD102_Q, "session_id": sid}, headers=self._auth()
        )
        conflicts = [r for r in self._lines() if r["kind"] == "conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["status"], 422)
        self.assertEqual(conflicts[0]["session_id"], sid)
        self.assertEqual(conflicts[0]["claims"]["role"], "hq_admin")

    def test_audit_disabled_writes_nothing(self) -> None:
        """AuditLog(enabled=False)（ATLAS_AUDIT_DISABLED 语义）：不写文件。"""
        tmp = tempfile.TemporaryDirectory(prefix="atlas-audit-off-")
        try:
            audit = AuditLog(Path(tmp.name), enabled=False)
            audit.record(endpoint=f"{API}/plan", claims={"role": "hq_admin", "sub": "u"})
            self.assertFalse((Path(tmp.name) / AUDIT_FILENAME).exists())
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
