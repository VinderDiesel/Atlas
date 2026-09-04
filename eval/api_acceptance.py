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
    A4 存活      ：/health 公开 200（记录 head_sha 与 snapshot_sha 实际值）
    A5 三角色差异：gold-146 经 /ask × hq_admin / branch_manager（分支取总部 Top
                     实测值，serving/rls_verify.py 同源逻辑）——行级过滤生效
                     （branch_manager 行数减少 + 全行分支匹配 + 本分支 Top 行不
                     漏）+ explanation 含策略名 rp_branch_visible 且不含条件值
                     （0011 不外泄）；hq_admin EX 与 gold-146 锚定 hash 一致
    A6 会话冲突  ：同 session_id 换 token（branch_manager → hq_admin）→ 422
                     「会话身份冲突」（C2 指纹绑定在 HTTP 层生效）
    A7 零售+跨域 ：retail model + category_analyst（TN + Shoes/Electronics）→
                     结果品类 ⊆ 受限集 + explanation 含 rp_dept_visible；
                     region_manager × finance model → kind=blocked（Guard 无
                     join 路径拒绝——0011 决策 4 不做 API 域校验表的真链证据）

注入口径：与 e2e_acceptance.py 同构——真执行器（eval/runner.execute_sql）+ 锁定
快照（2026-09-05 P7 后统一 29 表全量数据版本，最新入库锁定指纹 b933e20；A1-A4 历史
用 7d48dcb（25 表 finance 域）——finance 数据未变（make eval b933e20 双域全绿证明），
仅快照覆盖新增零售 4 表，hash 锚定断言不移动）。不走 create_live_agent 的「当前 HEAD
meta」校验：代码 commit 后 HEAD 无 meta 属常态（数据未重装），api-verify 验证的是
HTTP 面 + 真数据链等价性；503 语义已由 tests/test_api.py 契约覆盖。
A7 零售档与 serving/rls_verify.py 零售域同 meta 同问句同 claims（载体单源互证）。

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
from eval.runner import DOMAIN_MODELS, build_budget, execute_sql, git_short_sha
from serving.api import create_app
from serving.audit import AuditLog
from serving.auth import sign_token
from serving.rls_verify import (  # 零售载体与分支取值单源（rls-verify 同载体互证）
    RETAIL_CATEGORIES,
    RETAIL_QUESTION,
    RETAIL_REGION,
    pick_branch_value,
)

REPO = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO / "eval" / "gold"
SNAPSHOT_META = REPO / "data/snapshots" / "b933e20.meta.json"

