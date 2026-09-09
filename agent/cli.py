"""Agent 命令行入口（AGENTS.md 常用命令：make plan / make compile / ask / query）。

MVP 范围：
- plan：问句 → Plan（确定性 Planner）；歧义时打印 ClarificationRequest 并退出码 1
- compile：query.plan.json → 只读 SQL（确定性 Compiler）
- ask：多轮问数会话（Day 49）——真实 Doris + 锁定快照，Data Agent 状态机
  逐轮输出 answer / clarify / blocked / error / handoff；无参数进入交互模式
  （同一 session 连续多轮，空行退出）
- query：一步问数（本次新增）——Planner→Compiler→Guard→Doris 真连库，
  非会话、可脚本化、退出码区分 clarify/blocked/error（见 cmd_query 注释）。

Plan 的 JSON 形态（compile 输入约定）：

    {
      "metric": "commission_revenue",
      "dimensions": ["Branch"],
      "time": {"granularity": "year", "value": 2013},
      "order_by": [{"column": "commission_revenue", "desc": true}],
      "limit": 5
    }

时间 value：year/month 为 int，quarter/date 为 str（与 compiler.TimeSpec 一致）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.compiler import (
    FINANCE_MODEL,
    CompileError,
    Compiler,
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
)
from agent.factory import SnapshotUnavailable, create_live_agent
from agent.generator import Generator
from agent.planner import ClarificationRequest, Planner
from agent.security.sql_guard import Budget, BudgetExceeded, Policy, UnsafeQuery, enforce
from agent.state import TurnResult
from eval.runner import SNAPSHOT_DIR, build_budget, execute_sql
from serving.auth import AuthError, resolve_claims

REPO_ROOT = Path(__file__).resolve().parent.parent
RETAIL_MODEL = REPO_ROOT / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
# 域 → 语义模型（与 serving/api.py DOMAIN_MODEL_PATHS 同源，避免 CLI 依赖 serving 运行时）
DOMAIN_MODEL_PATHS: dict[str, Path] = {"finance": FINANCE_MODEL, "retail": RETAIL_MODEL}


def _load_dotenv() -> None:
    """从仓库根 .env 载入环境变量（仅当未设置时，显式环境优先）。

    让 `atlas query` 在任意 cwd 下都能拿到 DORIS_* 等连接，无需手动 export 或 --env-file。
    路径基于 __file__，与当前工作目录无关；.env 缺失则静默跳过。
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()

# query 退出码约定（脚本可据此分流）：
#   0 = ok（已出结果）  1 = error（编译/执行/快照缺失）  2 = clarify（歧义或 LLM 拒答）
#   3 = blocked（Guard 拒绝 / 行级策略解析失败）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CLARIFY = 2
EXIT_BLOCKED = 3


def _print_turn(result: TurnResult) -> None:
    """单轮结果的确定性摘要打印（kind 判定顺序与 turn_from_state 一致）。"""
    print(f"[{result.kind}] 第 {result.turns_in_session} 轮")
    if result.kind == "answer":
        assert result.sql is not None
        print(f"  指标={result.metric} | 行数={result.row_count} | 执行 {result.latency_ms}ms")
        print(
            f"  path={result.path} | 表={', '.join((result.explanation or {}).get('tables') or ())}"
        )
        print(f"  SQL: {result.sql}")
        for row in result.rows[:3]:
            print(f"  {list(row)}")
        if result.row_count > 3:
            print(f"  …（共 {result.row_count} 行）")
    elif result.kind == "clarify":
        assert result.clarification is not None
        for reason in result.clarification.reasons:
            print(f"  反问：{reason}")
        if result.clarification.candidates:
            print(f"  候选指标：{', '.join(result.clarification.candidates)}")
    elif result.kind == "blocked":
        print(f"  Guard 拒绝：{result.block_reason}")
    elif result.kind == "error":
        print(f"  执行故障：{result.error}")
    else:  # handoff
        print(f"  人工接管：{result.handoff_reason}")


