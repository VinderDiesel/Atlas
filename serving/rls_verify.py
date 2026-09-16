"""双域多角色行级权限验证（回归工具）：同一问句 → 不同 JWT 角色 → 不同 SQL 谓词 → 实测不同结果。

定位
----
本工具是行级权限（RLS）链路的可重复回归验证：每次语义层 / 策略 / Guard
变更后运行，确认“权限仍在 SQL 谓词层生效、结果差异符合预期”。输出绑定 git sha，
供 README「验证成功」节与 Known Limitations 交叉核对。

口径（诚实声明）
----------------
- 金融档问句取 gold-146「按分支和客户等级统计 2015 年交易额，列出前 5 名」：
  编译 SQL 同时 join dim_customer(dc) 与 dim_broker(db)，同一问句可承载
  金融四档行级谓词（无需跨表补 join）。
- 零售档问句「2000 年按门店城市和品类统计销售额，列出前 3 名」（自定义
  载体，无 gold 样本同时 join store+item）：编译 SQL 同时 join dim_item 与
  dim_store（谓词列 dim_item.i_category / dim_store.s_state 天然可达）。
- 档数（serving/auth.py ROLE_DIRECTORY + row_policy.yml + 各域语义模型
  default_row_policy，ADR-0021 二维事实源；策略名由 run_finance / run_retail
  按域传入）：
  - 金融 4 档（策略 rp_branch_visible）：
    - hq_admin（总部）：条件 1=1 → 全量（跨域共用）
    - branch_manager（分支经理）：dim_broker.branch = 本人分支 → 仅本分支
    - broker（经纪人）：dim_broker.brokerid = 本人 brokerid → 仅本人名下
    - compliance_auditor（合规审计）：dim_customer.tier <= max_tier → 仅低敏感客户档
  - 零售 3 档（策略 rp_dept_visible）：
    - hq_admin（总部）：条件 1=1 → 全量
    - region_manager（州经理）：dim_store.s_state = 本人州 → 仅本州门店
    - category_analyst（品类分析师）：本州 + dim_item.i_category IN 本人品类集
- 零售 claims 值（region/categories）取自 eval/gold/retail/data-profile.md 实测
  值域：s_state 单州 TN（12/12）→ 州谓词在单州数据下无过滤效果（region_manager
  与 hq_admin 结果一致是**数据事实**，如实报告，不伪造差异）；差异验证由
  category_analyst（2 品类 vs 全量 10 品类）承担。
- 金融 broker 档的 brokerid 取自 2015 交易实测 Top1 的真实值，**不硬编码**
  （适配说明见 pick_broker_value docstring）。
- 谓词经 Guard 注入：注入前把物理表名改写为编译 SQL 实际别名，注入后二次
  只读校验；执行走 eval/runner.execute_sql（Doris 只读）。

产出
----
- stdout：每域各档 SQL 谓词 / 行数 / Top 行
- eval/reports/rls-verify-<git sha>.json：结构化证据（双域分节；文件名 = 代码 HEAD，
  内含 snapshot_* 三键记实际绑定的快照，二者可以不同，见 ADR-0019 决策 ① 与代价 ③）
- docs/screenshots/rls-verify.html：同一证据的 HTML 快照（供截图留证）

用法：
    uv run python serving/rls_verify.py                      # 双域（finance + retail）
    uv run python serving/rls_verify.py --domain finance     # 只跑金融档
    uv run python serving/rls_verify.py --domain retail      # 只跑零售档
    uv run python serving/rls_verify.py --role-branch <Branch 值>   # 金融档指定分支
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

from agent.compiler import Compiler, Plan, SemanticModel  # noqa: E402
from agent.planner import Planner  # noqa: E402
from agent.security.sql_guard import Policy, UnsafeQuery, enforce  # noqa: E402
from data.identity import (  # noqa: E402
    SnapshotUnavailable,
    git_short_sha,
    resolve_runtime_snapshot,
)
from eval.runner import execute_sql  # noqa: E402
from serving.auth import resolve_policy, sign_token  # noqa: E402

FINANCE_QUESTION = "按分支和客户等级统计 2015 年交易额，列出前 5 名"
FINANCE_GOLD_FILE = REPO_ROOT / "eval" / "gold" / "finance" / "gold-146.json"
MAX_TIER_AUDITOR = 3  # tier 分布实测 1/2/3/8/NULL，8 为高净值档（见 rls-verify 输出）
# broker 档的 brokerid 实测查询（Top1 交易量；并列时按 BrokerID 排序保确定性）。
# 与载体同源表：dim_broker.brokerid 即策略模板列（row_policy.yml rp_branch_visible）。
BROKER_PICK_SQL = (
    "SELECT db.BrokerID AS BrokerID, db.Branch AS Branch, COUNT(*) AS n "
    "FROM atlas.dwd.fact_trades ft "
    "INNER JOIN atlas.dwd.dim_date dd ON ft.SK_CreateDateID = dd.SK_DateID "
    "INNER JOIN atlas.dwd.dim_broker db ON ft.SK_BrokerID = db.SK_BrokerID "
    "WHERE dd.CalendarYearID = 2015 AND db.BrokerID IS NOT NULL "
    "GROUP BY db.BrokerID, db.Branch ORDER BY n DESC, db.BrokerID LIMIT 1"
)
BROKER_ID_FALLBACK = 7285  # 2015 交易量 Top1 broker（--no-execute 展示用；Doris 抽验，非伪造）

RETAIL_QUESTION = "2000 年按门店城市和品类统计销售额，列出前 3 名"
# 零售 claims 值：来自 eval/gold/retail/data-profile.md 实测值域（N1 机械转述，勿手改）
RETAIL_REGION = "TN"  # s_state 单州 TN × 12（低 SF 地理集中）
RETAIL_CATEGORIES = ("Shoes", "Electronics")  # i_category 实测 10 类中取 2
MAX_ROWS = 10000


def load_budget():
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
    from agent.security.sql_guard import Budget

    return Budget(dialect="doris", max_rows=MAX_ROWS, allowed_tables=frozenset(allowed)), snapshot


def _scalar(value: object) -> object:
    """报告规约：与 eval/runner._scalar 同口径（Decimal 用 str，尾零稳定）。"""
    if isinstance(value, Decimal):
        return str(value)
    return value


def _run_role(role: str, token: str, sql: str, budget, no_execute: bool, policy_name: str) -> dict:
    """单角色全链路：token → 策略解析 → Guard 注入 → （可选）Doris 执行。

    policy_name 由调用方按域传入（rp_branch_visible / rp_dept_visible，
    ADR-0021 决策 ③ 签名改造）——角色不在该策略内即 AuthError。
    """
    resolved = resolve_policy(token, policy_name=policy_name)
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


def pick_broker_value() -> tuple[int, str | None]:
    """从 2015 交易实测取 Top1 broker 的 (brokerid, branch)——真实值，不硬编码。

    ADR-0021 决策 ⑦ 原设想「brokerid 取自 hq_admin 全量结果」（与 branch
    取值方式同构），但实测载体结果列 = [Tier, Branch, total] **不含 brokerid**
    （同一问句约束下无法改列）→ 适配为独立实测查询（同一快照下确定性：交易量
    降序、并列按 BrokerID 排序）。branch 供「结果集 Branch 单值」断言用。
    """
    rows, _ = execute_sql(BROKER_PICK_SQL)
    if not rows:
        raise SystemExit("[error] 2015 交易实测中找不到任何 broker（快照异常？）")
    brokerid, branch = rows[0][0], rows[0][1]
    return int(brokerid), branch


def _compile_sql(model_path: Path, question: str) -> str:
    """真实 planner → compiler 编译（与 eval/runner 同链路；载体问句须可解析）。"""
    model = SemanticModel(model_path)
    planner = Planner(model)
    compiler = Compiler(model)
    plan = planner.plan(question)
    if not isinstance(plan, Plan):
        raise SystemExit(f"[error] 载体问句未解析为 Plan：{type(plan).__name__}")
    sql, _ = compiler.compile(plan)
    return sql


def _assert_distinct(outcomes: dict[str, dict], domain: str) -> bool:
    """差异断言（实测计数，不做推断）：角色结果互不相同。

    单州数据事实：零售 region_manager（州=TN）与 hq_admin 结果一致，差异集
    仍 >= 2（category_analyst 品类受限后结果不同）；只有全部相同时才报错。
    """
    distinct = {
        json.dumps(o.get("rows_top5"), ensure_ascii=False, sort_keys=True)
        for o in outcomes.values()
    }
    print(f"[{domain}] 角色结果差异集大小：{len(distinct)}（>=2 通过；1=完全相同）")
    if len(outcomes) >= 2 and len(distinct) < 2:
        print(f"[error] {domain} 各角色结果未产生差异——权限下推未生效？", file=sys.stderr)
        return False
    return True


def _assert_broker_narrowed(outcomes: dict[str, dict], broker_branch: str | None) -> bool:
    """broker 档断言（ADR-0021 判据 10 的数据事实适配版）。

    - broker 行数 < hq_admin 行数（谓词产生行级收缩）；
    - 结果集 Branch 列单值 == 该 broker 实测 branch（brokerid 唯一对应一个
      branch——谓词真实生效，非应用层过滤；载体 LIMIT 5 保证 rows_top5 即全集）。

    适配说明（N1 如实登记）：ADR 原文断言「结果集的 brokerid 列单值」，但载体
    输出列 = [Tier, Branch, total] 不含 brokerid（同问句约束下无法改列）→ 由
    「注入 SQL 含 brokerid 谓词（落报告）+ Branch 列单值」共同承担同义验证。
    """
    broker = outcomes.get("broker", {})
    hq = outcomes.get("hq_admin", {})
    columns = broker.get("columns", [])
    n_broker, n_hq = broker.get("row_count", 0), hq.get("row_count", 0)
    narrowed = n_broker < n_hq
    print(f"[finance] broker 行数 {n_broker} < hq_admin 行数 {n_hq}：{narrowed}")
    if "Branch" not in columns:
        print(f"[error] broker 结果列 {columns} 中没有 Branch 列", file=sys.stderr)
        return False
    idx = columns.index("Branch")
    branches = {row[idx] for row in broker.get("rows_top5", [])}
    if branches != {broker_branch}:
        print(
            f"[error] broker 结果 Branch 列 {branches} ≠ 该 broker 实测 branch {broker_branch!r}",
            file=sys.stderr,
        )
        return False
    return narrowed


def run_finance(budget, args) -> tuple[dict, bool]:
    """金融档（gold-146 载体）：hq_admin / branch_manager / broker / compliance_auditor。"""
    finance_policy = "rp_branch_visible"  # 域 → 策略（ADR-0021 决策 ③：按域传入）
    model_path = REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
    sql = _compile_sql(model_path, FINANCE_QUESTION)
    print(f"[finance] 问句：{FINANCE_QUESTION}\n[finance] 编译 SQL（未注入）:\n  {sql}\n")

    # 角色顺序：hq_admin 先跑（1=1），branch_manager 的分支取自总部 Top 结果
    outcomes: dict[str, dict] = {}
    for role in ("hq_admin",):
        token = sign_token(role, {})
        outcome = _run_role(role, token, sql, budget, args.no_execute, policy_name=finance_policy)
        outcomes[role] = outcome
        print(outcome.pop("print"))

    # branch_manager 用真实分支（CLI 指定或取自总部 Top 结果中第一个无空格值）
    if args.no_execute:
        # 仅编译模式无总部结果：用实测存在的无空格 Branch（Doris 抽验，非伪造）
        branch = args.role_branch or "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"
    else:
        branch = args.role_branch or pick_branch_value(
            outcomes["hq_admin"]["rows_top5"], outcomes["hq_admin"]["columns"]
        )
    print(f"branch_manager 分支：{branch}（该角色将只见此分支数据）\n")
    for role, claims in (
        ("branch_manager", {"branch": branch}),
        ("compliance_auditor", {"max_tier": MAX_TIER_AUDITOR}),
    ):
        token = sign_token(role, claims)
        outcome = _run_role(role, token, sql, budget, args.no_execute, policy_name=finance_policy)
        outcomes[role] = outcome
        print(outcome.pop("print"))

    # broker 档（ADR-0021 决策 ⑦）：brokerid 取 2015 交易实测 Top1（真实值，不硬编码；
    # 载体结果列不含 brokerid，适配说明见 pick_broker_value）
    if args.no_execute:
        brokerid, broker_branch = BROKER_ID_FALLBACK, None
    else:
        brokerid, broker_branch = pick_broker_value()
    hint = "" if broker_branch is None else f"，branch={broker_branch}"
    print(f"broker 档：brokerid={brokerid}{hint}（该角色将只见本人 brokerid 名下数据）\n")
    token = sign_token("broker", {"brokerid": brokerid})
    outcome = _run_role("broker", token, sql, budget, args.no_execute, policy_name=finance_policy)
    outcomes["broker"] = outcome
    print(outcome.pop("print"))

    # 断言仅在执行模式有意义（--no-execute 无结果集，只展示注入 SQL）
    if args.no_execute:
        ok = True
    else:
        distinct_ok = _assert_distinct(outcomes, "finance")
        broker_ok = _assert_broker_narrowed(outcomes, broker_branch)
        ok = distinct_ok and broker_ok
    section = {
        "question": FINANCE_QUESTION,
        "gold_file": str(FINANCE_GOLD_FILE.relative_to(REPO_ROOT)),
        "auditor_max_tier": MAX_TIER_AUDITOR,
        "broker_claims": {"brokerid": brokerid},  # 执行模式=实测 Top1；--no-execute=抽验值
        "roles": outcomes,
    }
    return section, ok


def run_retail(budget, args) -> tuple[dict, bool]:
    """零售档（TPC-DS 载体）：hq_admin / region_manager / category_analyst。"""
    retail_policy = "rp_dept_visible"  # 域 → 策略（ADR-0021 决策 ③：按域传入）
    model_path = REPO_ROOT / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
    sql = _compile_sql(model_path, RETAIL_QUESTION)
    print(f"[retail] 问句：{RETAIL_QUESTION}\n[retail] 编译 SQL（未注入）:\n  {sql}\n")

    outcomes: dict[str, dict] = {}
    claims_map = {
        "hq_admin": {},
        "region_manager": {"region": RETAIL_REGION},
        "category_analyst": {
            "region": RETAIL_REGION,
            "categories": list(RETAIL_CATEGORIES),
        },
    }
    for role in ("hq_admin", "region_manager", "category_analyst"):
        token = sign_token(role, claims_map[role])
        outcome = _run_role(role, token, sql, budget, args.no_execute, policy_name=retail_policy)
        outcomes[role] = outcome
        print(outcome.pop("print"))

    # 差异断言仅在执行模式有意义（--no-execute 无结果集，只展示注入 SQL）
    ok = True if args.no_execute else _assert_distinct(outcomes, "retail")
    section = {
        "question": RETAIL_QUESTION,
        "claims": {
            "region_manager": {"region": RETAIL_REGION},
            "category_analyst": {
                "region": RETAIL_REGION,
                "categories": list(RETAIL_CATEGORIES),
            },
            # 注：SF0.1 全库单州 TN → region_manager 与 hq_admin 结果一致是数据事实
            "note": "claims 值取自 eval/gold/retail/data-profile.md 实测值域",
        },
        "roles": outcomes,
    }
    return section, ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--domain",
        choices=("finance", "retail", "all"),
        default="all",
        help="评测域（默认 all：finance/retail 双域分节，不混报）",
    )
    ap.add_argument(
        "--role-branch",
        default="",
        help="金融档 branch_manager 的 Branch 值（默认取自总部 Top 结果）",
    )
    ap.add_argument("--no-execute", action="store_true", help="只编译不执行（打印 Guard 后 SQL）")
    args = ap.parse_args()

    if not os.environ.get("ATLAS_JWT_SECRET"):
        print("[error] 请先在 .env 设置 ATLAS_JWT_SECRET（签发角色 JWT 用）", file=sys.stderr)
        return 2

    budget, snapshot = load_budget()
    print(f"[snapshot] {snapshot.describe()}")
    domains = ("finance", "retail") if args.domain == "all" else (args.domain,)
    sections: dict[str, dict] = {}
    all_ok = True
    for domain in domains:
        fn = run_finance if domain == "finance" else run_retail
        section, ok = fn(budget, args)
        sections[domain] = section
        all_ok = all_ok and ok

    sha = git_short_sha()
    report = {
        "sha": sha,
        # 文件名用代码 HEAD，而数字绑在解析出的快照上（二者可以不同，ADR-0019
        # 决策 ① 第 3 级）——三个 snapshot_* 键就是防「以 <HEAD>.json 命名却被读成
        # HEAD 的评测结果」这种互引（代价 ③）。
        "snapshot_sha": snapshot.sha,
        "snapshot_source": snapshot.source,
        "snapshot_bound_to_head": snapshot.bound_to_head,
        "domains": list(domains),
        **sections,
        "note": (
            "各域同一问句经同一编译器/Guard 链，仅 JWT 角色不同；谓词注入发生在 SQL 层"
            "（Guard 别名对齐后二次只读校验），非应用层过滤。零售档 region_manager"
            "（州=TN）与 hq_admin 结果一致：SF0.1 单州数据下州谓词无过滤效果（数据事实，"
            "差异验证由 category_analyst 品类受限承担）。"
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
    return 0 if all_ok else 1


def _write_html(report: dict, path: Path) -> None:
    """把报告渲染成自包含 HTML（真实数据，供浏览器截图留证）。"""
    parts = [
        f"<h1>Atlas 行级权限验证 <code>{report['sha']}</code></h1>",
        # 留证物上必须同屏显示代码 HEAD 与实际快照（二者可以不同，ADR-0019 代价 ③）
        f"<p>快照绑定：<code>{escape(str(report['snapshot_sha']))}</code> · "
        f"source=<code>{escape(str(report['snapshot_source']))}</code> · "
        f"bound_to_head={escape(str(report['snapshot_bound_to_head']).lower())}</p>",
        "<p>链路：Planner → Compiler → Guard（谓词别名对齐 + 二次只读校验）→ Doris 执行</p>",
    ]
    for domain in report["domains"]:
        sec = report[domain]
        parts.append(
            f"<h2>域：{escape(domain)}</h2>"
            f"<p>问句：{escape(sec['question'])}</p>"
        )
        for role, out in sec["roles"].items():
            top = "".join(
                f"<tr>{''.join(f'<td>{escape(str(v))}</td>' for v in row)}</tr>"
                for row in out.get("rows_top5", [])
            )
            cols = "".join(f"<th>{escape(c)}</th>" for c in out.get("columns", []))
            parts.append(
                f"<h3>{escape(role)}（{escape(out['policy_name'])}）</h3>"
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