# 场景问句与锚点（与 eval/gold/ 同源，防文档问句漂移）
GOLD102_Q_ID = "gold-102"  # 命中：按分支统计 2013 年佣金收入 Top5
GOLD104_Q_ID = "gold-104"  # 歧义：最近交易情况怎么样（反问样本）
GOLD146_Q_ID = "gold-146"  # RLS 载体：按分支和客户等级统计 2015 年交易额 Top5
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE")


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
        agent = DataAgent(model=model, executor=executor, budget=build_budget(meta), snapshot_meta=meta)
    else:
        raise ValueError(f"未知域：{domain!r}")
    return (
        TestClient(create_app(agent_factory=lambda _model: agent, audit=AuditLog(enabled=False))),
        executor,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
            "/plan", json={"question": gold102["question"]}, headers=_auth(token)
        )
        assert plan_resp.status_code == 200, f"A1 /plan HTTP {plan_resp.status_code}"
        plan_body = plan_resp.json()
        assert plan_body["kind"] == "plan", f"A1 期望 kind=plan，实际 {plan_body['kind']}"
        plan = plan_body["plan"]
        assert plan["metric"] == gold102["expected_metric"], "A1 metric 与 gold 标注不一致"

        comp_resp = client.post("/compile", json=plan, headers=_auth(token))
        assert comp_resp.status_code == 200, f"A1 /compile HTTP {comp_resp.status_code}"
        sql = comp_resp.json()["sql"]
        upper = sql.upper()
        assert not any(
            re.search(rf"\b{kw}\b", upper) for kw in WRITE_KEYWORDS
        ), f"A1 出口 SQL 含写语句：{sql}"
        assert "LIMIT" in upper, "A1 出口 SQL 缺 LIMIT（Guard 强制上限形态）"

        ask_resp = client.post("/ask", json={"question": gold102["question"]}, headers=_auth(token))
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
        resp = client.post("/ask", json={"question": gold104["question"]}, headers=_auth(token))
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
        no_token = client.post("/ask", json={"question": gold102["question"]})
        forged = client.post(
            "/ask",
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

    # ---- A4：/health 公开面（如实记录当前 HEAD 的快照绑定状态）----
    client, _ = _live_client()
    try:
        resp = client.get("/health")
        assert resp.status_code == 200, f"A4 /health HTTP {resp.status_code}"
        health = resp.json()
        assert health["status"] == "ok"
        scenarios.append(
            {
                "id": "A4",
                "title": "存活探针",
                "goal": "/health 公开 200（记录实际快照绑定状态）",
                "status": health["status"],
                "head_sha": health["head_sha"],
                "snapshot_sha": health["snapshot_sha"],
            }
        )
    finally:
        client.close()

    # ---- A5：三角色行级差异（gold-146 载体：hq_admin 全量 vs branch_manager 本分支）----
    client, _ = _live_client()
    try:
        gold146 = load_gold(GOLD146_Q_ID)
        r_hq = client.post(
            "/ask", json={"question": gold146["question"]}, headers=_auth(sign_token("hq_admin", {}))
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
            "/ask",
            json={"question": gold146["question"]},
            headers=_auth(sign_token("branch_manager", {"branch": branch})),
        )
        assert r_bm.status_code == 200, f"A5 branch_manager /ask HTTP {r_bm.status_code}"
        bm = r_bm.json()
        assert bm["kind"] == "answer", f"A5 branch_manager 期望 answer，实际 {bm['kind']}"
        bm_rows = list(bm["rows"])
        idx = columns.index("Branch")
        assert {r[idx] for r in bm_rows} == {branch}, f"A5 行级过滤失效：branch_manager 见多分支"
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
                "goal": "hq_admin 全量 vs branch_manager 本分支：过滤生效 + 策略名可见且条件值不外泄",
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
            "/ask",
            json={"question": gold146_q, "session_id": sid},
            headers=_auth(sign_token("branch_manager", {"branch": "uHtbMrIxbLVfWHFhCIeAnTu"})),
        )
        assert r1.status_code == 200, f"A6 首绑 /ask HTTP {r1.status_code}"
        r2 = client.post(
            "/ask",
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

    # ---- A7：零售档行级受限 + 跨域 Guard 拒绝（0011 决策 4 真链证据）----
    client, _ = _live_client(domain="retail")
    try:
        ca_token = sign_token(
            "category_analyst",
            {"region": RETAIL_REGION, "categories": list(RETAIL_CATEGORIES)},
        )
        r_ca = client.post(
            "/ask",
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
    finally:
        client.close()

    # A7b 跨域拒绝：region_manager（零售策略角色）× finance model → Guard 无 join 路径
    client, _ = _live_client()
    try:
        gold146_q = load_gold(GOLD146_Q_ID)["question"]
        r_x = client.post(
            "/ask",
            json={"question": gold146_q},
            headers=_auth(sign_token("region_manager", {"region": RETAIL_REGION})),
        )
        assert r_x.status_code == 200, f"A7b 跨域 /ask HTTP {r_x.status_code}"
        cross = r_x.json()
        assert cross["kind"] == "blocked", f"A7b 期望 blocked，实际 {cross['kind']}"
        reason = str(cross["block_reason"] or "")
        assert "UnsafeQuery" in reason, f"A7b 拒绝原因缺 Guard 证据：{reason}"
        scenarios.append(
            {
                "id": "A7b",
                "title": "跨域 Guard 拒绝（region_manager × finance）",
                "goal": "不做 API 域校验表：谓词无 join 路径由 Guard 拒绝（kind=blocked）",
                "question": gold146_q,
                "claims": {"region": RETAIL_REGION},
                "kind": cross["kind"],
                "block_reason": reason,
            }
        )
    finally:
        client.close()

    report = {
        "schema_version": 1,
        "purpose": "HTTP API v1 真实链路验收（ADR-0012；与 e2e-acceptance 同数据口径）",
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
