"""P1 端到端验收（回归工具）：gold-102 问句全链路 = 路由 → 选版本 → 编译 → 策略 → 执行 → trace。

定位
----
Day 28 P1 端到端验收的可重复回归载体：问句「按分支统计 2013 年佣金收入，列出前 5 名」
（gold-102）从 Planner 路由到唯一指标 commission_revenue@v1（active），经 Compiler 编译、
Guard 注入行级策略后在 Doris 实测。输出绑定 git sha 的报告 + HTML 快照，供 README /
验收记录交叉核对。本工具是验收而非演示：任一步骤断言失败即退出码 1。

口径（诚实声明）
----------------
- 载体问句与 gold-102 一致；hq_admin（策略 1=1）执行结果 sha256 必须等于 gold-102 锚定
  result_hash（EX 匹配，复用 eval.runner 同口径 result_hash）；branch_manager 注入分支
  谓词后仅见该分支行（行数 1）。
- Guard 恶意拦截门槛：10 条代表性恶意 SQL（INSERT/UPDATE/DELETE/DROP/ALTER/GRANT/
  CREATE/sleep/pg_sleep/未渲染占位符）逐条断言 UnsafeQuery（Day 19 门槛，pytest 另有
  17 例契约测试）。
- trace 为 MVP 结构化耗时事件列表（perf_counter 实测毫秒）；OTel 全链路埋点见 Day 50。
- Branch 值取自总部 Top 结果中第一个无空格值（TPC-DI 变造串，Guard 防注入边界不放松）。

产出
----
- eval/reports/p1-chain-<git sha>.json：结构化验收证据（绑定 HEAD）
- docs/screenshots/p1-chain.html：同一证据的 HTML 快照（供截图留证）
- 用法：uv run --env-file .env python serving/p1_acceptance.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.compiler import Compiler, OrderSpec, Plan, SemanticModel, TimeSpec  # noqa: E402
from agent.planner import Planner  # noqa: E402
from agent.security.sql_guard import Policy, UnsafeQuery, enforce  # noqa: E402
from eval.runner import build_budget, execute_sql, result_hash  # noqa: E402
from semantic.governance_validate import collect_metric_governance  # noqa: E402
from serving.auth import resolve_policy, sign_token  # noqa: E402

QUESTION = "按分支统计 2013 年佣金收入，列出前 5 名"
GOLD_FILE = REPO_ROOT / "eval" / "gold" / "gold-102.json"
MODEL_FILE = REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
EXPECTED_METRIC = "commission_revenue"
EXPECTED_METRIC_ID = "commission_revenue@v1"

# Day 19 门槛的 10 条代表性恶意 SQL（逐条必须被 Guard 以 UnsafeQuery 拒绝）
MALICIOUS_SQLS: list[tuple[str, str]] = [
    ("insert", "INSERT INTO atlas.dwd.fact_trades (SK_TradeID, Commission) VALUES (1, 2.0)"),
    ("update", "UPDATE atlas.dwd.fact_trades SET Commission = 0"),
    ("delete", "DELETE FROM atlas.dwd.fact_trades WHERE Commission > 0"),
    ("drop", "DROP TABLE atlas.dwd.fact_trades"),
    ("alter", "ALTER TABLE atlas.dwd.fact_trades ADD COLUMN hacked INT"),
    ("grant", "GRANT SELECT ON atlas.dwd.fact_trades TO public"),
    ("create", "CREATE TABLE atlas.dwd.evil AS SELECT 1 AS one"),
    ("sleep", "SELECT sleep(10)"),
    ("pg_sleep", "SELECT pg_sleep(10)"),
    ("benchmark", "SELECT benchmark(1000000, md5(1))"),
]


class Trace:
    """MVP 结构化 trace：phase 级耗时事件（perf_counter 实测毫秒）。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._t0 = time.perf_counter()

    def step(self, phase: str, detail: str = "") -> None:
        self._events.append({"phase": phase, "detail": detail, "ms": self._elapsed_ms()})

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 2)

    @property
    def events(self) -> list[dict]:
        return self._events