def cmd_ask(args: argparse.Namespace) -> int:
    """多轮问数：给定问句单轮；省略则进入交互会话（同一 session 连续多轮）。"""
    try:
        agent = create_live_agent()
    except SnapshotUnavailable as exc:
        print(f"[ask] {exc}", file=sys.stderr)
        return 1
    if args.question is not None:
        _print_turn(agent.ask(args.question))
        return 0
    print("[ask] 交互会话（真实 Doris + 锁定快照；空行退出）")
    sid = f"cli-{uuid4().hex[:8]}"  # 进程内会话键（checkpointer 内存态，重启即新会话）
    try:
        while True:
            try:
                question = input("> ").strip()
            except EOFError:
                break
            if not question:
                break
            _print_turn(agent.ask(question, session_id=sid))
    except KeyboardInterrupt:
        pass
    print("[ask] 会话结束")
    return 0


def _plan_from_dict(d: dict[str, Any]) -> Plan:
    """query.plan.json → Plan（字段缺失时用 Plan 默认值，不猜）。"""
    time_d = d.get("time")
    time = TimeSpec(time_d["granularity"], time_d["value"]) if time_d else None
    filters = tuple(Filter(f["column"], f["op"], f["value"]) for f in d.get("filters", []))
    order_by = tuple(OrderSpec(o["column"], o.get("desc", False)) for o in d.get("order_by", []))
    return Plan(
        metric=d["metric"],
        dimensions=tuple(d.get("dimensions", [])),
        time=time,
        filters=filters,
        order_by=order_by,
        limit=d.get("limit", 100),
    )


def cmd_plan(args: argparse.Namespace) -> int:
    """问句 → Plan（不执行 SQL，见 AGENTS.md 常用命令约定）。"""
    model = SemanticModel(DOMAIN_MODEL_PATHS[args.domain])
    result = Planner(model).plan(args.question)
    if isinstance(result, ClarificationRequest):
        print(f"[澄清] 问句：{result.question}")
        for reason in result.reasons:
            print(f"  - {reason}")
        if result.candidates:
            print(f"  候选指标：{', '.join(result.candidates)}")
        return 1
    print(result)
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    """query.plan.json → 只读 SQL。"""
    plan = _plan_from_dict(json.loads(Path(args.plan_file).read_text(encoding="utf-8")))
    sql, _ = Compiler(SemanticModel(DOMAIN_MODEL_PATHS[args.domain])).compile(plan)
    print(sql)
    return 0


# ---------------------------------------------------------------------------
# query：一步问数（Planner→Compiler→Guard→Doris）
# ---------------------------------------------------------------------------


def _load_budget() -> Budget:
    """锁定快照表白名单预算（口径与 eval/runner.build_budget / metrics_verify 一致）。

    无快照 meta 时抛 SystemExit——query 必须绑固定快照（AGENTS.md N3/N6），
    不允许查未入快照的漂移表。
    """
    metas = sorted(SNAPSHOT_DIR.glob("*.meta.json"))
    if not metas:
        raise SystemExit("[error] data/snapshots 无 meta.json，先 make seed 锁定快照")
    return build_budget(json.loads(metas[-1].read_text(encoding="utf-8")))


def _parse_role_ctx(s: str | None) -> dict[str, str]:
    """--role-ctx 'branch=BR_A1' 或 'region=TN,categories=Shoes' → user_context。

    值统一为字符串；Guard 渲染时按列类型落引号/裸数字（见 serving/auth._literal）。
    列表值（如 categories）MVP 不在此解析——需它的角色请走 HTTP API 的 JSON 上下文。
    """
    if not s:
        return {}
    ctx: dict[str, str] = {}
    for part in s.split(","):
        key, sep, val = part.partition("=")
        if not sep:
            raise ValueError(f"--role-ctx 片段缺 '='：{part!r}（应为 k=v）")
        ctx[key.strip()] = val.strip()
    return ctx


