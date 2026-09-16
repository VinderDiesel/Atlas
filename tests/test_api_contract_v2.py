#!/usr/bin/env python3
r"""HTTP 契约 v2 防漂移测试（ADR-0022 判据 1/2/4/5/6/7/9/10；无 DB）。

口径（落地实测 2026-09-16；与 ADR-0022 判据原文的漂移逐条登记——N1 以实测为准）：

- 判据 1/9：`EXPECTED_PATHS`（16 条字面量）== `app.openapi()["paths"]` 集合，
  且无前缀业务路径 404（硬切，无兼容期）。**P0b 已接入 TS 侧**：
  `frontend/src/api/endpoints.ts` 由本文件 `test_ts_endpoints_match_expected_paths`
  正则提取（`/"(\/(?:api\/v1|health)[a-z0-9_\/{}.-]*)"/g`，只认双引号字面量），
  与 `EXPECTED_PATHS` **双向相等**——`EXPECTED_PATHS` 保留为"正则提错东西"的兜底
  （设计页 §1.1 的裁定形态）。
- 判据 2：`/health` 根与前缀双挂点同 body。
- 判据 4：两桶独立（双向）——注入面：业务桶 max=1、治理桶 max=3。3 次治理请求
  （超出业务桶容量）后业务首请求仍 200；业务打满 429 后治理桶仍有余量。
  429 detail 标桶名 + Retry-After + 审计行 bucket 字段。
- 判据 5：快照缺失（factory 抛 SnapshotUnavailable）时 8 个治理集合全 200 且
  factory 零调用；同 app 的 /ask → 503（决策 ④ 隔离：面板恰在故障时最该可用）。
- 判据 6：/plan/execute 合法 Plan → answer（explanation 键集与 /ask 一致）；
  非法指标 → kind=error（不是 422 不是 500）；kind 永不为 clarify/handoff
  （plan_override 不进 Planner 即无解析歧义面）；无 session_id → 每次随机
  （一次性 thread，不落可续接会话态——代价 ⑥ 口径）；换身份 → 422 同 /ask。
- 判据 7：诚实性标志位逐条——values skipped=11 且 skip_reason 非空；
  reports structured=14 / pattern=19；zh_cn empty_placeholder=True（en_us=False）；
  broker registered=True（P-2sec 注册后反转，ADR 原文为 False，其注注明写
  「落地后此断言必须反转」）；snapshots 首条 sha=7c966e9 +
  is_latest_by_created_at=True + dc4f350 不是首条。
- 判据 10：FastAPI version == importlib.metadata.version("atlas")；包未安装 → "unknown"。
- 判据 6（配置面）：治理桶默认 240/60 + 独立 env 命名空间（ATLAS_GOVERNANCE_*）。

漂移登记（2026-09-16 实测 vs ADR-0022 原文，均为期间批次落地所致，不是本批错误）：
- 主报告 12→13、报告模式 18→19：P-2sec 批次新增 `e0f2d29.json` 主报告与
  `rls-verify-<sha>` 模式；
- 快照首条 a11d779→ccb4c8b：P-1 收口批次 2026-09-15T16:08:31+08:00 新锁快照；
- 主报告 13→14：P0a 批次（2026-09-16）锁快照 `7c966e9` 后新增
  `eval/reports/7c966e9.json`（报告模式集不变，仍 19）；
- 快照首条 ccb4c8b→7c966e9：P0a 同日 11:43:52 新锁（同数据多锁，指纹与 ccb4c8b
  全一致，见 `data/snapshots/README.md`）；
- explanation 13→14 键：ADR 述「13 个固定键」，实测 14（含 `data_refreshed_at`
  / `data_version`）——本文件按实测键集断言且与 /ask 逐键相等。
计数型断言（reports structured/pattern）会随新评测批次变红：这是**有意**的——
标志位是实测快照，变更须人工确认后更新常量，不得留着变成假绿（同 ADR-0022
判据 7 对 broker 断言的「必须反转」纪律）。
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from agent.factory import SnapshotUnavailable
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from serving.api import API_PREFIX, create_app
from serving.audit import AUDIT_FILENAME, AuditLog
from serving.auth import sign_token
from serving.ratelimit import (
    ENV_GOVERNANCE_MAX,
    ENV_GOVERNANCE_WINDOW,
    RateLimiter,
)

REPO = Path(__file__).resolve().parent.parent
API = API_PREFIX  # 唯一前缀事实源（serving/api.py 常量；本文件不重复字面量）

# 判据 1/9：16 条字面量（根 /health + 前缀 /health + 业务 4 + 治理 8 集合 + 2 钻取）
EXPECTED_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/api/v1/health",
        "/api/v1/plan",
        "/api/v1/compile",
        "/api/v1/ask",
        "/api/v1/plan/execute",
        "/api/v1/governance/models",
        "/api/v1/governance/metrics",
        "/api/v1/governance/dimensions",
        "/api/v1/governance/synonyms",
        "/api/v1/governance/values",
        "/api/v1/governance/policies",
        "/api/v1/governance/reports",
        "/api/v1/governance/snapshots",
        "/api/v1/governance/values/{item}",
        "/api/v1/governance/reports/{name}",
    }
)

# 8 个治理集合路径（判据 5 与 A8 同清单；钻取面不在本清单）
GOV_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("models", f"{API}/governance/models"),
    ("metrics", f"{API}/governance/metrics"),
    ("dimensions", f"{API}/governance/dimensions"),
    ("synonyms", f"{API}/governance/synonyms"),
    ("values", f"{API}/governance/values"),
    ("policies", f"{API}/governance/policies"),
    ("reports", f"{API}/governance/reports"),
    ("snapshots", f"{API}/governance/snapshots"),
)

# explanation 字段全集（实测 14 键；ADR 述 13——见模块 docstring 漂移登记）
EXPLANATION_KEYS = frozenset(
    {
        "data_refreshed_at",
        "data_version",
        "dimensions",
        "engine",
        "filters",
        "latency_ms",
        "metric",
        "metric_expression",
        "path",
        "policy_effect",
        "row_count",
        "sql",
        "tables",
        "time",
    }
)

# 诚实性标志位（判据 7 的实测快照——变更须有意更新，见模块 docstring）
SKIPPED_VALUES = 11
STRUCTURED_REPORTS = 14
REPORT_PATTERNS = 19
FIRST_SNAPSHOT_SHA = "7c966e9"
NON_FIRST_SNAPSHOT_SHA = "dc4f350"  # ADR 明写「断言它不是首条」

GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"
SECRET = "api-contract-v2-test-secret"

_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
# P7 双域白名单（与 tests/test_api_hardening.py 同口径；finance 单域用例其实只需 meta 表）
ALLOWED |= frozenset(
    {"atlas.dwd.store_sales", "atlas.dwd.date_dim", "atlas.dwd.dim_item", "atlas.dwd.dim_store"}
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集（同 tests/test_api.py）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
        self.calls.append(sql)
        return [("v",)], ["v"]


class _BaseCase(unittest.TestCase):
    """共享注入面：ATLAS_JWT_SECRET 环境恢复 + tmp 审计目录。"""

    def setUp(self) -> None:
        self._secret_was_set = "ATLAS_JWT_SECRET" in os.environ
        self._secret_orig = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-contract-")
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

    def _gov_client(self, **kwargs: Any) -> TestClient:
        """治理面客户端：factory 一旦被调用即断言失败（决策 ④ 零构造红线）。"""

        def _boom(_model_name: str) -> DataAgent:
            raise AssertionError("治理面触发了 agent 构造（决策 ④ 隔离被破坏）")

        return TestClient(create_app(agent_factory=_boom, audit=self.audit, **kwargs))


# ---------------------------------------------------------------------------
# 判据 1/2：openapi paths 集合 + 硬切 404 + health 双挂
# ---------------------------------------------------------------------------


class TestOpenapiPaths(_BaseCase):
    def test_paths_equal_expected_literal_set(self) -> None:
        """判据 1/9：集合相等（双向）。只断言超集会把「少一条」变成静默通过。"""
        app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        paths = set(app.openapi()["paths"])
        self.assertEqual(
            paths,
            EXPECTED_PATHS,
            f"契约 v2 路径漂移：多 {paths - EXPECTED_PATHS} 缺 {EXPECTED_PATHS - paths}",
        )
        self.assertEqual(len(paths), 16, f"设计页 §5 断言 16 条，实测 {len(paths)}")

    def test_ts_endpoints_match_expected_paths(self) -> None:
        """判据 9（TS 侧，P0b 接入）：endpoints.ts 正则提取 == EXPECTED_PATHS。

        正则只认双引号字面量（设计页 §1.1）：注释里双引号包裹的路径会被误提取
        成"第 17 条"——这正是要挡住"正则提错东西"的原因；断言双向相等而非子集
        （子集会让"前端写了不存在的端点"静默通过）。
        """
        ts_file = REPO / "frontend" / "src" / "api" / "endpoints.ts"
        self.assertTrue(ts_file.is_file(), f"缺少 {ts_file}（判据 9 的 TS 侧对象）")
        extracted = set(
            re.findall(
                r'"(\/(?:api\/v1|health)[a-z0-9_\/{}.-]*)"', ts_file.read_text(encoding="utf-8")
            )
        )
        self.assertEqual(
            extracted,
            EXPECTED_PATHS,
            "endpoints.ts 与契约漂移："
            f"多 {extracted - EXPECTED_PATHS} 缺 {EXPECTED_PATHS - extracted}",
        )


class TestHardCut404(_BaseCase):
    """判据 1 后半句：无前缀业务路径 404（硬切，不留兼容别名）。

    锚定「无 dist」态（P0b）：本机 frontend/dist 存在时 create_app 会挂 SPA
    catch-all——POST 流虽仍落非 GET 分支 404，但 GET /governance/values 会被 SPA
    接走变 200 HTML。本类断言的是"硬切 404"本身，必须在无 SPA 挂载的形态上验证；
    两态完整矩阵（含 SPA 挂载后的等价断言）见 tests/test_spa_static.py。
    """

    def setUp(self) -> None:
        super().setUp()
        no_dist = Path(self._audit_tmp.name) / "no-dist"
        with mock.patch("serving.api.FRONTEND_DIST", no_dist):
            app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_unprefixed_business_paths_are_404(self) -> None:
        for old in ("/plan", "/compile", "/ask", "/plan/execute"):
            with self.subTest(path=old):
                resp = self.client.post(old, json={}, headers=self._auth())
                self.assertEqual(
                    resp.status_code, 404, f"{old} 仍可达——双份契约并存（0022:152 禁止）"
                )

    def test_unprefixed_governance_path_is_404(self) -> None:
        resp = self.client.get("/governance/values", headers=self._auth())
        self.assertEqual(resp.status_code, 404, "治理面只存在于 /api/v1/governance/*")


class TestHealthDualMount(_BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(agent_factory=lambda _m: None, audit=self.audit))
        self.addCleanup(self.client.close)

    def test_root_and_prefixed_return_same_body(self) -> None:
        """判据 2：同一 handler 两处注册——body 必须逐键相同（boot_id 同进程恒定）。"""
        root = self.client.get("/health")
        prefixed = self.client.get(f"{API}/health")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(prefixed.status_code, 200)
        self.assertEqual(root.json(), prefixed.json(), "双挂点 body 不一致")

    def test_both_mounts_are_public(self) -> None:
        """认证面：两挂点都不带 Bearer 也应 200（compose 探针无 token）。"""
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get(f"{API}/health").status_code, 200)


# ---------------------------------------------------------------------------
# 判据 4：两桶独立（双向）+ 429 标桶名（真链侧见 api_acceptance A8）
# ---------------------------------------------------------------------------


class TestTwoBucketIndependence(_BaseCase):
    """注入面：业务桶 max=1、治理桶 max=3（小窗口确定性；不碰真实 .env）。"""

    def setUp(self) -> None:
        super().setUp()
        self.executor = FakeExecutor()
        agent = DataAgent(executor=self.executor, budget=BUDGET)
        self.client = TestClient(
            create_app(
                agent_factory=lambda _m: agent,
                audit=self.audit,
                rate_limiter=RateLimiter(max_requests=1, window_seconds=60),
                governance_rate_limiter=RateLimiter(max_requests=3, window_seconds=60),
            )
        )
        self.addCleanup(self.client.close)

    def test_buckets_do_not_share_quota(self) -> None:
        # 3 次治理请求（已超出业务桶容量 1）：若共享实例，第 2 次起必 429
        for i in range(3):
            resp = self.client.get(f"{API}/governance/values", headers=self._auth())
            self.assertEqual(resp.status_code, 200, f"第 {i + 1} 次治理请求被业务桶挤占")
        # 业务桶未被治理消耗：首问 200
        first = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(first.status_code, 200, "业务首问 429——治理请求消耗了业务桶")
        self.assertEqual(first.json()["kind"], "answer")
        # 业务桶打满（max=1）→ 429 标「业务面」+ Retry-After
        second = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(second.status_code, 429)
        self.assertIn("业务面", second.json()["detail"])
        self.assertGreaterEqual(int(second.headers["Retry-After"]), 1)
        # 业务 429 未消耗治理桶：第 4 次治理请求才 429，标「治理面」
        gov4 = self.client.get(f"{API}/governance/values", headers=self._auth())
        self.assertEqual(gov4.status_code, 429, "治理桶被业务 429 连带/或计数漂移")
        self.assertIn("治理面", gov4.json()["detail"])
        self.assertGreaterEqual(int(gov4.headers["Retry-After"]), 1)

    def test_audit_rows_carry_bucket_field(self) -> None:
        """判据 4 的审计侧：429 行 bucket 标名；治理 200 行 kind=governance_read。"""
        for _ in range(3):
            self.client.get(f"{API}/governance/values", headers=self._auth())
        self.client.get(f"{API}/governance/values", headers=self._auth())  # 第 4 次 429
        self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        rows = self._rows()
        gov429 = [r for r in rows if r["kind"] == "rate_limited" and r["bucket"] == "governance"]
        self.assertEqual(len(gov429), 1, f"治理 429 审计行异常：{rows}")
        self.assertEqual(gov429[0]["status"], 429)
        reads = [r for r in rows if r["kind"] == "governance_read"]
        self.assertEqual(len(reads), 3, "治理 200 请求每行一条 governance_read")
        self.assertTrue(all(r["bucket"] == "governance" for r in reads))
        biz = [r for r in rows if r["kind"] == "answer"]
        self.assertEqual(len(biz), 1)
        self.assertEqual(biz[0]["bucket"], "business")
        self.assertEqual(biz[0]["endpoint"], f"{API}/ask")

    def test_governance_env_defaults_and_namespace(self) -> None:
        """判据 6 配置面：治理桶默认 240/60 + 独立 env 覆盖（ADR-0022 决策 ⑥）。"""
        saved = {k: os.environ.pop(k, None) for k in (ENV_GOVERNANCE_MAX, ENV_GOVERNANCE_WINDOW)}
        try:
            lim = RateLimiter.governance_from_env()
            self.assertEqual((lim.max_requests, lim.window_seconds), (240, 60))
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        with mock.patch.dict(os.environ, {ENV_GOVERNANCE_MAX: "5", ENV_GOVERNANCE_WINDOW: "30"}):
            overridden = RateLimiter.governance_from_env()
            self.assertEqual((overridden.max_requests, overridden.window_seconds), (5, 30))
        # 治理 env 不得影响业务桶（现名沿用）
        with mock.patch.dict(os.environ, {ENV_GOVERNANCE_MAX: "5"}):
            self.assertEqual(RateLimiter.from_env().max_requests, 60)


# ---------------------------------------------------------------------------
# 判据 5：快照缺失时治理面隔离（factory 零调用）
# ---------------------------------------------------------------------------


class TestGovernanceIsolation(_BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []

        def factory(model_name: str) -> DataAgent:
            self.calls.append(model_name)
            raise SnapshotUnavailable("绑定快照（测试注入）缺少语义模型所需表：x")

        self.client = TestClient(
            create_app(
                agent_factory=factory,
                audit=self.audit,
                rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
                governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            )
        )
        self.addCleanup(self.client.close)

    def test_governance_all_200_when_snapshot_missing(self) -> None:
        """决策 ④：8 集合全 200 且不触发 agent 构造；同 app 的 /ask 如实 503。"""
        for name, url in GOV_COLLECTIONS:
            with self.subTest(endpoint=name):
                resp = self.client.get(url, headers=self._auth())
                self.assertEqual(resp.status_code, 200, f"{name} 在快照缺失时非 200")
                self.assertEqual(resp.json()["kind"], f"governance.{name}")
        self.assertEqual(self.calls, [], "治理面触发了 agent 构造（决策 ④ 被破坏）")
        ask = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(ask.status_code, 503, "快照缺失时 /ask 应 503")
        self.assertIn("无法绑定锁定快照", ask.json()["detail"])
        self.assertEqual(self.calls, ["finance"], "工厂应只被 /ask 调用一次")
        # 503 之后治理面仍 200（面板恰在故障排查时最需要可用）
        again = self.client.get(f"{API}/governance/values", headers=self._auth())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.calls, ["finance"], "治理请求再次触发了 agent 构造")


# ---------------------------------------------------------------------------
# 判据 6：/plan/execute 行为契约
# ---------------------------------------------------------------------------


class TestPlanExecute(_BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.executor = FakeExecutor()
        agent = DataAgent(executor=self.executor, budget=BUDGET)
        self.client = TestClient(
            create_app(
                agent_factory=lambda _m: agent,
                audit=self.audit,
                rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
                governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            )
        )
        self.addCleanup(self.client.close)
        # 合法 Plan 单一来源：/plan 产物原样回填（防指标名手写漂移）
        plan_resp = self.client.post(
            f"{API}/plan", json={"question": GOLD102_Q}, headers=self._auth()
        )
        self.assertEqual(plan_resp.status_code, 200)
        self.plan = plan_resp.json()["plan"]

    def _execute(self, plan: dict, **extra: Any) -> Any:
        return self.client.post(f"{API}/plan/execute", json={**plan, **extra}, headers=self._auth())

    def test_legal_plan_answer_with_sql_limits_and_explanation_shape(self) -> None:
        """合法 Plan → answer：SQL 含 LIMIT 与时间谓词；explanation 键集与 /ask 一致。"""
        resp = self._execute(self.plan)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer", f"期望 answer：{body.get('error')}")
        self.assertIn("LIMIT", str(body["sql"]).upper(), "执行 SQL 缺 LIMIT（Guard 强制上限）")
        self.assertIn("2013", str(body["sql"]), "执行 SQL 缺时间谓词")
        # 行数与 LIMIT 的相等性由真链（api_acceptance A9）断言；此处 stub 只回 1 行
        self.assertEqual(body["row_count"], len(body["rows"]), "row_count 与 rows 长度须一致")
        self.assertTrue(self.executor.calls, "answer 必须到达执行器")
        self.assertEqual(set(body["explanation"]), EXPLANATION_KEYS)
        # 形态与 /ask 逐键一致（ADR-0022 判据 6 的「与 /ask 一致」原文）
        ask = self.client.post(f"{API}/ask", json={"question": GOLD102_Q}, headers=self._auth())
        self.assertEqual(set(ask.json()["explanation"]), set(body["explanation"]))

    def test_default_question_is_plan_normalized_text(self) -> None:
        body = self._execute(self.plan).json()
        expected = (
            f"直接执行 Plan：{self.plan['metric']}，按 {self.plan['dimensions'][0]}，"
            f"{self.plan['time']['value']} {self.plan['time']['granularity']}"
        )
        self.assertEqual(body["question"], expected, "缺省 question 应为 Plan 规范化文本")

    def test_explicit_question_passthrough(self) -> None:
        body = self._execute(self.plan, question="手改维度的计划执行").json()
        self.assertEqual(body["question"], "手改维度的计划执行")

    def test_illegal_metric_is_turn_error_not_http_error(self) -> None:
        """判据 6：指标名不存在 → kind=error（200），不是 422 不是 500。"""
        resp = self._execute({**self.plan, "metric": "不存在的指标"})
        self.assertEqual(resp.status_code, 200, "非法 Plan 不得是 HTTP 层错误（N3 单通道）")
        body = resp.json()
        self.assertEqual(body["kind"], "error")
        self.assertIn("指标不存在", str(body["error"]))
        self.assertEqual(self.executor.calls, [], "非法 Plan 不得到达执行器")

    def test_kind_never_clarify_or_handoff(self) -> None:
        """plan_override 不进 Planner：无解析面 → clarify/handoff 不可达。"""
        kinds = {
            self._execute(self.plan).json()["kind"],
            self._execute({**self.plan, "metric": "不存在的指标"}).json()["kind"],
        }
        self.assertLessEqual(kinds, {"answer", "blocked", "error"}, f"出现了非法 kind：{kinds}")

    def test_identity_injection_with_branch_predicate(self) -> None:
        """带 identity → 行级策略随 Guard 注入（与 /ask 同机制，标硬化面）。"""
        headers = self._auth("branch_manager", {"branch": "east"})
        resp = self.client.post(f"{API}/plan/execute", json=self.plan, headers=headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "answer")
        self.assertIn("= 'east'", self.executor.calls[0], "分支谓词未注入执行 SQL")
        effect = str(body["explanation"]["policy_effect"])
        self.assertIn("rp_branch_visible", effect)
        self.assertNotIn("east", effect, "条件值不得外泄（0011 口径）")

    def test_no_session_id_is_fresh_disposable_each_time(self) -> None:
        """代价 ⑥：缺省 = 一次性 thread——两次调用回显不同随机键。"""
        sid1 = self._execute(self.plan).json()["session_id"]
        sid2 = self._execute(self.plan).json()["session_id"]
        self.assertTrue(sid1 and sid2)
        self.assertNotEqual(sid1, sid2, "缺省 session_id 重复 = 写入了可续接会话态")

    def test_session_identity_conflict_422_like_ask(self) -> None:
        """给定 session_id 即与 /ask 同一会话空间：换身份 → 422（端点捕获路径）。"""
        sid = "contract-v2-sid-conflict"
        first = self.client.post(
            f"{API}/plan/execute",
            json={**self.plan, "session_id": sid},
            headers={
                "Authorization": (
                    f"Bearer {sign_token('branch_manager', {'branch': 'east'}, secret=SECRET)}"
                )
            },
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"{API}/plan/execute",
            json={**self.plan, "session_id": sid},
            headers=self._auth(),
        )
        self.assertEqual(second.status_code, 422)
        self.assertIn("会话身份冲突", second.json()["detail"])


# ---------------------------------------------------------------------------
# 判据 7：治理端点诚实性标志位（实测快照——见模块 docstring 漂移登记）
# ---------------------------------------------------------------------------


class TestHonestyFlags(_BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self._gov_client(
            rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
        )
        self.addCleanup(self.client.close)

    def _items(self, url: str) -> list[dict[str, Any]]:
        resp = self.client.get(url, headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        return list(resp.json()["items"])

    def test_values_skipped_flag_and_reason(self) -> None:
        items = self._items(f"{API}/governance/values")
        skipped = [i for i in items if i["status"] == "skipped"]
        self.assertEqual(len(skipped), SKIPPED_VALUES, f"skipped 实测漂移：{len(skipped)}")
        self.assertTrue(
            all(isinstance(i["skip_reason"], str) and i["skip_reason"] for i in skipped),
            "skipped 条目必须携带非空 skip_reason（阈值事实如实透传）",
        )

    def test_reports_structured_and_pattern_flags(self) -> None:
        items = self._items(f"{API}/governance/reports")
        structured = [i for i in items if i["structured"]]
        patterns = {i["pattern"] for i in items}
        self.assertEqual(len(structured), STRUCTURED_REPORTS, "主报告数漂移")
        self.assertEqual(len(patterns), REPORT_PATTERNS, "报告模式数漂移")
        for entry in items:
            with self.subTest(name=entry["name"]):
                if entry["structured"]:
                    self.assertEqual(
                        set(entry),
                        {
                            "name",
                            "pattern",
                            "structured",
                            "size_bytes",
                            "mtime",
                            "sha",
                            "created_at",
                            "dry",
                            "domains",
                        },
                        "主报告 7 键 body 形态漂移",
                    )
                    self.assertEqual(entry["pattern"], "<sha>", "主报告模式标签应为 <sha>")
                    self.assertIsInstance(entry["dry"], bool, "dry 必须透传为布尔")
                else:
                    self.assertEqual(
                        set(entry),
                        {"name", "pattern", "structured", "size_bytes", "mtime"},
                        "非主报告不得携带伪造的统一表头（ADR-0018 代价 ⑥）",
                    )

    def test_synonyms_empty_placeholder_flags(self) -> None:
        zh = self._items(f"{API}/governance/synonyms?locale=zh_cn")[0]
        self.assertIs(zh["empty_placeholder"], True, "zh_cn 当前为空占位（实测两节皆空）")
        self.assertTrue(zh["authority_note"], "空占位必须携带权威源说明")
        en = self._items(f"{API}/governance/synonyms?locale=en_us")[0]
        self.assertIs(en["empty_placeholder"], False, "en_us 有词条，不得误标空占位")

    def test_policies_broker_registered_reversed(self) -> None:
        """P-2sec 注册 broker 后按 ADR-0022 判据 7 注记反转的断言（原为 False）。"""
        items = self._items(f"{API}/governance/policies")
        roles = {r["name"]: r for p in items for r in p["roles"]}
        self.assertIn("broker", roles, "broker 不在角色目录（载体缺失）")
        self.assertIs(roles["broker"]["registered"], True, "broker 注册状态应为 True（已反转）")
        self.assertTrue(
            all(isinstance(r["registered"], bool) for r in roles.values()),
            "registered 必须逐角色为布尔标志位",
        )

    def test_snapshots_latest_flag_and_order(self) -> None:
        items = self._items(f"{API}/governance/snapshots")
        created = [str(i["created_at"]) for i in items]
        self.assertEqual(created, sorted(created, reverse=True), "created_at 必须降序（陷阱 6）")
        self.assertEqual(items[0]["sha"], FIRST_SNAPSHOT_SHA, "首条 sha 漂移（实测快照）")
        self.assertIs(items[0]["is_latest_by_created_at"], True)
        self.assertEqual(
            [i for i in items if i["is_latest_by_created_at"]], [items[0]], "最新标志唯一"
        )
        shas = [i["sha"] for i in items]
        self.assertIn(NON_FIRST_SNAPSHOT_SHA, shas, "dc4f350 载体不在清单（判据依赖它）")
        self.assertNotEqual(shas[0], NON_FIRST_SNAPSHOT_SHA, "dc4f350 不得是首条")


# ---------------------------------------------------------------------------
# 判据 10：OpenAPI version 单一事实源
# ---------------------------------------------------------------------------


class TestVersionSource(_BaseCase):
    def test_app_version_equals_package_metadata(self) -> None:
        app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        self.assertEqual(app.version, importlib_metadata.version("atlas"))
        self.assertEqual(app.openapi()["info"]["version"], app.version)

    def test_version_reads_metadata_not_constant(self) -> None:
        """换掉 metadata 版本，新 app 必须跟着变（防硬编码常量回归）。"""
        with mock.patch("serving.api.importlib_metadata.version", return_value="9.9.9"):
            app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        self.assertEqual(app.version, "9.9.9")

    def test_version_unknown_when_package_missing(self) -> None:
        """源码树裸跑（包未安装）→ 如实说 "unknown"，不回落看似合理的常量。"""
        with mock.patch(
            "serving.api.importlib_metadata.version",
            side_effect=importlib_metadata.PackageNotFoundError,
        ):
            app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        self.assertEqual(app.version, "unknown")


if __name__ == "__main__":
    unittest.main()
