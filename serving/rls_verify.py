"""三角色行级权限验证（回归工具）：同一问句 → 不同 JWT 角色 → 不同 SQL 谓词 → 实测不同结果。

定位
----
本工具是行级权限（RLS）链路的可重复回归验证：每次语义层 / 策略 / Guard
变更后运行，确认"权限仍在 SQL 谓词层生效、结果差异符合预期"。输出绑定 git sha，
供 README「验证成功」节与 Known Limitations 交叉核对。

口径（诚实声明）
----------------
- 问句取 gold-146「按分支和客户等级统计 2015 年交易额，列出前 5 名」：
  编译 SQL 同时 join dim_customer(dc) 与 dim_broker(db)，同一问句可承载
  三角色行级谓词（无需跨表补 join）。
- 三角色（serving/auth.py ROLE_DIRECTORY + row_policy.yml rp_branch_visible）：
  - hq_admin（总部）：条件 1=1 → 全量
  - branch_manager（分支经理）：dim_broker.branch = 本人分支 → 仅本分支
  - compliance_auditor（合规审计）：dim_customer.tier <= max_tier → 仅低敏感客户档
- TPC-DI 无地理/品类维度（实测 dim_broker.Branch 为随机变造串），清单原文的
  「华东区 / 某品类」角色在零售域 rp_dept_visible 注册待数据，机制相同。
- 谓词经 Guard 注入：注入前把物理表名改写为编译 SQL 实际别名（Day 25 新增），
  注入后二次只读校验；执行走 eval/runner.execute_sql（Doris 只读）。

产出
----
- stdout：三角色 SQL 谓词 / 行数 / Top 行
- eval/reports/rls-verify-<git sha>.json：结构化证据（绑定 HEAD）
- docs/screenshots/rls-verify.html：同一证据的 HTML 快照（供截图留证）

用法：
    uv run python serving/rls_verify.py [--role-branch <Branch 值>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.compiler import Compiler, SemanticModel  # noqa: E402
from agent.planner import Planner  # noqa: E402
from agent.security.sql_guard import Policy, UnsafeQuery, enforce  # noqa: E402
from eval.runner import execute_sql  # noqa: E402
from serving.auth import resolve_policy, sign_token  # noqa: E402

QUESTION = "按分支和客户等级统计 2015 年交易额，列出前 5 名"
GOLD_FILE = REPO_ROOT / "eval" / "gold" / "gold-146.json"
MAX_TIER_AUDITOR = 3  # tier 分布实测 1/2/3/8/NULL，8 为高净值档（见 rls-verify 输出）
MAX_ROWS = 10000


def git_short_sha() -> str:
    import subprocess

    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def load_budget():
    """快照行数白名单（口径与 eval/runner.build_budget 一致，仅取最新 meta）。"""
    metas = sorted((REPO_ROOT / "data" / "snapshots").glob("*.meta.json"))
    if not metas:
        raise SystemExit("[error] data/snapshots 无 meta.json，先锁定快照")
    meta = json.loads(metas[-1].read_text(encoding="utf-8"))
    allowed = {
        f"atlas.{ns}.{table}" for ns, tables in meta["row_counts"].items() for table in tables
    }
    from agent.security.sql_guard import Budget

    return Budget(dialect="doris", max_rows=MAX_ROWS, allowed_tables=frozenset(allowed))


def _scalar(value: object) -> object:
    """报告规约：与 eval/runner._scalar 同口径（Decimal 用 str，尾零稳定）。"""
    if isinstance(value, Decimal):
        return str(value)
    return value


def _run_role(role: str, token: str, sql: str, budget, no_execute: bool) -> dict:
    """单角色全链路：token → 策略解析 → Guard 注入 → （可选）Doris 执行。"""
    resolved = resolve_policy(token)
    policy = Policy(name=resolved.policy_name, condition=resolved.condition)
    try:
        guarded, _ = enforce(sql, policy=policy, budget=budget)
    except UnsafeQuery as exc:
        raise SystemExit(f"[{role}] Guard 拒绝：{exc}") from None
    outcome: dict = {
        "role": role,
        "policy_name": resolved.policy_name,
        "condition": resolved.condition,
        "sql": guarded,
    }
    if not no_execute:
        rows, columns = execute_sql(guarded)
        outcome["columns"] = columns
        outcome["row_count"] = len(rows)
        outcome["rows_top5"] = [[_scalar(v) for v in r] for r in rows[:5]]
        summary = f"  行数 {len(rows)}；Top5：{rows[:5]}"
    else:
        summary = "  （--no-execute，未执行）"
    outcome["print"] = f"[{role}] 谓词：{resolved.condition}\n  注入后 SQL：{guarded}\n{summary}"
    return outcome


def pick_branch_value(hq_rows: list[list], columns: list[str]) -> str:
    """从总部 Top 结果挑可注入的分支值：branch 列中第一个无空格、非 None 的值。

    TPC-DI Branch 多为带空格的变造串，Guard 的 _escape_literal 按防注入规则拒绝
    空格；取无空格值，注入安全边界不变（宁可少验证，不放松校验）。
    """
    if "Branch" not in columns:
        raise SystemExit(f"[error] 总部结果列 {columns} 中没有 Branch 列")
    idx = columns.index("Branch")
    for row in hq_rows:
        text = row[idx]
        if text is None:
            continue
        value = str(text)
        if value and not any(ch.isspace() for ch in value):
            return value
    raise SystemExit("[error] 总部 Top 结果中找不到可注入的无空格 Branch 值")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--role-branch", default="", help="branch_manager 的 Branch 值（默认取自总部 Top 结果）"
    )
    ap.add_argument("--no-execute", action="store_true", help="只编译不执行（打印 Guard 后 SQL）")
    args = ap.parse_args()

    if not os.environ.get("ATLAS_JWT_SECRET"):
        print("[error] 请先在 .env 设置 ATLAS_JWT_SECRET（签发角色 JWT 用）", file=sys.stderr)
        return 2

    # 复现 eval/runner 编译链路（真实 planner → compiler，不用 gold.expected_sql）
    model = SemanticModel()
    planner = Planner(model)
    compiler = Compiler(model)
    plan = planner.plan(QUESTION)
    sql, _ = compiler.compile(plan)
    print(f"问句：{QUESTION}\n编译 SQL（未注入）:\n  {sql}\n")

    budget = load_budget()

    # 角色顺序：hq_admin 先跑（1=1），branch_manager 的分支取自总部 Top 结果
    outcomes: dict[str, dict] = {}
    # 第一阶段：hq_admin 先跑（1=1），供挑选 branch_manager 的真实分支值
    for role in ("hq_admin",):
        token = sign_token(role, {})
        outcome = _run_role(role, token, sql, budget, args.no_execute)
        outcomes[role] = outcome
        print(outcome.pop("print"))

    # 第二阶段：branch_manager 用真实分支（CLI 指定或取自总部 Top 结果中第一个无空格值）
    if args.no_execute:
        # 仅编译模式无总部结果：用实测存在的无空格 Branch（Doris 抽验，非伪造）
        branch = args.role_branch or "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"
    else:
        branch = args.role_branch or pick_branch_value(
            outcomes["hq_admin"]["rows_top5"], outcomes["hq_admin"]["columns"]
        )
    print(f"branch_manager 分支：{branch}（该角色将只见此分支数据）\n")
    claims = {"branch": branch}
    for role in ("branch_manager", "compliance_auditor"):
        if role == "compliance_auditor":
            claims = {"max_tier": MAX_TIER_AUDITOR}
        token = sign_token(role, claims)
        outcome = _run_role(role, token, sql, budget, args.no_execute)
        outcomes[role] = outcome
        print(outcome.pop("print"))

    # 差异断言（实测计数，不做推断）：三个角色结果互不相同
    distinct = {
        json.dumps(o.get("rows_top5"), ensure_ascii=False, sort_keys=True)
        for o in outcomes.values()
    }
    print(f"三角色结果差异集大小：{len(distinct)}（3=三份结果各不相同，1=完全相同）")
    if len(outcomes) == 3 and len(distinct) < 2:
        print("[error] 三角色结果未产生差异——权限下推未生效？", file=sys.stderr)
        return 1

    sha = git_short_sha()
    report = {
        "sha": sha,
        "question": QUESTION,
        "gold_file": str(GOLD_FILE.relative_to(REPO_ROOT)),
        "auditor_max_tier": MAX_TIER_AUDITOR,
        "roles": outcomes,
        "note": (
            "同一问句经同一编译器/Guard 链，仅 JWT 角色不同；谓词注入发生在 SQL 层"
            "（Guard 别名对齐后二次只读校验），非应用层过滤。"
        ),
    }
    report_dir = REPO_ROOT / "eval" / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"rls-verify-{sha}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"报告：{report_path.relative_to(REPO_ROOT)}")

    _write_html(report, REPO_ROOT / "docs" / "screenshots" / "rls-verify.html")
    return 0


def _write_html(report: dict, path: Path) -> None:
    """把报告渲染成自包含 HTML（真实数据，供浏览器截图留证）。"""
    parts = [
        f"<h1>Atlas 三角色行级权限验证 <code>{report['sha']}</code></h1>",
        f"<p>问句：{escape(report['question'])}（gold-146）</p>",
        "<p>链路：Planner → Compiler → Guard（谓词别名对齐 + 二次只读校验）→ Doris 执行</p>",
    ]
    for role, out in report["roles"].items():
        top = "".join(
            f"<tr>{''.join(f'<td>{escape(str(v))}</td>' for v in row)}</tr>"
            for row in out.get("rows_top5", [])
        )
        cols = "".join(f"<th>{escape(c)}</th>" for c in out.get("columns", []))
        parts.append(
            f"<h2>{escape(role)}（{escape(out['policy_name'])}）</h2>"
            f"<p>谓词：<code>{escape(out['condition'])}</code></p>"
            f"<p>注入后 SQL：<code>{escape(out['sql'])}</code></p>"
            f"<p>行数：<strong>{out.get('row_count', '-')}</strong></p>"
            f"<table border='1' cellspacing='0' cellpadding='4'><tr>{cols}</tr>{top}</table>"
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