def _emit_clarify(
    result: ClarificationRequest, refusal: str | None, args: argparse.Namespace
) -> int:
    """歧义 / LLM 拒答分支（退出码 2）。json 形态含 reasons/candidates/refusal。"""
    if args.format == "json":
        payload = {
            "question": result.question,
            "exit_status": "clarify",
            "reasons": list(result.reasons),
            "candidates": list(result.candidates or []),
        }
        if refusal is not None:
            payload["refusal"] = refusal
        print(json.dumps(payload, ensure_ascii=False, default=_json_default))
    else:
        print(f"[澄清] 问句：{result.question}")
        for reason in result.reasons:
            print(f"  - {reason}")
        if result.candidates:
            print(f"  候选指标：{', '.join(result.candidates)}")
        if refusal is not None:
            print(f"  LLM 候选链拒绝：{refusal}")
    return EXIT_CLARIFY


def _print_table(columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    """对齐表格打印；超过 500 行截断显示（完整数据走 --format json）。"""
    cols = list(columns)
    table = [cols] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(cols))]
    sep = "  " + "-+-".join("-" * w for w in widths)
    for i, row in enumerate(table):
        print("  " + " | ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print(sep)
    if len(rows) > 500:
        print(f"  …（仅显示前 500 / 共 {len(rows)} 行；完整数据用 --format json）")


def _json_default(o: object) -> Any:
    """json.dumps 兜底：Decimal→str（保精度，机读可解析）、date/datetime→iso。"""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime.date):
        return o.isoformat()
    return str(o)


def _emit_result(
    args: argparse.Namespace,
    *,
    question: str,
    path: str,
    plan: Plan,
    sql: str,
    guarded: str,
    cost: float,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    policy: Policy | None,
    usage: dict[str, Any] | None,
) -> int:
    """结果输出（table 默认 / json 机读）。退出码 0。"""
    if args.format == "json":
        payload: dict[str, Any] = {
            "question": question,
            "path": path,
            "metric": plan.metric,
            "dimensions": list(plan.dimensions),
            "time": {"granularity": plan.time.granularity, "value": str(plan.time.value)}
            if plan.time is not None
            else None,
            "sql": sql,
            "guarded_sql": guarded,
            "guard_cost": cost,
            "columns": list(columns),
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "row_policy": policy.name if policy is not None else None,
            "exit_status": "ok",
        }
        if usage is not None:
            payload["usage"] = usage
        print(json.dumps(payload, ensure_ascii=False, default=_json_default))
        return EXIT_OK

    print(f"[answer] 指标={plan.metric} | path={path} | 行数={len(rows)} | 估算成本={cost}")
    if policy is not None:
        print(f"  行级策略已生效：{policy.name}")
    print(f"  SQL: {guarded}")
    _print_table(columns, rows)
    return EXIT_OK