def git_short_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def load_budget():
    """快照行数白名单（与 eval/runner.build_budget 同口径，取最新 meta）。"""
    metas = sorted((REPO_ROOT / "data" / "snapshots").glob("*.meta.json"))
    if not metas:
        raise SystemExit("[error] data/snapshots 无 meta.json，先锁定快照")
    meta = json.loads(metas[-1].read_text(encoding="utf-8"))
    return build_budget(meta)


def metric_governance() -> dict:
    """读取指标 governance 记录（复用 lint 链路的 collect_metric_governance，口径一致）。"""
    for rec in collect_metric_governance([MODEL_FILE]):
        if rec.metric_name == EXPECTED_METRIC:
            return {"version": rec.version, "status": rec.status, "supersedes": rec.supersedes}
    raise SystemExit(f"[error] 语义层找不到指标 {EXPECTED_METRIC} 的 governance 记录")


def scalar(value: object) -> object:
    """报告规约：与 eval/runner._scalar 同口径（Decimal 用 str，尾零稳定）。"""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    return value


def guard_blocked(sql: str, budget) -> bool:
    """单条恶意 SQL 是否被 Guard 拒绝（UnsafeQuery）。"""
    try:
        enforce(sql, budget=budget)
    except UnsafeQuery:
        return True
    return False


def run_malicious_gate(budget) -> dict:
    """门槛第 4 项：10 条恶意 SQL 逐条断言拦截。"""
    results = []
    all_blocked = True
    for kind, sql in MALICIOUS_SQLS:
        blocked = guard_blocked(sql, budget)
        all_blocked = all_blocked and blocked
        results.append({"kind": kind, "blocked": blocked, "sql": sql})
    return {
        "total": len(MALICIOUS_SQLS),
        "blocked": sum(1 for r in results if r["blocked"]),
        "all_blocked": all_blocked,
        "cases": results,
    }


def pick_branch_value(rows: list[tuple], columns: list[str]) -> str:
    """从总部 Top 结果挑可注入的分支值（无空格、非 None，Guard 防注入边界不放松）。"""
    try:
        idx = next(i for i, c in enumerate(columns) if c.lower() == "branch")
    except StopIteration:
        raise SystemExit(f"[error] 总部结果列 {columns} 中没有 branch 列") from None
    for row in rows:
        text = row[idx]
        if text is None:
            continue
        value = str(text)
        if value and not any(ch.isspace() for ch in value):
            return value
    raise SystemExit("[error] 总部 Top 结果中找不到可注入的无空格 Branch 值")


