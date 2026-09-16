"""Day 27 新发布指标的可重复编译 + Doris 实测验证工具。

针对审核发布（2026-09-02）进入语义层的 5 个派生指标，逐项走
真实链路：SemanticModel 读取 YAML → Plan → Compiler 编译（确定性）
→ Guard 只读校验（ADR-0003 链）→ Doris 执行，记录实测值。

定位是**可重复回归的验证工具**：每次语义层变更后可重跑，
输出绑定 git sha 的证据产物，供发布记录引用。指标口径若有变更，
先过 make lint 与契约测试，再重跑本工具刷新证据。

产出
----
- stdout：每指标编译 SQL / Guard 通过 / Doris 实测值
- eval/reports/metrics-verify-<git sha>.json：结构化证据（文件名 = 代码 HEAD；内含
  snapshot_* 三键记实际绑定的快照，二者可以不同，见 ADR-0019 决策 ① 与代价 ③）
- docs/screenshots/metrics-verify.html：同一证据的 HTML 快照（供截图留证）

用法：
    uv run python serving/metrics_verify.py [--no-execute]
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.compiler import Compiler, Plan, SemanticModel  # noqa: E402
from agent.security.sql_guard import Budget, enforce  # noqa: E402
from data.identity import (  # noqa: E402
    RuntimeSnapshot,
    SnapshotUnavailable,
    git_short_sha,
    resolve_runtime_snapshot,
)
from eval.runner import execute_sql  # noqa: E402

# Day 27 审核发布的指标（审核判定见 semantic/migrations/2026-09-02-*）
DAY27_METRICS = (
    "average_trade_value",
    "average_commission_per_trade",
    "commission_rate",
    "average_holding_value",
    "average_cash_balance",
)
MAX_ROWS = 10000


def load_budget() -> tuple[Budget, RuntimeSnapshot]:
    """Guard 预算：快照行数白名单（口径与 eval/runner.build_budget 一致）。

    返回 (预算, 快照解析结果)——报告要如实记下数字绑在哪份快照上。改造前取
    `sorted(metas)[-1]`（字典序）而 docstring 写「取最新」：ADR-0019 背景节实测
    字典序选中 `dc4f350`（2026-09-04），按 `created_at` 应为 `a11d779`（2026-09-09）。
    """
    try:
        snapshot = resolve_runtime_snapshot()
    except SnapshotUnavailable as exc:
        raise SystemExit(f"[error] {exc}") from None
    row_counts = snapshot.meta["row_counts"]
    allowed = {f"atlas.{ns}.{table}" for ns, tables in row_counts.items() for table in tables}
    return (
        Budget(dialect="doris", max_rows=MAX_ROWS, allowed_tables=frozenset(allowed)),
        snapshot,
    )


def _scalar(value: object) -> object:
    """报告规约：与 eval/runner._scalar 同口径（Decimal 用 str，尾零稳定）。"""
    if isinstance(value, Decimal):
        return str(value)
    return value


def _verify_metric(
    name: str, compiler: Compiler, model: SemanticModel, budget: Budget, no_execute: bool
) -> dict:
    """单指标全链路：YAML 表达式 → Plan → 编译 → Guard → （可选）Doris 实测。"""
    definition = model.metrics[name]  # 语义层权威表达式（非脚本内复制）
    plan = Plan(metric=name)
    sql, _ = compiler.compile(plan)
    try:
        guarded, _ = enforce(sql, budget=budget)
    except Exception as exc:  # noqa: BLE001 - Guard 拒绝视为验证失败
        raise SystemExit(f"[{name}] Guard 拒绝：{exc}") from None

    outcome: dict = {
        "metric": name,
        "definition": definition,
        "description": model.metric_descriptions.get(name, ""),
        "sql_compiled": sql,
        "sql_guarded": guarded,
    }
    if no_execute:
        outcome["row_count"] = None
        outcome["value"] = None
        outcome["print"] = f"[{name}] （--no-execute，未执行）\n  SQL：{sql}"
        return outcome

    rows, columns = execute_sql(guarded)
    if len(rows) != 1:
        raise SystemExit(
            f"[{name}] 期望聚合返回 1 行，实际 {len(rows)} 行（列 {columns}，Top3 {rows[:3]}）"
        )
    value = rows[0][0]
    if value is None:
        raise SystemExit(f"[{name}] 聚合结果为空（NULL），检查口径与物理数据")
    outcome["columns"] = columns
    outcome["row_count"] = len(rows)
    outcome["value"] = _scalar(value)
    desc = model.metric_descriptions.get(name, "")
    outcome["print"] = f"[{name}] {desc}\n  SQL：{sql}\n  实测：{value}"
    return outcome


def render_html(outcomes: dict[str, dict], sha: str, snapshot: RuntimeSnapshot) -> str:
    """证据 → 自包含 HTML（无外链资源，供浏览器截图留证）。

    `snapshot` 用于回显数字实际绑在哪份快照上：HTML 是 docs/screenshots 的留证物，
    只写代码 HEAD 会让「绑最新快照」的数字看起来像 HEAD 的产物（ADR-0019 代价 ③）。
    """
    rows_html = []
    for name in DAY27_METRICS:
        o = outcomes[name]
        rows_html.append(
            "<tr>"
            f"<td><code>{escape(name)}</code></td>"
            f"<td>{escape(o['description'])}</td>"
            f"<td><pre>{escape(o['sql_compiled'])}</pre></td>"
            f"<td><pre>{escape(o['value'] if o['value'] is not None else '（未执行）')}</pre></td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Day 27 指标发布验证（{sha}）</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 32px; color: #1f2328; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th, td {{ border: 1px solid #d0d7de; padding: 8px 10px; text-align: left; "
"vertical-align: top; font-size: 13px; }}
th {{ background: #f6f8fa; }} code, pre {{ font-family: ui-monospace, Menlo, monospace; }}
pre {{ margin: 0; white-space: pre-wrap; font-size: 12px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; }}
.ok {{ background: #dafbe1; color: #1a7f37; }}
.meta {{ color: #656d76; font-size: 13px; }}
</style>
</head>
<body>
<h1>Day 27 指标审核发布 · 编译与实测验证</h1>
<p class="meta">git sha <code>{escape(sha)}</code> · 语义层 "
"<code>semantic/ossie/atlas_finance.ossie.yaml</code> ·
实测执行走 Doris 只读（eval/runner.execute_sql）· 语义表达式来源为 YAML 权威定义，脚本不复制口径</p>
<p class="meta">快照绑定 <code>{escape(snapshot.sha)}</code> ·
source=<code>{escape(snapshot.source)}</code> ·
bound_to_head={str(snapshot.bound_to_head).lower()} ·
created_at=<code>{escape(str(snapshot.meta.get("created_at")))}</code></p>
<p><span class="badge ok">Guard 通过 · {len(DAY27_METRICS)} 个指标实测 OK</span></p>
<table>
<thead><tr><th style="width:22%">指标</th><th style="width:30%">口径</th>"
"<th style="width:30%">编译 SQL</th><th>实测值</th></tr></thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-execute", action="store_true", help="只编译不执行（打印 Guard 后 SQL）")
    args = ap.parse_args()

    model = SemanticModel()
    missing = [name for name in DAY27_METRICS if name not in model.metrics]
    if missing:
        raise SystemExit(f"[error] 语义层缺少 Day 27 指标：{missing}（先发布到 YAML）")

    compiler = Compiler(model)
    budget, snapshot = load_budget()
    print(f"[snapshot] {snapshot.describe()}")
    outcomes: dict[str, dict] = {}
    for name in DAY27_METRICS:
        outcome = _verify_metric(name, compiler, model, budget, args.no_execute)
        outcomes[name] = outcome
        print(outcome.pop("print"))

    sha = git_short_sha()
    report = {
        "tool": "metrics-verify",
        "git_sha": sha,
        # 数字绑定的快照可以与代码 HEAD 不同（ADR-0019 决策 ① 第 3 级回退）。报告
        # 文件名用 git_sha，故必须同时记下实际快照：否则这份以 <HEAD>.json 命名的
        # 产物会被读成「HEAD 的评测结果」——代价 ③ 明令禁止的互引正是这个。
        "snapshot_sha": snapshot.sha,
        "snapshot_source": snapshot.source,
        "snapshot_bound_to_head": snapshot.bound_to_head,
        "date": "2026-09-02",
        "semantic_file": "semantic/ossie/atlas_finance.ossie.yaml",
        "metrics": outcomes,
        "status": "ok",
    }
    reports_dir = REPO_ROOT / "eval" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"metrics-verify-{sha}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{report_path.relative_to(REPO_ROOT)}")

    html = render_html(outcomes, sha, snapshot)
    shots_dir = REPO_ROOT / "docs" / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    html_path = shots_dir / "metrics-verify.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"HTML：{html_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