def cmd_query(args: argparse.Namespace) -> int:
    """一步问数：Planner→Compiler→Guard→Doris 真连库，非会话、可脚本化。

    路由（确定性优先，同 agent/graph）：
    - 已知指标：Planner 命中 → 确定性 Compiler（不经 LLM）
    - 未知指标（unmatched）：默认澄清（退出码 2）；仅 --llm 时走 Generator 候选链
      （输出已过 validate_plan_json 注册校验，仍编译→Guard→执行，不生成任意 SQL）

    退出码：0 ok / 1 error（编译·执行·快照缺失）/ 2 clarify / 3 blocked（Guard·策略）。
    """
    model = SemanticModel(DOMAIN_MODEL_PATHS[args.domain])

    # 1. 预算：锁快照表白名单（无快照即拒绝，ExitError）
    try:
        budget = _load_budget()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # 2. 解析：Planner（确定性）→ 已知 Plan；未知按 --llm 决定走 LLM 还是澄清
    parsed = Planner(model).plan(args.question)
    path = "deterministic"
    usage: dict[str, Any] | None = None
    if isinstance(parsed, ClarificationRequest):
        if parsed.kind == "unmatched" and args.llm:
            gen = Generator(model, engine=args.engine)
            gen_res = gen.generate(args.question, k=args.candidate_k)
            if gen_res.refusal is not None:
                return _emit_clarify(parsed, refusal=gen_res.refusal.reason, args=args)
            assert gen_res.plan is not None  # 拒答已在上一支处理，余下必有 plan
            plan = gen_res.plan
            path = "candidate"
            usage = gen_res.usage
        else:
            return _emit_clarify(parsed, refusal=None, args=args)
    else:
        plan = parsed

    # 3. 编译（确定性）
    try:
        sql, _ = Compiler(model).compile(plan)
    except CompileError as exc:
        print(f"[error] 编译失败：{exc}", file=sys.stderr)
        return EXIT_ERROR

    # 4. 行级策略（可选）：--role 经 resolve_claims 渲染谓词 → Policy → enforce 注入
    policy: Policy | None = None
    if args.role:
        claims = {"role": args.role, "user_context": _parse_role_ctx(args.role_ctx)}
        try:
            resolved = resolve_claims(claims)
        except (AuthError, ValueError) as exc:
            print(f"[blocked] 行级策略解析失败：{exc}", file=sys.stderr)
            return EXIT_BLOCKED
        policy = Policy(name=resolved.policy_name, condition=resolved.condition)

    # 5. Guard：只读 + 表白名单 + LIMIT + 时间窗 +（可选）行级策略注入
    try:
        guarded, cost = enforce(sql, policy=policy, budget=budget, model=model)
    except (UnsafeQuery, BudgetExceeded) as exc:
        # 只报拒绝类型与原因，不携带被拒 SQL（纵深防御，不外泄细节）
        print(f"[blocked] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    # 6. 执行（真连库）
    try:
        rows, columns = execute_sql(guarded)
    except Exception as exc:  # noqa: BLE001 - 库连接/查询故障统一归 error 退出码
        print(f"[error] 执行故障：{exc}", file=sys.stderr)
        return EXIT_ERROR

    # 7. 输出
    return _emit_result(
        args,
        question=args.question,
        path=path,
        plan=plan,
        sql=sql,
        guarded=guarded,
        cost=cost,
        columns=columns,
        rows=rows,
        policy=policy,
        usage=usage,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.cli", description="Atlas Agent 确定性链路（Planner → Compiler / query）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="问句 → Plan")
    p_plan.add_argument("question", help="自然语言问句")
    p_plan.add_argument("--domain", choices=["finance", "retail"], default="finance",
                        help="语义模型域（默认 finance）")
    p_plan.set_defaults(func=cmd_plan)

    p_compile = sub.add_parser("compile", help="query.plan.json → 只读 SQL")
    p_compile.add_argument("plan_file", help="Plan JSON 文件路径")
    p_compile.add_argument("--domain", choices=["finance", "retail"], default="finance",
                           help="语义模型域（默认 finance）")
    p_compile.set_defaults(func=cmd_compile)

    p_ask = sub.add_parser("ask", help="多轮问数（真实 Doris + 锁定快照）")
    p_ask.add_argument("question", nargs="?", default=None, help="问句；省略进入交互会话")
    p_ask.set_defaults(func=cmd_ask)

    p_query = sub.add_parser("query", help="一步问数（Planner→Compiler→Guard→Doris）")
    p_query.add_argument("question", help="自然语言问句")
    p_query.add_argument("--domain", choices=["finance", "retail"], default="finance",
                         help="语义模型域（默认 finance）")
    p_query.add_argument("--format", choices=["table", "json"], default="table",
                         help="输出格式（默认 table；json 机读、含 SQL/rows/退出状态）")
    p_query.add_argument("--llm", action="store_true",
                         help="unmatched 问句走 LLM 候选链（需 OPENAI_API_KEY；默认澄清）")
    p_query.add_argument("--engine", choices=["openai", "stub"], default="openai",
                         help="候选链引擎（默认 openai）")
    p_query.add_argument("--candidate-k", type=int, default=5, dest="candidate_k",
                         help="候选链检索候选数 k（默认 5）")
    p_query.add_argument("--role", default=None,
                         help="注入行级策略角色（如 branch_manager）；需配合 --role-ctx")
    p_query.add_argument("--role-ctx", default=None, dest="role_ctx",
                         help="角色上下文 k=v，逗号分隔（如 branch=BR_A1）")
    p_query.set_defaults(func=cmd_query)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