def main() -> int:
    if not os.environ.get("ATLAS_JWT_SECRET"):
        print("[error] 请先在 .env 设置 ATLAS_JWT_SECRET（签发角色 JWT 用）", file=sys.stderr)
        return 2

    trace = Trace()
    model = SemanticModel()
    planner = Planner(model)
    compiler = Compiler(model)

    # -- 阶段 1：问句路由（门槛：问句命中唯一指标，不猜测） ---------------------
    plan = planner.plan(QUESTION)
    if isinstance(plan, Plan):
        route_ok = (
            plan.metric == EXPECTED_METRIC
            and plan.dimensions == ("Branch",)
            and plan.time == TimeSpec("year", 2013)
            and plan.limit == 5
            and plan.order_by == (OrderSpec(EXPECTED_METRIC, desc=True),)
        )
    else:
        route_ok = False
    if not route_ok:
        print(f"[fail] 问句路由不符合预期：{plan!r}", file=sys.stderr)
        return 1
    trace.step(
        "route",
        f"唯一命中 {plan.metric}，dimensions={list(plan.dimensions)}，limit={plan.limit}",
    )

    # -- 阶段 2：选中指标版本（governance：v1 active，supersedes 链头部） ----------
    gov = metric_governance()
    version_ok = gov["status"] == "active" and gov["version"] == 1
    if not version_ok:
        print(f"[fail] {EXPECTED_METRIC} governance 非 v1 active：{gov}", file=sys.stderr)
        return 1
    trace.step(
        "select_version",
        f"{EXPECTED_METRIC_ID} status={gov['status']} supersedes={gov['supersedes']}",
    )

    # -- 阶段 3：编译（断言 LIMIT/分组/年份；CompileError 即失败） -----------------
    try:
        sql, _ = compiler.compile(plan)
    except Exception as exc:  # noqa: BLE001 - 编译失败即为验收失败
        print(f"[fail] 编译失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    upper = sql.upper()
    compile_ok = "LIMIT 5" in upper and "GROUP BY" in upper and "2013" in upper
    if not compile_ok:
        print(f"[fail] 编译 SQL 缺 LIMIT 5 / GROUP BY / 2013 谓词：\n  {sql}", file=sys.stderr)
        return 1
    trace.step("compile", f"LIMIT/分组/年份断言通过，SQL {len(sql)} 字符")

    # -- 阶段 4：Guard 恶意拦截门槛（10 条全拒） -----------------------------------
    budget = load_budget()
    gate = run_malicious_gate(budget)
    if not gate["all_blocked"]:
        leaked = [c for c in gate["cases"] if not c["blocked"]]
        print(f"[fail] 恶意 SQL 未被全部拦截：{leaked}", file=sys.stderr)
        return 1
    trace.step("malicious_gate", "10 条恶意 SQL 全部被 Guard 拒绝")

    # -- 阶段 5：加策略并执行（hq_admin 全量 → EX 匹配 gold-102；branch_manager 分支） --
    gold = json.loads(GOLD_FILE.read_text(encoding="utf-8"))
    # hq_admin：策略 1=1（总部全量），执行结果必须与 gold-102 锚定 hash 一致
    resolved_hq = resolve_policy(sign_token("hq_admin", {}))
    guarded_hq, _ = enforce(
        sql,
        policy=Policy(name=resolved_hq.policy_name, condition=resolved_hq.condition),
        budget=budget,
    )
    rows_hq, columns_hq = execute_sql(guarded_hq)
    digest = result_hash(rows_hq)
    gold_match = digest == gold.get("result_hash")
    hq_ok = len(rows_hq) == 5 and gold_match
    trace.step("execute_hq", f"行数 {len(rows_hq)}，sha256 与 gold-102 锚定一致={gold_match}")

    # branch_manager：分支谓词注入（分支取自总部 Top 第一个无空格值）
    branch = pick_branch_value(rows_hq, columns_hq)
    resolved_bm = resolve_policy(sign_token("branch_manager", {"branch": branch}))
    guarded_bm, _ = enforce(
        sql,
        policy=Policy(name=resolved_bm.policy_name, condition=resolved_bm.condition),
        budget=budget,
    )
    rows_bm, columns_bm = execute_sql(guarded_bm)
    bm_ok = len(rows_bm) == 1 and str(rows_bm[0][0]) == branch
    trace.step("execute_branch", f"分支 {branch} 仅见 1 行={len(rows_bm) == 1}")

    if not (hq_ok and bm_ok):
        print(f"[fail] 执行断言失败：hq_ok={hq_ok} bm_ok={bm_ok}", file=sys.stderr)
        return 1

    sha = git_short_sha()
    report = {
        "sha": sha,
        "question": QUESTION,
        "gold_file": str(GOLD_FILE.relative_to(REPO_ROOT)),
        "metric_id": EXPECTED_METRIC_ID,
        "metric_version": gov,
        "plan": {
            "metric": plan.metric,
            "dimensions": list(plan.dimensions),
            "time": {"granularity": plan.time.granularity, "value": plan.time.value},
            "limit": plan.limit,
            "order_by": [{"column": o.column, "desc": o.desc} for o in plan.order_by],
        },
        "sql_compiled": sql,
        "malicious_gate": {k: v for k, v in gate.items() if k != "cases"},
        "malicious_cases": gate["cases"],
        "roles": {
            "hq_admin": {
                "policy": resolved_hq.policy_name,
                "condition": resolved_hq.condition,
                "sql": guarded_hq,
                "row_count": len(rows_hq),
                "columns": columns_hq,
                "rows_top5": [[scalar(v) for v in r] for r in rows_hq],
                "result_sha256": digest,
                "gold_hash_match": gold_match,
                "gold_result_hash": gold.get("result_hash"),
            },
            "branch_manager": {
                "policy": resolved_bm.policy_name,
                "condition": resolved_bm.condition,
                "branch": branch,
                "sql": guarded_bm,
                "row_count": len(rows_bm),
                "columns": columns_bm,
                "rows": [[scalar(v) for v in r] for r in rows_bm],
            },
        },
        "gates": {
            "route_unique_metric": route_ok,
            "sql_limit_and_time": compile_ok,
            "malicious_10_blocked": gate["all_blocked"],
            "gold102_hash_match": gold_match,
            "branch_policy_effective": bm_ok,
        },
        "trace": trace.events,
        "note": (
            "同一问句 → Planner 唯一路由 → commission_revenue@v1(active) → Compiler → "
            "Guard（谓词注入 + 只读二次校验）→ Doris 执行；hq_admin 结果与 gold-102 锚定 "
            "result_hash 一致（EX 匹配）；branch_manager 只见被注入分支。恶意 SQL 10 条 "
            "全拒（Day 19 门槛）。trace 为 MVP 结构化耗时事件，OTel 全链路见 Day 50。"
        ),
    }
    report_dir = REPO_ROOT / "eval" / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"p1-chain-{sha}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"报告：{report_path.relative_to(REPO_ROOT)}")

    _write_html(report, REPO_ROOT / "docs" / "screenshots" / "p1-chain.html")
    print("[ok] P1 端到端验收通过：路由/版本/编译/Guard×10/EX 匹配/分支策略全部达标")
    return 0


def _write_html(report: dict, path: Path) -> None:
    """把报告渲染成自包含 HTML（真实数据，供浏览器截图留证）。"""
    esc = escape
    plan = report["plan"]
    parts = [
        f"<h1>P1 端到端验收 <code>{report['sha']}</code></h1>",
        f"<p>问句：{esc(report['question'])}（gold-102）</p>",
        f"<p>链路：Planner → {esc(report['metric_id'])} → Compiler → Guard → Doris</p>",
        f"<p>Plan：metric={esc(plan['metric'])} dimensions={plan['dimensions']} "
        f"time={esc(str(plan['time']))} limit={plan['limit']}</p>",
        f"<p>编译 SQL：<code>{esc(report['sql_compiled'])}</code></p>",
        f"<p>Guard 恶意拦截：<strong>{report['malicious_gate']['blocked']}/"
        f"{report['malicious_gate']['total']}</strong></p>",
    ]
    for role, out in report["roles"].items():
        rows = out.get("rows_top5") or out.get("rows") or []
        header = out.get("columns") or []
        top = "".join(f"<tr>{''.join(f'<td>{esc(str(v))}</td>' for v in row)}</tr>" for row in rows)
        cols = "".join(f"<th>{esc(c)}</th>" for c in header)
        hash_line = (
            f"<p>sha256：<code>{out.get('result_sha256', '-')}</code> "
            f"gold-102 匹配：<strong>{out.get('gold_hash_match', '-')}</strong></p>"
            if "gold_hash_match" in out
            else f"<p>policy：<code>{esc(out.get('policy', ''))}</code></p>"
        )
        parts.append(
            f"<h2>{esc(role)}</h2>"
            f"<p>注入后 SQL：<code>{esc(out['sql'])}</code></p>"
            f"<p>行数：<strong>{out['row_count']}</strong></p>{hash_line}"
            f"<table border='1' cellspacing='0' cellpadding='4'><tr>{cols}</tr>{top}</table>"
        )
    tr = "".join(
        f"<tr><td>{esc(e['phase'])}</td><td>{esc(e['detail'])}</td><td>{e['ms']}</td></tr>"
        for e in report["trace"]
    )
    parts.append(
        f"<h2>trace（MVP 结构化耗时事件）</h2>"
        f"<table border='1' cellspacing='0' cellpadding='4'>"
        f"<tr><th>phase</th><th>detail</th><th>ms</th></tr>{tr}</table>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    style = "body{font-family:Menlo,monospace;margin:24px;background:#fff}"
    path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{style}</style></head>"
        f"<body>{''.join(parts)}</body></html>",
        encoding="utf-8",
    )
    print(f"HTML 快照：{path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    raise SystemExit(main())
