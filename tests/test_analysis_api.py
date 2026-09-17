#!/usr/bin/env python3
r"""HTTP /api/v1/analyze 契约测试（ADR-0026 T08，决策⑥）。

覆盖 task-8-brief 检查单：
- 安全/校验面：无令牌 401、非法体 422、未知域 422、身份冲突 422（conflict 审计行）、
  业务桶限流 429（治理桶不挤占）、快照缺失 503（治理面隔离 200）；
- 终态五形态：ok（17 键固定投影 + Decimal→str + 父轮不冒充单 SQL 结果 + 一次审计）、
  clarify 与无意图 fallback（analysis 恒 null、键恒在、澄清零 SQL、reasons 为 list）、
  blocked（被拒 SQL 不出网、零执行器调用、失败步安全摘要）、
  error（底层异常文本不出网、对外 error 固定安全文案）、
  unavailable（前置资格门 ⇔ 零步、零 SQL）；
- R1（控制器裁定）：普通 ask / plan/execute 的执行故障轮保留现状——携带 post-Guard
  已执行 SQL 与 latency_ms（node_execute L461-465 现状），契约测试锁定防无意识变更；
  /ask 响应同样恒有 analysis 键（非分析为 null）；
- R2（控制器裁定）：分析澄清轮冲刷 analysis_record（与 node_plan 冲刷集同构）——
  先 blocked 终态（failed 记录留存）后澄清轮，checkpoint 中 analysis_record 必须为 None；
- AskBody 不得新增客户端身份字段（0011 硬化：身份只来自服务端验证的 Bearer claims）；
- 一次 HTTP 请求一条业务审计（bucket="business"）。

复用 T07 注入面（tests.test_analysis_agent），不碰网络与 Doris。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent.analysis import ANALYSIS_ROLES
from agent.factory import SnapshotUnavailable
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from serving.api import API_PREFIX, AskBody, create_app
from serving.audit import AUDIT_FILENAME, AuditLog
from serving.auth import sign_token
from serving.ratelimit import RateLimiter
from tests.test_analysis_agent import (
    CLARIFY_Q,
    DENY_BUDGET,
    ELIGIBLE,
    FULL_Q,
    NO_INTENT_Q,
    SNAPSHOT_SHA,
    AnalysisExecutor,
    ForbiddenGenerator,
    _budget,
    _meta,
    _values,
)

REPO = Path(__file__).resolve().parent.parent
API = API_PREFIX  # 唯一前缀事实源（serving/api.py 常量；本文件不重复字面量）
SECRET = "analysis-api-test-secret"

# ADR-0026 决策⑥ L228-229 的固定投影 17 键（逐字照抄；集合断言不依赖键序）
ANALYSIS_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "intent",
        "status",
        "metric",
        "dimension",
        "baseline",
        "current",
        "filters",
        "snapshot_sha",
        "semantic_sha256",
        "recipe_version",
        "totals",
        "items",
        "steps",
        "reason_code",
        "text",
        "elapsed_ms",
    }
)

# 响应字段全集：原 22 键（ADR-0022/0025）+ analysis = 23 键；analysis 键恒在
# （非分析请求恒为 null，不消失——字段全集恒定口径）
TURN_KEYS: frozenset[str] = frozenset(
    {
        "kind",
        "session_id",
        "question",
        "turns_in_session",
        "metric",
        "sql",
        "columns",
        "rows",
        "row_count",
        "latency_ms",
        "engine",
        "path",
        "usage",
        "validation_issues",
        "explanation",
        "chart",
        "clarification",
        "block_reason",
        "error",
        "handoff_reason",
        "snapshot_sha",
        "snapshot_bound_to_head",
        "analysis",
    }
)

# /analyze 分析轮（plan 在）执行故障的对外 error 固定安全文案：不透传底层异常文本
# （"RuntimeError: doris 断连" 属底层连接信息，ADR L216「不泄露……底层异常中的
# 连接信息」；机器可读原因留在 analysis.reason_code）。仅分析轮适用——无意图
# fallback 轮（plan=None）维持 /ask error 轮原文（R5，见
# TestAnalyzeFallbackAndClarify.test_fallback_error_round_mirrors_ask_error_semantics）
SANITIZED_ANALYSIS_ERROR = "分析子步骤执行失败（execution_error）"


class _BaseCase(unittest.TestCase):
    """共享注入面：ATLAS_JWT_SECRET 环境恢复 + tmp 审计目录（0022 契约测试同构）。"""

    def setUp(self) -> None:
        self._secret_was_set = "ATLAS_JWT_SECRET" in os.environ
        self._secret_orig = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-analysis-api-")
        self.audit = AuditLog(Path(self._audit_tmp.name))

    def tearDown(self) -> None:
        self._audit_tmp.cleanup()
        if self._secret_was_set:
            os.environ["ATLAS_JWT_SECRET"] = self._secret_orig or ""
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)

    def _auth(self, role: str = "hq_admin", context: dict | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {sign_token(role, context or {}, secret=SECRET)}"}

    def _rows(self) -> list[dict[str, Any]]:
        path = Path(self._audit_tmp.name) / AUDIT_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _client(
        self,
        executor: Any,
        *,
        budget: Budget | None = None,
        eligibility: dict[str, Any] | None = ELIGIBLE,
        generator: Any | None = None,
        rate_limiter: RateLimiter | None = None,
        governance_rate_limiter: RateLimiter | None = None,
    ) -> tuple[TestClient, DataAgent]:
        """单 agent 注入客户端（finance 域懒建单例；T07 同款资格证据挂载）。"""
        kwargs: dict[str, Any] = {
            "executor": executor,
            "budget": budget or _budget(),
            "snapshot_meta": _meta(),
        }
        if generator is not None:
            kwargs["generator"] = generator
        agent = DataAgent(**kwargs)
        if eligibility is not None:
            agent.analysis_eligibility = eligibility  # type: ignore[attr-defined]
        app = create_app(
            agent_factory=lambda _m: agent,
            audit=self.audit,
            rate_limiter=rate_limiter or RateLimiter(max_requests=10_000, window_seconds=60),
            governance_rate_limiter=(
                governance_rate_limiter or RateLimiter(max_requests=10_000, window_seconds=60)
            ),
        )
        client = TestClient(app)
        self.addCleanup(client.close)
        return client, agent


# ---------------------------------------------------------------------------
# 安全 / 校验面（决策⑥：复用 Bearer / AskBody / 模型选择 / 业务桶 / 422 / 503）
# ---------------------------------------------------------------------------


class TestAnalyzeSecurityAndValidation(_BaseCase):
    """无令牌 / 非法体 / 未知域 / 身份冲突 / 限流 / 缺快照 + 治理桶隔离。"""

    def test_missing_token_is_401(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor)
        resp = client.post(f"{API}/analyze", json={"question": FULL_Q})
        self.assertEqual(resp.status_code, 401, "/analyze 是业务面：必须 Bearer")
        self.assertEqual(executor.calls, [], "未认证请求不得触达执行器")

    def test_invalid_body_is_422(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor)
        resp = client.post(f"{API}/analyze", json={}, headers=self._auth())
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(executor.calls, [])

    def test_unknown_model_is_422(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor)
        resp = client.post(
            f"{API}/analyze",
            json={"question": FULL_Q, "model": "unknown"},
            headers=self._auth(),
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("未知模型", resp.json()["detail"])
        self.assertEqual(executor.calls, [])

    def test_identity_conflict_is_422_with_conflict_audit_row(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor)
        sid = "t08-conflict"
        first = client.post(
            f"{API}/analyze",
            json={"question": FULL_Q, "session_id": sid},
            headers=self._auth(),
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["kind"], "answer")
        calls_after_first = len(executor.calls)
        second = client.post(
            f"{API}/analyze",
            json={"question": FULL_Q, "session_id": sid},
            headers=self._auth("branch_manager", {"branch": "A"}),
        )
        self.assertEqual(second.status_code, 422)
        self.assertIn("会话身份冲突", second.json()["detail"])
        self.assertEqual(len(executor.calls), calls_after_first, "冲突轮不得执行 SQL")
        conflicts = [r for r in self._rows() if r["kind"] == "conflict"]
        self.assertEqual(len(conflicts), 1, f"冲突必须恰落一条 conflict 审计：{self._rows()}")
        self.assertEqual(conflicts[0]["status"], 422)
        self.assertEqual(conflicts[0]["bucket"], "business")
        self.assertEqual(conflicts[0]["endpoint"], f"{API}/analyze")

    def test_rate_limit_business_bucket_and_governance_isolation(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(
            executor,
            rate_limiter=RateLimiter(max_requests=1, window_seconds=60),
            governance_rate_limiter=RateLimiter(max_requests=10, window_seconds=60),
        )
        first = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(first.status_code, 200)
        second = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(second.status_code, 429)
        self.assertIn("业务面", second.json()["detail"])
        self.assertGreaterEqual(int(second.headers["Retry-After"]), 1)
        gov = client.get(f"{API}/governance/values", headers=self._auth())
        self.assertEqual(gov.status_code, 200, "业务 429 不得挤占治理桶（两桶独立）")
        limited = [r for r in self._rows() if r["kind"] == "rate_limited"]
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["status"], 429)
        self.assertEqual(limited[0]["bucket"], "business")

    def test_snapshot_missing_is_503_and_governance_stays_200(self) -> None:
        def factory(_model_name: str) -> DataAgent:
            raise SnapshotUnavailable("绑定快照（测试注入）缺少语义模型所需表：x")

        app = create_app(agent_factory=factory, audit=self.audit)
        client = TestClient(app)
        self.addCleanup(client.close)
        resp = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 503)
        self.assertIn("无法绑定锁定快照", resp.json()["detail"])
        gov = client.get(f"{API}/governance/values", headers=self._auth())
        self.assertEqual(gov.status_code, 200, "快照 503 不得波及治理面（决策④隔离）")

    def test_ask_body_gains_no_client_identity_field(self) -> None:
        """0011 硬化：身份只来自服务端验证的 Bearer claims，AskBody 不得新增身份字段。"""
        self.assertNotIn("identity", AskBody.model_fields)


# ---------------------------------------------------------------------------
# ok 终态：17 键固定投影 + Decimal→str + 父轮不冒充 + 一次审计 + 身份注入
# ---------------------------------------------------------------------------


class TestAnalyzeSuccess(_BaseCase):
    """成功路径的固定投影契约（ADR-0026 决策⑥）。"""

    def setUp(self) -> None:
        super().setUp()
        self.executor = AnalysisExecutor()
        self.client, self.agent = self._client(self.executor)
        resp = self.client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        self.body = resp.json()
        self.analysis = self.body["analysis"]

    def test_turn_payload_is_23_keys_with_analysis_always_present(self) -> None:
        self.assertEqual(set(self.body), TURN_KEYS, "字段全集恒定：22 键 + analysis 键恒在")

    def test_analysis_has_exact_17_key_projection(self) -> None:
        self.assertEqual(set(self.analysis), ANALYSIS_KEYS, "决策⑥ 17 键逐字照抄，不多不少")

    def test_parent_turn_does_not_impersonate_single_sql_result(self) -> None:
        body = self.body
        self.assertEqual(body["kind"], "answer")
        self.assertIsNone(body["sql"], "分析父轮 sql 恒 null（决策⑥）")
        self.assertIsNone(body["explanation"], "分析父轮 explanation 恒 null（决策⑥）")
        self.assertEqual(body["columns"], [])
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["row_count"], 0)
        self.assertEqual(body["metric"], "commission_revenue", "父轮 metric = 目标指标")
        # 父轮 latency = 已执行子 SQL 耗时之和；总耗时另放 analysis.elapsed_ms
        self.assertAlmostEqual(
            body["latency_ms"],
            sum(step["latency_ms"] for step in self.analysis["steps"]),
            places=6,
        )
        self.assertGreaterEqual(self.analysis["elapsed_ms"], body["latency_ms"])

    def test_binding_projection(self) -> None:
        self.assertEqual(self.analysis["schema_version"], 1)
        self.assertEqual(self.analysis["intent"], "change_contribution")
        self.assertEqual(self.analysis["status"], "ok")
        self.assertEqual(self.analysis["metric"], "commission_revenue")
        self.assertEqual(self.analysis["dimension"], "Branch")
        self.assertEqual(self.analysis["baseline"], {"granularity": "quarter", "value": "2013Q3"})
        self.assertEqual(self.analysis["current"], {"granularity": "quarter", "value": "2013Q4"})
        self.assertEqual(self.analysis["filters"], [])
        self.assertEqual(self.analysis["snapshot_sha"], SNAPSHOT_SHA)
        self.assertEqual(self.analysis["semantic_sha256"], self.agent.model.source_sha256)
        self.assertEqual(self.analysis["recipe_version"], 1)
        self.assertIsNone(self.analysis["reason_code"])

    def test_totals_and_items_are_decimal_as_str(self) -> None:
        self.assertEqual(
            self.analysis["totals"],
            {"baseline": "300", "current": "260", "delta": "-40"},
            "Decimal 必须以 str 保精度出网",
        )
        items = self.analysis["items"]
        self.assertEqual([i["value"] for i in items], ["A", "B"], "items 按 |delta| 降序")
        self.assertEqual(
            [set(i) for i in items],
            [{"value", "baseline", "current", "delta", "contribution_pct"}] * 2,
        )
        self.assertEqual(items[0]["baseline"], "180")
        self.assertEqual(items[0]["current"], "90")
        self.assertEqual(items[0]["delta"], "-90")
        self.assertEqual(items[1]["baseline"], "120")
        self.assertEqual(items[1]["current"], "170")
        self.assertEqual(items[1]["delta"], "50")
        for item in items:
            self.assertIsInstance(item["baseline"], str)
            self.assertIsInstance(item["current"], str)
            self.assertIsInstance(item["delta"], str)
            self.assertIsInstance(item["contribution_pct"], str)
        text = self.analysis["text"]
        self.assertIsInstance(text, str)
        self.assertIn("变化贡献分解", text)
        self.assertIn("不代表业务因果", text)

    def test_steps_carry_role_kind_sql_columns_rows_latency(self) -> None:
        steps = self.analysis["steps"]
        self.assertEqual([s["role"] for s in steps], list(ANALYSIS_ROLES), "顺序即角色序")
        self.assertEqual([s["kind"] for s in steps], ["answer"] * 4)
        self.assertEqual(
            [set(s) for s in steps],
            [{"role", "kind", "sql", "columns", "rows", "latency_ms"}] * 4,
        )
        self.assertEqual(
            [s["sql"] for s in steps], list(self.executor.calls), "步 SQL 与执行器实收一致"
        )
        self.assertEqual(steps[0]["columns"], ["commission_revenue"])
        self.assertEqual(steps[0]["rows"], [["300"]])
        self.assertEqual(steps[2]["columns"], ["Branch", "commission_revenue"])
        self.assertEqual(steps[2]["rows"], [["A", "90"], ["B", "170"]])
        self.assertEqual(steps[3]["rows"], [["A", "180"], ["B", "120"]])
        self.assertIn("SELECT", str(steps[0]["sql"]).upper())

    def test_one_business_audit_row_per_request(self) -> None:
        rows = self._rows()
        self.assertEqual(len(rows), 1, f"一次 HTTP 请求一条业务审计：{rows}")
        row = rows[0]
        self.assertEqual(row["endpoint"], f"{API}/analyze")
        self.assertEqual(row["kind"], "answer")
        self.assertEqual(row["bucket"], "business")
        self.assertEqual(row["row_count"], 0, "父轮不冒充单 SQL 结果：审计行数诚实为 0")
        self.assertAlmostEqual(row["latency_ms"], self.body["latency_ms"], places=6)

    def test_identity_claims_reach_sub_step_guard(self) -> None:
        """已验证 claims 随子步下推（与 /ask 同机制）：谓词进入子步 Guard 评估。

        现状口径（实测锁定，非缺陷）：金融模型仅有的非 admin 行级策略谓词经
        dim_broker / dim_customer 下推（semantic/policies/row_policy.yml），而分析
        合计子步查询只含 fact_trades + dim_date——谓词表不在查询且无语义模型
        可自动补 join（default_deny）→ 子步 1 被 Guard 拒绝。这正是 claims 真实
        到达子步 Guard 的证据：同一问句无身份对照组可达 answer 四步；换
        branch_manager 身份则第一步即 blocked，且 block_reason 指向策略谓词表。
        """
        executor = AnalysisExecutor()
        client, _ = self._client(executor)
        control = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(control.status_code, 200)
        self.assertEqual(control.json()["kind"], "answer", "无身份对照组可达 answer")
        calls_after_control = len(executor.calls)

        restricted = client.post(
            f"{API}/analyze",
            json={"question": FULL_Q},
            headers=self._auth("branch_manager", {"branch": "A"}),
        )
        self.assertEqual(restricted.status_code, 200)
        body = restricted.json()
        self.assertEqual(body["kind"], "blocked", "分支谓词表不在合计子步查询中 → Guard 拒绝")
        self.assertEqual(len(executor.calls), calls_after_control, "被拒轮新增零执行器调用")
        self.assertIn("dim_broker", body["block_reason"], "拒绝原因指向策略谓词表")
        analysis = body["analysis"]
        self.assertEqual(analysis["status"], "blocked")
        self.assertEqual(analysis["reason_code"], "guard_blocked")


# ---------------------------------------------------------------------------
# 无意图 fallback 与澄清：analysis 恒 null、键恒在；澄清零 SQL、reasons 为 list
# ---------------------------------------------------------------------------


class TestAnalyzeFallbackAndClarify(_BaseCase):
    """/analyze 的回落与澄清两态（决策③⑥）。"""

    def test_no_intent_delegates_to_ask_with_analysis_null(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor)
        resp = client.post(f"{API}/analyze", json={"question": NO_INTENT_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer", "无分析意图按决策③回落普通 ask")
        self.assertIn("analysis", body, "analysis 键恒在（非分析为 null），不消失")
        self.assertIsNone(body["analysis"])
        # 普通轮字段原样：携带单 SQL 结果（与分析父轮相反）
        self.assertEqual(body["sql"], executor.calls[0])
        self.assertEqual(body["row_count"], len(body["rows"]))
        self.assertEqual(len(executor.calls), 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "answer")

    def test_analysis_clarify_is_null_analysis_zero_sql_list_reasons(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor, generator=ForbiddenGenerator())
        resp = client.post(f"{API}/analyze", json={"question": CLARIFY_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "clarify")
        self.assertIsNone(body["analysis"], "澄清未开始分析：analysis 恒 null")
        self.assertEqual(executor.calls, [], "澄清轮零 SQL")
        self.assertIsNone(body["sql"])
        clar = body["clarification"]
        self.assertIsNotNone(clar)
        self.assertIsInstance(
            clar["reasons"], list, "msgpack checkpoint 往返后 reasons 为 list 非 tuple"
        )
        self.assertTrue(clar["reasons"])

    def test_fallback_error_round_mirrors_ask_error_semantics(self) -> None:
        """R5（评审 Minor-2）：fallback 轮（plan=None）执行故障保持 /ask error 轮语义。

        无分析意图的 /analyze 回落轮没有分析子步：error 若被替换成
        「分析子步骤执行失败（execution_error）」属误归因（该轮 analysis=null，
        客户端无从看到 reason_code）。净化文案仅限分析轮（plan 在）；fallback 轮
        与 /ask error 轮同形——post-Guard 已执行 SQL、尝试耗时、原始 error 文本。
        """
        calls: list[str] = []

        def raising_executor(sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
            calls.append(sql)
            raise RuntimeError("doris 断连")

        client, _ = self._client(raising_executor)
        ask_resp = client.post(f"{API}/ask", json={"question": NO_INTENT_Q}, headers=self._auth())
        self.assertEqual(ask_resp.status_code, 200)
        ask_body = ask_resp.json()
        self.assertEqual(ask_body["kind"], "error")
        fallback = client.post(
            f"{API}/analyze", json={"question": NO_INTENT_Q}, headers=self._auth()
        )
        self.assertEqual(fallback.status_code, 200)
        body = fallback.json()
        self.assertEqual(body["kind"], "error")
        self.assertIsNone(body["analysis"], "fallback 轮 analysis 恒 null")
        self.assertEqual(
            body["sql"], calls[1], "回落 error 轮携带 post-Guard 已执行 SQL（R1 同款）"
        )
        self.assertGreaterEqual(body["latency_ms"], 0.0, "尝试耗时如实保留")
        self.assertEqual(
            body["error"], ask_body["error"], "error 文本与 /ask error 轮同源（原始转述）"
        )
        self.assertEqual(body["error"], "RuntimeError: doris 断连")
        self.assertNotEqual(body["error"], SANITIZED_ANALYSIS_ERROR, "不得误标为分析失败")
        self.assertNotIn(SANITIZED_ANALYSIS_ERROR, fallback.text)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["kind"] for r in rows], ["error", "error"])

    def test_r2_clarify_flushes_stale_analysis_record(self) -> None:
        """R2（控制器裁定）：blocked 终态留存的 analysis_record 必须被澄清轮冲刷。

        澄清轮是新用户轮，与 node_plan 冲刷集同构：_clarify_turn 直写 _apply_state
        不经过 node_plan，漏掉 "analysis_record": None 会让上轮 failed 记录在
        澄清轮之后仍留存于 checkpoint。
        """
        executor = AnalysisExecutor()
        client, agent = self._client(executor, budget=DENY_BUDGET)
        sid = "t08-r2"
        blocked = client.post(
            f"{API}/analyze",
            json={"question": FULL_Q, "session_id": sid},
            headers=self._auth(),
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.json()["kind"], "blocked")
        values = _values(agent, sid)
        self.assertEqual(values["analysis_record"]["reason_code"], "guard_blocked")
        self.assertEqual(values["turns"], 1)
        clarify = client.post(
            f"{API}/analyze",
            json={"question": CLARIFY_Q, "session_id": sid},
            headers=self._auth(),
        )
        self.assertEqual(clarify.status_code, 200)
        self.assertEqual(clarify.json()["kind"], "clarify")
        values = _values(agent, sid)
        self.assertIsNone(
            values.get("analysis_record"), "澄清轮必须冲刷 analysis_record（R2 同构）"
        )
        self.assertEqual(values["turns"], 2, "澄清计一父轮")


# ---------------------------------------------------------------------------
# blocked：被拒 SQL 不出网 + 零执行器调用 + 失败步安全摘要
# ---------------------------------------------------------------------------


class TestAnalyzeBlocked(_BaseCase):
    """Guard 拒绝第一步（ADR L215-216：对外仅安全步骤摘要）。"""

    def test_guard_blocked_response_and_audit_leak_scan(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor, budget=DENY_BUDGET)
        resp = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "blocked")
        self.assertEqual(executor.calls, [], "被 Guard 拒绝：执行器零调用")
        analysis = body["analysis"]
        self.assertEqual(analysis["status"], "blocked")
        self.assertEqual(analysis["reason_code"], "guard_blocked")
        self.assertIsNone(analysis["totals"], "blocked 无 totals（决策⑥ L215）")
        self.assertEqual(analysis["items"], [])
        self.assertIsNone(analysis["text"])
        self.assertEqual(len(analysis["steps"]), 1)
        step = analysis["steps"][0]
        self.assertEqual(step["role"], "baseline_total", "失败步角色保留")
        self.assertEqual(step["kind"], "blocked")
        self.assertIsNone(step["sql"], "被拒 SQL 不得序列化")
        self.assertEqual(step["columns"], [])
        self.assertEqual(step["rows"], [])
        self.assertEqual(step["reason_code"], "guard_blocked")
        # 泄漏扫描：整个响应体（含 block_reason 与全部步骤）不含 SQL 文本
        self.assertNotIn("SELECT", resp.text, "被拒 SQL 不得出网（ADR L216）")
        self.assertIsInstance(body["block_reason"], str)
        self.assertTrue(body["block_reason"])
        for row in self._rows():
            self.assertNotIn("SELECT", json.dumps(row), "审计行不含 SQL")
        blocked_rows = [r for r in self._rows() if r["kind"] == "blocked"]
        self.assertEqual(len(blocked_rows), 1)
        self.assertEqual(blocked_rows[0]["bucket"], "business")


# ---------------------------------------------------------------------------
# error：失败步安全摘要 + 底层异常文本不出网（对外清洗）
# ---------------------------------------------------------------------------


class TestAnalyzeError(_BaseCase):
    """执行故障（ADR L216：不泄露底层异常中的连接信息）。"""

    def test_executor_failure_trims_steps_and_sanitizes_error(self) -> None:
        executor = AnalysisExecutor(fail_on=2)
        client, _ = self._client(executor)
        resp = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "error")
        # 对外清洗：父轮 error 为固定安全文案，不透传 "RuntimeError: doris 断连"
        self.assertEqual(body["error"], SANITIZED_ANALYSIS_ERROR)
        self.assertNotIn("断连", resp.text, "底层连接异常文本不得出网（ADR L216）")
        self.assertNotIn("RuntimeError", resp.text)
        analysis = body["analysis"]
        self.assertEqual(analysis["status"], "error")
        self.assertEqual(analysis["reason_code"], "execution_error")
        self.assertIsNone(analysis["totals"])
        self.assertEqual(analysis["items"], [])
        self.assertIsNone(analysis["text"])
        steps = analysis["steps"]
        self.assertEqual(len(steps), 2)
        self.assertEqual([s["role"] for s in steps], list(ANALYSIS_ROLES)[:2])
        # 已成功步保留完整证据；失败步裁为安全摘要（checkpoint 裁剪纪律同构）
        self.assertEqual(steps[0]["kind"], "answer")
        self.assertEqual(steps[0]["sql"], executor.calls[0])
        self.assertEqual(steps[0]["rows"], [["300"]])
        self.assertEqual(steps[1]["kind"], "error")
        self.assertIsNone(steps[1]["sql"], "失败步不携带 SQL")
        self.assertEqual(steps[1]["columns"], [])
        self.assertEqual(steps[1]["rows"], [])
        self.assertEqual(steps[1]["reason_code"], "execution_error")
        self.assertGreaterEqual(steps[1]["latency_ms"], 0.0, "失败步 latency 保留（诚实数值）")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "error")
        self.assertNotIn("断连", json.dumps(rows), "审计行不含底层异常文本")


# ---------------------------------------------------------------------------
# unavailable：前置资格门 ⇔ 零步、零 SQL（决策②⑥）
# ---------------------------------------------------------------------------


class TestAnalyzeUnavailable(_BaseCase):
    """综合不可用仍是 answer，不扩展 TurnKind（决策⑥ L214）。"""

    def test_missing_eligibility_is_unavailable_with_zero_steps(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(executor, eligibility=None)
        resp = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer", "综合不可用仍是 answer（不扩展 TurnKind）")
        analysis = body["analysis"]
        self.assertEqual(analysis["status"], "unavailable")
        self.assertEqual(analysis["reason_code"], "missing_eligibility")
        self.assertEqual(analysis["steps"], [], "前置门失败 ⇔ 零步（T07 裁决双射）")
        self.assertEqual(executor.calls, [], "前置门失败零 SQL")
        self.assertIsNone(analysis["totals"])
        self.assertEqual(analysis["items"], [])
        # 机器可读原因在 reason_code；text 是给人看的稳定文案（前置门文案
        # 不内嵌原因码——原因码字段承担机器语义，双处重复反而漂移）
        self.assertEqual(analysis["reason_code"], "missing_eligibility")
        self.assertIsInstance(analysis["text"], str)
        self.assertTrue(analysis["text"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "answer")

    def test_semantic_hash_mismatch_maps_to_snapshot_mismatch(self) -> None:
        executor = AnalysisExecutor()
        client, _ = self._client(
            executor,
            eligibility={
                "available": False,
                "eligible": False,
                "reason": "semantic_hash_mismatch",
                "evidence": None,
            },
        )
        resp = client.post(f"{API}/analyze", json={"question": FULL_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["status"], "unavailable")
        self.assertEqual(analysis["reason_code"], "snapshot_mismatch")
        self.assertEqual(analysis["steps"], [])
        self.assertEqual(executor.calls, [])


# ---------------------------------------------------------------------------
# R1（控制器裁定）：普通 ask / plan/execute 执行故障轮的既有契约锁定
# ---------------------------------------------------------------------------


class TestR1ErrorRoundContract(_BaseCase):
    """R1：error 轮携带 post-Guard 已执行 SQL 与 latency_ms（node_execute 现状）。

    分析父轮严格为空（sql/explanation null、columns/rows 空）不受本裁定影响——
    那是决策⑥的独立要求。本组测试只锁普通轮的既有行为，防未来无意识变更。
    """

    def _raising_client(self) -> tuple[TestClient, Any]:
        class RaisingExecutor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __call__(self, sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
                self.calls.append(sql)
                raise RuntimeError("doris 断连")

        executor = RaisingExecutor()
        agent = DataAgent(executor=executor, budget=_budget(), snapshot_meta=_meta())
        app = create_app(
            agent_factory=lambda _m: agent,
            audit=self.audit,
            rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
        )
        client = TestClient(app)
        self.addCleanup(client.close)
        return client, executor

    def test_ask_execution_error_round_keeps_executed_sql_and_latency(self) -> None:
        client, executor = self._raising_client()
        resp = client.post(f"{API}/ask", json={"question": NO_INTENT_Q}, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "error")
        self.assertTrue(executor.calls, "执行故障轮的 SQL 已过 Guard 并送达执行器")
        self.assertEqual(
            body["sql"], executor.calls[0], "R1 锁定：error 轮携带 post-Guard 已执行 SQL"
        )
        self.assertIn("SELECT", str(body["sql"]).upper())
        self.assertGreaterEqual(
            body["latency_ms"], 0.0, "R1 锁定：error 轮携带尝试耗时（诚实数值）"
        )
        # analysis 键在 /ask 响应同样恒在（非分析为 null）
        self.assertIn("analysis", body)
        self.assertIsNone(body["analysis"])

    def test_plan_execute_execution_error_round_keeps_executed_sql_and_latency(self) -> None:
        client, executor = self._raising_client()
        plan_body = {
            "metric": "commission_revenue",
            "dimensions": [],
            "time": {"granularity": "quarter", "value": "2013Q4"},
        }
        resp = client.post(f"{API}/plan/execute", json=plan_body, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "error")
        self.assertTrue(executor.calls)
        self.assertEqual(
            body["sql"], executor.calls[0], "R1 锁定：error 轮携带 post-Guard 已执行 SQL"
        )
        self.assertGreaterEqual(
            body["latency_ms"], 0.0, "R1 锁定：error 轮携带尝试耗时（诚实数值）"
        )
        self.assertIsNone(body["analysis"])


if __name__ == "__main__":
    unittest.main()
