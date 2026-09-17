"""HTTP API 真实链路验收（ADR-0012，serving/api.py）：全 HTTP 栈 + 真 Doris。

用法
----
    uv run --env-file .env python eval/api_acceptance.py [--report 路径]
（make api-verify）

场景与验收口径（README 对外 HTTP API 小节引用本产物，禁止手写数字）
    A1 问→编→问  ：gold-102 问句经 /plan（Bearer）→ 响应 Plan 原样 POST /compile
                     → 出口 SQL 只读形态（无写语句 + LIMIT 强制）→ /ask answer：
                     行集 sha256 == gold-102.json 锚定 result_hash（EX 与锁定
                     快照一致），row_count == LIMIT
    A2 歧义反问  ：gold-104 经 /ask → 200 + kind=clarify（CLI exit 1 语义的 HTTP
                     化），反问轮 SQL 不达执行器（独立 Agent 计数为零）
    A3 认证      ：无 token / 伪造签名 /ask → 401（JWT 中间件在真链生效）
    A4 存活      ：/health 公开 200（记录 head_sha 与 snapshot_sha 实际值）；
                     根与前缀双挂点同 body（契约 v2 判据 2）
    A5 三角色差异：gold-146 经 /ask × hq_admin / branch_manager（分支取总部 Top
                     实测值，serving/rls_verify.py 同源逻辑）——行级过滤生效
                     （branch_manager 行数减少 + 全行分支匹配 + 本分支 Top 行不
                     漏）+ explanation 含策略名 rp_branch_visible 且不含条件值
                     （0011 不外泄）；hq_admin EX 与 gold-146 锚定 hash 一致
    A6 会话冲突  ：同 session_id 换 token（branch_manager → hq_admin）→ 422
                     「会话身份冲突」（C2 指纹绑定在 HTTP 层生效）
    A7 零售+跨域 ：retail model 双档——category_analyst（TN + Shoes/Electronics）
                     → 结果品类 ⊆ 受限集 + explanation 含 rp_dept_visible；
                     hq_admin → policy_effect 报 rp_dept_visible（域 → 策略按
                     模型声明解析，修复旧写死 rp_branch_visible 的跨域误报）；
                     region_manager × finance model → kind=error（身份策略解析
                     失败：域不匹配，Guard 之前即拒——ADR-0021 判据 11/12）
    A8 治理面+两桶：治理面 8 集合端点一轮全 200（信封 kind=governance.<名>）+
                     两桶独立——注入极小业务桶（3/60）与宽松治理桶（10_000/60），
                     8 次治理请求后业务 /ask 仍 200（若共享实例业务桶必先被打爆；
                     ADR-0022 判据 4 的真链侧；单测侧见 test_api_contract_v2.py）
    A9 Plan直执  ：/plan/execute 真链——/plan 产物原样回填执行，kind=answer、
                     行集 hash 与 gold-102 锚定一致（EX 与快照一致），缺省
                     session_id 回显为一次性随机键（ADR-0022 决策 ③/代价 ⑥）
    A10 多步分析 ：ADR-0026 四步贡献分析真链（独立 client 绑 7c966e9 meta +
                     显式资格证据）——a) hq_admin /analyze → answer：四步全 SQL
                     （LIMIT+2013+1 = 1）、totals 三键齐、父轮不冒充行集；
                     b) branch_manager /analyze → blocked（guard_blocked）+ 零 SQL
                     达执行器 + 同会话 /ask 仍可用；c) 同会话流 /analyze → /ask
                     turns=2；d) 同 sid 换 bm token → 422 会话身份冲突

注入口径：与 e2e_acceptance.py 同构——真执行器（eval/runner.execute_sql）+ 锁定
快照（2026-09-05 P7 后统一 29 表全量数据版本，最新入库锁定指纹 b933e20；A1-A4 历史
用 7d48dcb（25 表 finance 域）——finance 数据未变（make eval b933e20 双域全绿证明），
仅快照覆盖新增零售 4 表，hash 锚定断言不移动）。不走 create_live_agent 的「当前 HEAD
meta」校验：代码 commit 后 HEAD 无 meta 属常态（数据未重装），api-verify 验证的是
HTTP 面 + 真数据链等价性；503 语义已由 tests/test_api.py 契约覆盖。
A7 零售档与 serving/rls_verify.py 零售域同 meta 同问句同 claims（载体单源互证）。
契约 v2（ADR-0022）：全部调用点带 /api/v1 前缀（硬切，旧路径 404 由契约测试锁定）。

输出：stdout + JSON 报告；场景断言失败 → 退出码 1。
依赖：Doris up + 锁定快照表可查 + .env 有 ATLAS_JWT_SECRET。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent.compiler import SemanticModel
from agent.graph import DataAgent
from eval.analysis_eligibility import load_eligibility
from eval.runner import DOMAIN_MODELS, build_budget, execute_sql, git_short_sha
from serving.api import create_app
from serving.audit import AuditLog
from serving.auth import sign_token
from serving.ratelimit import RateLimiter
from serving.rls_verify import (  # 零售载体与分支取值单源（rls-verify 同载体互证）
    RETAIL_CATEGORIES,
    RETAIL_QUESTION,
    RETAIL_REGION,
    pick_branch_value,
)

REPO = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO / "eval" / "gold"
SNAPSHOT_META = REPO / "data/snapshots" / "b933e20.meta.json"
# A10 分析场景专用快照（R10-4）：b933e20 / 7d48dcb 均未登记资格证据，分析面必须绑
# 7c966e9（有 data/snapshots/7c966e9.analysis.json）；A1~A9 的 b933e20 锚定不动。
ANALYSIS_SNAPSHOT_META = REPO / "data/snapshots" / "7c966e9.meta.json"

# 契约 v2 前缀（ADR-0022 决策 ①②）：业务面与治理面端点一律带前缀；`/health`
# 双挂——A4 同时打根与前缀两个挂点并断言同 body（判据 2 的真链侧）。
API = "/api/v1"

# 场景问句与锚点（与 eval/gold/ 同源，防文档问句漂移）
GOLD102_Q_ID = "gold-102"  # 命中：按分支统计 2013 年佣金收入 Top5
GOLD104_Q_ID = "gold-104"  # 歧义：最近交易情况怎么样（反问样本）
GOLD146_Q_ID = "gold-146"  # RLS 载体：按分支和客户等级统计 2015 年交易额 Top5
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE")

# A10 载体问句（与 e2e_acceptance.py S8 / eval/analysis/finance/attribution-001.json 同源）
ANALYSIS_Q = "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"
# A10b/A10d 受限角色分支值（与 e2e_acceptance.py S9、A6 同源实测值，单源注释互证）
BRANCH = "uHtbMrIxbLVfWHFhCIeAnTu"


def load_gold(gold_id: str) -> dict[str, Any]:
    """按域目录加载（2026-09-05 gold 目录化 finance/retail 后，探测免调用方改域）。"""
    for sub in ("finance", "retail"):
        path = GOLD_DIR / sub / f"{gold_id}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"gold {gold_id!r} 未找到（期望 {GOLD_DIR}/<finance|retail>/）")


def result_hash(rows: list[Any]) -> str:
    """行集 → sha256 前缀（列序/行序固定；HTTP rows 已是 str 形态，与 gold 同口径）。"""
    payload = "\n".join("\t".join("NULL" if v is None else str(v) for v in row) for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class CountingExecutor:
    """包装真实 Doris 执行器：记录收到（已过 Guard）的 SQL 条数（同 e2e_acceptance）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return execute_sql(sql)


