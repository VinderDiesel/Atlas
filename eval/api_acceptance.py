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

注入口径：与 e2e_acceptance.py 同构——真执行器（eval/runner.execute_sql）+ 锁定
快照（data/snapshots/7d48dcb.meta.json 硬编码，数据装载 commit 的指纹；快照规则
见 data/snapshots/README.md）。不走 create_live_agent 的「当前 HEAD meta」校验：
代码 commit 后 HEAD 无 meta 属常态（数据未重装），api-verify 验证的是 HTTP 面 +
真数据链等价性；503 语义已由 tests/test_api.py 契约覆盖。

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

from agent.graph import DataAgent
from eval.runner import build_budget, execute_sql, git_short_sha
from serving.api import create_app
from serving.auth import sign_token

REPO = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO / "eval" / "gold"
SNAPSHOT_META = REPO / "data/snapshots" / "7d48dcb.meta.json"

# 场景问句与锚点（与 eval/gold/ 同源，防文档问句漂移）
GOLD102_Q_ID = "gold-102"  # 命中：按分支统计 2013 年佣金收入 Top5
GOLD104_Q_ID = "gold-104"  # 歧义：最近交易情况怎么样（反问样本）
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE")


def load_gold(gold_id: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((GOLD_DIR / f"{gold_id}.json").read_text(encoding="utf-8"))
    return doc


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


def _live_client() -> tuple[TestClient, CountingExecutor]:
    """真链注入面：真 Doris 执行器 + 锁定快照预算（e2e 同构），独立 Agent 计数。"""
    meta = json.loads(SNAPSHOT_META.read_text(encoding="utf-8"))
    executor = CountingExecutor()
    agent = DataAgent(executor=executor, budget=build_budget(meta), snapshot_meta=meta)
    return TestClient(create_app(agent_factory=lambda: agent)), executor


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