def _live_client(domain: str = "finance") -> tuple[TestClient, CountingExecutor]:
    """真链注入面：真 Doris 执行器 + 锁定快照预算（e2e 同构），独立 Agent 计数。

    domain：finance（缺省）| retail——P7 双模型后按域直构（A7 零售档需要
    atlas_retail 语义模型）；agent_factory 签名 (model_name) -> DataAgent
    （create_app 多模型路由，2026-09-05 起）。审计注入 disabled：审计/限流面
    契约由 tests/test_api_hardening.py 承担，本验收聚焦 HTTP RLS/会话语义，
    不产生仓库侧审计文件副作用。
    """
    meta = json.loads(SNAPSHOT_META.read_text(encoding="utf-8"))
    executor = CountingExecutor()
    if domain == "finance":
        agent = DataAgent(executor=executor, budget=build_budget(meta), snapshot_meta=meta)
    elif domain == "retail":
        model = SemanticModel(DOMAIN_MODELS["retail"])
        agent = DataAgent(
            model=model, executor=executor, budget=build_budget(meta), snapshot_meta=meta
        )
    else:
        raise ValueError(f"未知域：{domain!r}")
    return (
        TestClient(create_app(agent_factory=lambda _model: agent, audit=AuditLog(enabled=False))),
        executor,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _analysis_client() -> tuple[TestClient, CountingExecutor]:
    """A10 分析场景专用注入面（R10-4）：7c966e9 meta + 显式挂资格证据。

    A1~A9 维持 b933e20 锚定不动；分析快照必须已登记
    data/snapshots/7c966e9.analysis.json（缺证据时前置门 missing_eligibility 先于
    一切触发）。直构 DataAgent 无 analysis_eligibility（仅 agent/factory 附加），
    分析场景 agent 必须显式 `load_eligibility(SemanticModel(), meta)` 挂上。
    """
    meta = json.loads(ANALYSIS_SNAPSHOT_META.read_text(encoding="utf-8"))
    executor = CountingExecutor()
    agent = DataAgent(executor=executor, budget=build_budget(meta), snapshot_meta=meta)
    agent.analysis_eligibility = load_eligibility(SemanticModel(), meta)
    return (
        TestClient(create_app(agent_factory=lambda _model: agent, audit=AuditLog(enabled=False))),
        executor,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", help="JSON 报告输出路径（默认 eval/reports/api-acceptance-<sha>.json）"
    )
    args = parser.parse_args()

    ts = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z").replace(":", "")
    sha = git_short_sha()
    token = sign_token("hq_admin", {})  # env ATLAS_JWT_SECRET（make api-verify 走 .env）
    gold102 = load_gold(GOLD102_Q_ID)
    gold104 = load_gold(GOLD104_Q_ID)
    scenarios: list[dict[str, Any]] = []

    # ---- A1：/plan → /compile → /ask 全链（只读形态 + EX 与快照一致）----
    client, executor = _live_client()
    try:
        plan_resp = client.post(
            f"{API}/plan", json={"question": gold102["question"]}, headers=_auth(token)
        )
        assert plan_resp.status_code == 200, f"A1 /plan HTTP {plan_resp.status_code}"
        plan_body = plan_resp.json()
        assert plan_body["kind"] == "plan", f"A1 期望 kind=plan，实际 {plan_body['kind']}"
        plan = plan_body["plan"]
        assert plan["metric"] == gold102["expected_metric"], "A1 metric 与 gold 标注不一致"

        comp_resp = client.post(f"{API}/compile", json=plan, headers=_auth(token))
        assert comp_resp.status_code == 200, f"A1 /compile HTTP {comp_resp.status_code}"
        sql = comp_resp.json()["sql"]
        upper = sql.upper()
        assert not any(re.search(rf"\b{kw}\b", upper) for kw in WRITE_KEYWORDS), (
            f"A1 出口 SQL 含写语句：{sql}"
        )
        assert "LIMIT" in upper, "A1 出口 SQL 缺 LIMIT（Guard 强制上限形态）"

        ask_resp = client.post(
            f"{API}/ask", json={"question": gold102["question"]}, headers=_auth(token)
        )
        assert ask_resp.status_code == 200, f"A1 /ask HTTP {ask_resp.status_code}"
        body = ask_resp.json()
        assert body["kind"] == "answer", f"A1 期望 answer，实际 {body['kind']}"
        assert body["row_count"] == plan["limit"], f"A1 行数 ≠ LIMIT：{body['row_count']}"
        assert executor.calls, "A1 answer 必须到达真实执行器"
        assert result_hash(list(body["rows"])) == gold102["result_hash"][:12], (
            "A1 行集 hash 与 gold-102 锚定不一致（EX 漂移）"
        )
        scenarios.append(
            {
                "id": "A1",
                "title": "问→编→问全链（只读 + EX）",
                "goal": "gold-102 经 /plan→/compile→/ask，EX 与锁定快照一致",
                "question": gold102["question"],
                "plan": plan,
                "sql": sql,
                "sql_readonly": "pass",
                "kind": body["kind"],
                "metric": body["metric"],
                "row_count": body["row_count"],
                "rows_sample": body["rows"][:3],
                "result_hash": result_hash(list(body["rows"])),
                "gold_result_hash_prefix": gold102["result_hash"][:12],
                "executor_calls": len(executor.calls),
            }
        )
    finally:
        client.close()

    # ---- A2：歧义 /ask → 200 + kind=clarify（反问不执行 SQL）----
    client, executor = _live_client()
    try:
        resp = client.post(
            f"{API}/ask", json={"question": gold104["question"]}, headers=_auth(token)
        )
        assert resp.status_code == 200, f"A2 /ask HTTP {resp.status_code}"
        body = resp.json()
        assert body["kind"] == "clarify", f"A2 期望 clarify，实际 {body['kind']}"
        assert body["clarification"] is not None and body["clarification"]["reasons"]
        assert executor.calls == [], "A2 反问轮 SQL 不得达执行器"
        scenarios.append(
            {
                "id": "A2",
                "title": "歧义反问（HTTP 化）",
                "goal": "歧义问句经 HTTP → 200 + kind=clarify，不猜答",
                "question": gold104["question"],
                "kind": body["kind"],
                "clarification_kind": body["clarification"]["kind"],
                "reasons": body["clarification"]["reasons"],
                "executor_calls": len(executor.calls),
            }
        )
    finally:
        client.close()

    # ---- A3：认证（JWT 中间件在真链生效）----
    client, _ = _live_client()
    try:
        no_token = client.post(f"{API}/ask", json={"question": gold102["question"]})
        forged = client.post(
            f"{API}/ask",
            json={"question": gold102["question"]},
            headers=_auth(sign_token("hq_admin", {}, secret="api-verify-wrong-secret")),
        )
        assert no_token.status_code == 401, f"A3 无 token 期望 401，实际 {no_token.status_code}"
        assert forged.status_code == 401, f"A3 伪造签名期望 401，实际 {forged.status_code}"
        scenarios.append(
            {
                "id": "A3",
                "title": "认证拦截",
                "goal": "无 token / 伪造签名 → 401",
                "missing_token_status": no_token.status_code,
                "forged_signature_status": forged.status_code,
            }
        )
    finally:
        client.close()

    # ---- A4：/health 公开面 + 双挂同 body（如实记录当前 HEAD 的快照绑定状态）----
    client, _ = _live_client()
    try:
        resp = client.get("/health")  # 根挂点：compose healthcheck 的既有入口
        assert resp.status_code == 200, f"A4 /health HTTP {resp.status_code}"
        r_pre = client.get(f"{API}/health")  # 前缀挂点（决策 ① 双挂同一 handler）
        assert r_pre.status_code == 200, f"A4 {API}/health HTTP {r_pre.status_code}"
        assert resp.json() == r_pre.json(), "A4 双挂点 body 不一致（同一 handler 应恒同）"
        health = resp.json()
        assert health["status"] == "ok"
        scenarios.append(
            {
                "id": "A4",
                "title": "存活探针（/health 双挂同 body）",
                "goal": "/health 根与前缀挂点公开 200 且 body 相同（记录实际快照绑定状态）",
                "status": health["status"],
                "head_sha": health["head_sha"],
                "snapshot_sha": health["snapshot_sha"],
                "dual_mount_root_status": resp.status_code,
                "dual_mount_prefixed_status": r_pre.status_code,
            }
        )
    finally:
        client.close()

    # ---- A5：三角色行级差异（gold-146 载体：hq_admin 全量 vs branch_manager 本分支）----
    client, _ = _live_client()
    try:
        gold146 = load_gold(GOLD146_Q_ID)
        r_hq = client.post(
            f"{API}/ask",
            json={"question": gold146["question"]},
            headers=_auth(sign_token("hq_admin", {})),
        )
        assert r_hq.status_code == 200, f"A5 hq_admin /ask HTTP {r_hq.status_code}"
        hq = r_hq.json()
        assert hq["kind"] == "answer", f"A5 hq_admin 期望 answer，实际 {hq['kind']}"
        assert result_hash(list(hq["rows"])) == gold146["result_hash"][:12], (
            "A5 hq_admin（1=1 注入）EX 与 gold-146 锚定不一致（数据漂移？）"
        )
        hq_rows, columns = list(hq["rows"]), hq["columns"]
        # 分支值取总部 Top 实测值（rls-verify 同源：无空格值才可安全注入）
        branch = pick_branch_value(hq_rows, columns)
        r_bm = client.post(
            f"{API}/ask",
            json={"question": gold146["question"]},
            headers=_auth(sign_token("branch_manager", {"branch": branch})),
        )
        assert r_bm.status_code == 200, f"A5 branch_manager /ask HTTP {r_bm.status_code}"
        bm = r_bm.json()
        assert bm["kind"] == "answer", f"A5 branch_manager 期望 answer，实际 {bm['kind']}"
        bm_rows = list(bm["rows"])
        idx = columns.index("Branch")
        assert {r[idx] for r in bm_rows} == {branch}, "A5 行级过滤失效：branch_manager 见多分支"
        hq_branch_rows = [r for r in hq_rows if r[idx] == branch]
        assert hq_branch_rows, "A5 载体数据异常：hq Top 结果无该分支行"
        assert all(r in bm_rows for r in hq_branch_rows), "A5 本分支 Top 行未全部可见（漏行）"
        assert len(bm_rows) < len(hq_rows), "A5 行级过滤未见差异（branch_manager 行数未减少）"
        effect = str(bm["explanation"]["policy_effect"])
        assert "branch_manager" in effect and "rp_branch_visible" in effect, (
            f"A5 生效句缺角色/策略名：{effect}"
        )
        assert branch not in effect, "A5 条件值泄漏到 explanation（0011 口径）"
        scenarios.append(
            {
                "id": "A5",
                "title": "三角色行级差异（gold-146 载体）",
                "goal": (
                    "hq_admin 全量 vs branch_manager 本分支：过滤生效 + 策略名可见且条件值不外泄"
                ),
                "question": gold146["question"],
                "branch": branch,
                "hq": {
                    "row_count": len(hq_rows),
                    "rows_top5": hq_rows[:5],
                    "result_hash": result_hash(hq_rows),
                    "gold_result_hash_prefix": gold146["result_hash"][:12],
                    "policy_effect": hq["explanation"]["policy_effect"],
                },
                "branch_manager": {
                    "row_count": len(bm_rows),
                    "rows_top5": bm_rows[:5],
                    "policy_effect": effect,
                },
            }
        )
    finally:
        client.close()

    # ---- A6：会话身份冲突（同 session_id 换 token → 422）----
    client, _ = _live_client()
    try:
        sid = "api-verify-a6-conflict"
        gold146_q = load_gold(GOLD146_Q_ID)["question"]
        # 首角色用实测存在的分支值（与 A5/rls-verify 同载体）；422 断言只依赖
        # 两次 claims 指纹不同，分支值真假不影响冲突语义
        r1 = client.post(
            f"{API}/ask",
            json={"question": gold146_q, "session_id": sid},
            headers=_auth(sign_token("branch_manager", {"branch": "uHtbMrIxbLVfWHFhCIeAnTu"})),
        )
        assert r1.status_code == 200, f"A6 首绑 /ask HTTP {r1.status_code}"
        r2 = client.post(
            f"{API}/ask",
            json={"question": gold146_q, "session_id": sid},
            headers=_auth(sign_token("hq_admin", {})),
        )
        assert r2.status_code == 422, f"A6 换身份期望 422，实际 {r2.status_code}"
        detail = r2.json()["detail"]
        assert "会话身份冲突" in detail, f"A6 422 文案不符：{detail}"
        scenarios.append(
            {
                "id": "A6",
                "title": "会话身份冲突（HTTP 422）",
                "goal": "同 session_id 换 token → 422 冲突文案（C2 指纹绑定生效）",
                "session_id": sid,
                "first_role": "branch_manager",
                "second_role": "hq_admin",
                "second_status": r2.status_code,
                "detail": detail,
            }
        )
    finally:
        client.close()

    # ---- A7：零售档（域内策略按模型声明）+ 跨域身份拒绝（ADR-0021 真链证据）----
    client, _ = _live_client(domain="retail")
    try:
        ca_token = sign_token(
            "category_analyst",
            {"region": RETAIL_REGION, "categories": list(RETAIL_CATEGORIES)},
        )
        r_ca = client.post(
            f"{API}/ask",
            json={"question": RETAIL_QUESTION, "model": "retail"},
            headers=_auth(ca_token),
        )
        assert r_ca.status_code == 200, f"A7a category_analyst /ask HTTP {r_ca.status_code}"
        ca = r_ca.json()
        assert ca["kind"] == "answer", f"A7a 期望 answer，实际 {ca['kind']}"
        ca_rows, ca_columns = list(ca["rows"]), ca["columns"]
        assert ca_rows, "A7a 零售受限结果为空（载体数据异常？）"
        cat_idx = ca_columns.index("i_category")
        cats = {r[cat_idx] for r in ca_rows}
        assert cats and cats <= set(RETAIL_CATEGORIES), f"A7a 品类越出受限集：{cats}"
        ca_effect = str(ca["explanation"]["policy_effect"])
        assert "category_analyst" in ca_effect and "rp_dept_visible" in ca_effect, (
            f"A7a 生效句缺角色/策略名：{ca_effect}"
        )
        scenarios.append(
            {
                "id": "A7a",
                "title": "零售档品类受限（category_analyst）",
                "goal": "retail model + TN + Shoes/Electronics → 结果品类 ⊆ 受限集",
                "question": RETAIL_QUESTION,
                "claims": {"region": RETAIL_REGION, "categories": list(RETAIL_CATEGORIES)},
                "row_count": len(ca_rows),
                "categories_seen": sorted(cats),
                "policy_effect": ca_effect,
            }
        )

        # A7c 零售 hq_admin：域 → 策略来自 atlas_retail 的 default_row_policy
        # （rp_dept_visible）——ADR-0021 判据 11，修复旧 ROLE_DIRECTORY 写死
        # rp_branch_visible 导致的跨域误报（谓词 1=1 无过滤故结果对、归因错）
        r_hq = client.post(
            f"{API}/ask",
            json={"question": RETAIL_QUESTION, "model": "retail"},
            headers=_auth(sign_token("hq_admin", {})),
        )
        assert r_hq.status_code == 200, f"A7c 零售 hq_admin /ask HTTP {r_hq.status_code}"
        hq_retail = r_hq.json()
        assert hq_retail["kind"] == "answer", f"A7c 期望 answer，实际 {hq_retail['kind']}"
        hq_effect = str(hq_retail["explanation"]["policy_effect"])
        assert "hq_admin" in hq_effect and "rp_dept_visible" in hq_effect, (
            f"A7c 生效句缺角色/策略名：{hq_effect}"
        )
        assert "rp_branch_visible" not in hq_effect, f"A7c 跨域策略名误报：{hq_effect}"
        scenarios.append(
            {
                "id": "A7c",
                "title": "零售档策略名按域（hq_admin）",
                "goal": "retail model + hq_admin → policy_effect 报 rp_dept_visible（判据 11）",
                "question": RETAIL_QUESTION,
                "row_count": hq_retail["row_count"],
                "policy_effect": hq_effect,
            }
        )
    finally:
        client.close()

    # A7b 跨域身份拒绝：region_manager（零售策略角色）× finance model →
    # 身份策略解析失败（域不匹配）→ kind=error，零 SQL 达执行器（ADR-0021
    # 判据 12；旧行为是 Guard 兜底拒绝（blocked），新行为在 Guard 之前按域拒绝）
    client, executor = _live_client()
    try:
        gold146_q = load_gold(GOLD146_Q_ID)["question"]
        r_x = client.post(
            f"{API}/ask",
            json={"question": gold146_q},
            headers=_auth(sign_token("region_manager", {"region": RETAIL_REGION})),
        )
        assert r_x.status_code == 200, f"A7b 跨域 /ask HTTP {r_x.status_code}"
        cross = r_x.json()
        assert cross["kind"] == "error", f"A7b 期望 error，实际 {cross['kind']}"
        reason = str(cross["error"] or "")
        assert "域不匹配" in reason, f"A7b 拒绝原因缺域不匹配证据：{reason}"
        assert executor.calls == [], f"A7b Guard 之前即拒：零 SQL 应达执行器，实际 {executor.calls}"
        scenarios.append(
            {
                "id": "A7b",
                "title": "跨域身份拒绝（region_manager × finance）",
                "goal": (
                    "身份策略解析失败：域不匹配 → kind=error 且零 SQL 达执行器（Guard 之前即拒）"
                ),
                "question": gold146_q,
                "claims": {"region": RETAIL_REGION},
                "kind": cross["kind"],
                "error": reason,
                "executed_sql_count": len(executor.calls),
            }
        )
    finally:
        client.close()

    # ---- A8：治理面一轮（8 集合全 200）+ 两桶独立（ADR-0022 判据 4/5 真链侧）----
    # 注入面刻意非对称：业务桶 3/60 极小、治理桶 10_000/60 宽松。若两桶共享实例，
    # 先发的 8 次治理请求必然先把业务桶打爆（第 4 次即 429）；全 200 且其后业务
    # 请求仍 200，即证明两桶独立（决策 ⑥）。治理面只读文件与产物目录，不触发
    # agent 构造（决策 ④：本场景注入的 agent 全程零调用）。
    meta = json.loads(SNAPSHOT_META.read_text(encoding="utf-8"))
    executor = CountingExecutor()
    agent = DataAgent(executor=executor, budget=build_budget(meta), snapshot_meta=meta)
    client = TestClient(
        create_app(
            agent_factory=lambda _model: agent,
            audit=AuditLog(enabled=False),
            rate_limiter=RateLimiter(max_requests=3, window_seconds=60),
            governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
        )
    )
    try:
        gov_endpoints = [
            ("models", f"{API}/governance/models", None),
            ("metrics", f"{API}/governance/metrics", {"model": "finance"}),
            ("dimensions", f"{API}/governance/dimensions", {"model": "finance"}),
            ("synonyms", f"{API}/governance/synonyms", {"locale": "zh_cn"}),
            ("values", f"{API}/governance/values", None),
            ("policies", f"{API}/governance/policies", None),
            ("reports", f"{API}/governance/reports", None),
            ("snapshots", f"{API}/governance/snapshots", None),
        ]
        gov_status: dict[str, int] = {}
        gov_kind: dict[str, str] = {}
        for name, url, params in gov_endpoints:
            r = client.get(url, params=params, headers=_auth(token))
            assert r.status_code == 200, f"A8 governance/{name} HTTP {r.status_code}"
            body = r.json()
            assert body["kind"] == f"governance.{name}", (
                f"A8 governance/{name} 信封 kind 异常：{body['kind']}"
            )
            gov_status[name], gov_kind[name] = r.status_code, body["kind"]
        r_ask = client.post(
            f"{API}/ask", json={"question": gold102["question"]}, headers=_auth(token)
        )
        assert r_ask.status_code == 200, (
            f"A8 治理请求后业务面 HTTP {r_ask.status_code}——两桶串了（业务桶被治理消耗）"
        )
        scenarios.append(
            {
                "id": "A8",
                "title": "治理面一轮 + 两桶独立",
                "goal": "8 集合全 200（信封 kind=governance.<名>）且治理请求不消耗业务桶（判据 4）",
                "business_bucket_capacity": 3,
                "governance_bucket_capacity": 10_000,
                "governance_status": gov_status,
                "governance_kind": gov_kind,
                "business_ask_after_governance_status": r_ask.status_code,
            }
        )
    finally:
        client.close()

    # ---- A9：/plan/execute 真链（Plan 直接执行，EX 与快照一致；ADR-0022 决策 ③）----
    client, executor = _live_client()
    try:
        plan_resp = client.post(
            f"{API}/plan", json={"question": gold102["question"]}, headers=_auth(token)
        )
        assert plan_resp.status_code == 200, f"A9 /plan HTTP {plan_resp.status_code}"
        plan = plan_resp.json()["plan"]
        ex_resp = client.post(f"{API}/plan/execute", json=plan, headers=_auth(token))
        assert ex_resp.status_code == 200, f"A9 /plan/execute HTTP {ex_resp.status_code}"
        body = ex_resp.json()
        assert body["kind"] == "answer", f"A9 期望 answer，实际 {body['kind']}"
        assert body["session_id"], "A9 缺省 session_id 必须回显（一次性随机键，代价⑥）"
        assert body["row_count"] == plan["limit"], f"A9 行数 ≠ LIMIT：{body['row_count']}"
        assert executor.calls, "A9 answer 必须到达真实执行器"
        assert result_hash(list(body["rows"])) == gold102["result_hash"][:12], (
            "A9 行集 hash 与 gold-102 锚定不一致（EX 漂移）"
        )
        scenarios.append(
            {
                "id": "A9",
                "title": "Plan 直接执行（/plan/execute 真链）",
                "goal": "/plan 产物原样回填执行：kind=answer + EX 与锁定快照一致",
                "plan": plan,
                "kind": body["kind"],
                "question_text": body["question"],
                "row_count": body["row_count"],
                "rows_sample": body["rows"][:3],
                "result_hash": result_hash(list(body["rows"])),
                "gold_result_hash_prefix": gold102["result_hash"][:12],
                "session_id_echoed": body["session_id"],
                "executor_calls": len(executor.calls),
            }
        )
    finally:
        client.close()

    # ---- A10a：hq_admin /analyze 成功路径（analyze → 四步 Guard/RLS → 贡献响应）----
    # 独立 client：7c966e9 meta + 显式资格证据（R10-4）；A1~A9 的 b933e20 锚定不动
    client, executor = _analysis_client()
    try:
        r_hq = client.post(
            f"{API}/analyze",
            json={"question": ANALYSIS_Q},
            headers=_auth(sign_token("hq_admin", {})),
        )
        assert r_hq.status_code == 200, f"A10a /analyze HTTP {r_hq.status_code}"
        body = r_hq.json()
        assert body["kind"] == "answer", f"A10a 期望 answer，实际 {body['kind']}"
        analysis = body["analysis"]
        assert analysis is not None, "A10a 分析响应缺 analysis 投影"
        assert analysis["status"] == "ok", f"A10a 期望分析 ok，实际 {analysis['status']}"
        steps = analysis["steps"]
        assert len(steps) == 4, f"A10a 期望恰 4 步，实际 {len(steps)}"
        assert [s["role"] for s in steps] == [
            "baseline_total",
            "current_total",
            "current_by_dimension",
            "baseline_by_dimension",
        ], f"A10a 步序 role 不符：{[s['role'] for s in steps]}"
        assert all(s["sql"] for s in steps), "A10a 四步必须全带 SQL"
        assert executor.calls, "A10a 必须有 SQL 到达真实执行器"
        assert len(executor.calls) == 4, f"A10a 执行器调用数应恰 4：{len(executor.calls)}"
        assert all("LIMIT" in sql.upper() for sql in executor.calls), "A10a 每条 SQL 缺 LIMIT"
        assert all("2013" in sql for sql in executor.calls), "A10a 每条 SQL 缺 2013 时间约束"
        assert all("1 = 1" in sql for sql in executor.calls), (
            "A10a 每条 SQL 缺 hq_admin 谓词渲染（1 = 1）"
        )
        totals = analysis["totals"]
        assert totals is not None, "A10a 成功路径必须有 totals"
        assert all(totals[k] is not None for k in ("baseline", "current", "delta")), (
            f"A10a totals 三键必须齐且非 null：{totals}"
        )
        # 父轮形态四件套（serving/api.py 决策⑥）：不冒充单 SQL 结果
        assert body["sql"] is None, "A10a 父轮不得携带 sql"
        assert body["explanation"] is None, "A10a 父轮不得携带 explanation"
        assert body["columns"] == [] and body["rows"] == [], "A10a 父轮不得携带行集"
        assert body["row_count"] == 0, f"A10a 父轮 row_count 应为 0：{body['row_count']}"
        assert body["metric"] == analysis["metric"], "A10a 父轮 metric 应与计划一致"
        scenarios.append(
            {
                "id": "A10a",
                "title": "多步贡献分析（hq_admin /analyze）",
                "goal": (
                    "analyze → 四步 Guard/RLS → 贡献响应：四步全 SQL"
                    "（LIMIT+2013+1 = 1）+ totals 三键齐 + 父轮不冒充行集"
                ),
                "question": ANALYSIS_Q,
                "kind": body["kind"],
                "analysis_status": analysis["status"],
                "steps": len(steps),
                "roles": [s["role"] for s in steps],
                "totals_keys": sorted(totals),
                "items_count": len(analysis["items"]),
                "executor_calls": len(executor.calls),
            }
        )
    finally:
        client.close()

    # ---- A10b：真实受限角色 /analyze（blocked 即四步 Guard/RLS 在受限角色的真实证据，
    #      如实断言，不拿 hq_admin 冒充、不断言成功贡献）----
    client, executor = _analysis_client()
    try:
        bm_token = sign_token("branch_manager", {"branch": BRANCH})
        sid = "api-verify-a10b-bm"
        r_bm = client.post(
            f"{API}/analyze",
            json={"question": ANALYSIS_Q, "session_id": sid},
            headers=_auth(bm_token),
        )
        assert r_bm.status_code == 200, f"A10b /analyze HTTP {r_bm.status_code}"
        body = r_bm.json()
        assert body["kind"] == "blocked", f"A10b 期望 blocked，实际 {body['kind']}"
        analysis = body["analysis"]
        assert analysis is not None, "A10b 分析响应缺 analysis 投影"
        assert analysis["reason_code"] == "guard_blocked", (
            f"A10b 原因码不符：{analysis['reason_code']}"
        )
        block_reason = body["block_reason"] or ""
        assert block_reason, "A10b blocked 轮必须给 block_reason"
        assert "SELECT" not in block_reason.upper(), (
            f"A10b block_reason 泄漏 SQL 文本：{block_reason}"
        )
        steps = analysis["steps"]
        assert len(steps) == 1, f"A10b 被拒即裁剪：期望恰 1 步，实际 {len(steps)}"
        assert steps[0]["role"] == "baseline_total" and steps[0]["kind"] == "blocked", (
            f"A10b 首步形态不符：{steps[0]}"
        )
        assert steps[0]["sql"] is None, "A10b 被拒 SQL 不出网"
        assert executor.calls == [], f"A10b 零 SQL 应达执行器，实际 {executor.calls}"
        assert analysis["totals"] is None, "A10b 不得产出 totals"
        assert body["turns_in_session"] == 1, f"A10b 轮数不多算：{body['turns_in_session']}"
        # blocked 轮后同会话普通 /ask（同 bm 身份）：会话仍可用且轮数接续
        r_follow = client.post(
            f"{API}/ask",
            json={"question": gold102["question"], "session_id": sid},
            headers=_auth(bm_token),
        )
        assert r_follow.status_code == 200, f"A10b 后续 /ask HTTP {r_follow.status_code}"
        follow = r_follow.json()
        assert follow["kind"] == "answer", f"A10b 后续期望 answer，实际 {follow['kind']}"
        assert follow["turns_in_session"] == 2, (
            f"A10b 会话轮数应接续为 2：{follow['turns_in_session']}"
        )
        scenarios.append(
            {
                "id": "A10b",
                "title": "真实受限角色（branch_manager /analyze）",
                "goal": (
                    "受限角色 /analyze → blocked（guard_blocked）+ 零 SQL 达执行器"
                    " + 同会话 /ask 仍可用（轮数接续 2）"
                ),
                "question": ANALYSIS_Q,
                "kind": body["kind"],
                "reason_code": analysis["reason_code"],
                "block_reason": block_reason,
                "steps": len(steps),
                "executor_calls": len(executor.calls),
                "turns_in_session_blocked": body["turns_in_session"],
                "followup_kind": follow["kind"],
                "followup_turns_in_session": follow["turns_in_session"],
            }
        )
    finally:
        client.close()

    # ---- A10c：同会话流（hq）：/analyze → 同 sid /ask ----
    # ---- A10d：跨身份 422（A10c 的 sid 上换 bm token /ask）----
    # 两者共用一个 client 会话：A10d 依赖 A10c 建立的会话身份（与 A7a/A7c 同模式）
    client, _ = _analysis_client()
    try:
        hq_token = sign_token("hq_admin", {})
        sid = "api-verify-a10c-hq"
        r_an = client.post(
            f"{API}/analyze",
            json={"question": ANALYSIS_Q, "session_id": sid},
            headers=_auth(hq_token),
        )
        assert r_an.status_code == 200, f"A10c /analyze HTTP {r_an.status_code}"
        an = r_an.json()
        assert an["kind"] == "answer", f"A10c 期望 answer，实际 {an['kind']}"
        assert an["turns_in_session"] == 1, f"A10c 首轮应记 1：{an['turns_in_session']}"
        r_ask = client.post(
            f"{API}/ask",
            json={"question": gold102["question"], "session_id": sid},
            headers=_auth(hq_token),
        )
        assert r_ask.status_code == 200, f"A10c 同会话 /ask HTTP {r_ask.status_code}"
        ask = r_ask.json()
        assert ask["kind"] == "answer", f"A10c /ask 期望 answer，实际 {ask['kind']}"
        assert ask["turns_in_session"] == 2, f"A10c 轮数不多算：{ask['turns_in_session']}"
        scenarios.append(
            {
                "id": "A10c",
                "title": "同会话流（analyze → 普通问数）",
                "goal": "/analyze 后同 sid /ask → answer，turns_in_session 接续为 2",
                "question": ANALYSIS_Q,
                "session_id": sid,
                "analyze_kind": an["kind"],
                "ask_kind": ask["kind"],
                "turns_in_session": ask["turns_in_session"],
            }
        )

        r_x = client.post(
            f"{API}/ask",
            json={"question": gold102["question"], "session_id": sid},
            headers=_auth(sign_token("branch_manager", {"branch": BRANCH})),
        )
        assert r_x.status_code == 422, f"A10d 期望 422，实际 {r_x.status_code}"
        detail = r_x.json()["detail"]
        assert "会话身份冲突" in detail, f"A10d 422 文案不符：{detail}"
        scenarios.append(
            {
                "id": "A10d",
                "title": "跨身份会话冲突（HTTP 422）",
                "goal": "A10c 的 sid 上换 bm token /ask → 422 会话身份冲突（C2 真链侧）",
                "session_id": sid,
                "first_role": "hq_admin",
                "second_role": "branch_manager",
                "second_status": r_x.status_code,
                "detail": detail,
            }
        )
    finally:
        client.close()

    report = {
        "schema_version": 1,
        "purpose": (
            "HTTP API 真实链路验收（ADR-0012 服务面 + ADR-0022 URL 契约 v2；"
            "与 e2e-acceptance 同数据口径）"
        ),
        "head_sha": sha,
        "snapshot_sha": json.loads(SNAPSHOT_META.read_text(encoding="utf-8"))["sha"],
        "created_at": ts,
        "engine": "真实 Doris（eval/runner.execute_sql）+ 确定性链路（engine=stub）",
        "transport": "fastapi TestClient（ASGI 全栈：pydantic 校验 + JWT + 序列化）",
        "scenarios": scenarios,
        "summary": {"total": len(scenarios), "passed": len(scenarios)},
    }
    if args.report:
        out = Path(args.report)
    else:
        out = REPO / "eval" / "reports" / f"api-acceptance-{sha}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    print(f"\n[报告] {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:  # noqa: BLE001 - 验收门禁：任何场景失败都以非 0 退出
        print(f"\n[验收失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
