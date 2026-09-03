"""Agent 命令行入口（AGENTS.md 常用命令：make plan / make compile / ask）。

MVP 范围：
- plan：问句 → Plan（确定性 Planner）；歧义时打印 ClarificationRequest 并退出码 1
- compile：query.plan.json → 只读 SQL（确定性 Compiler）
- ask：多轮问数会话（Day 49）——真实 Doris + 锁定快照，Data Agent 状态机
  逐轮输出 answer / clarify / blocked / error / handoff；无参数进入交互模式
  （同一 session 连续多轮，空行退出）

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
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.compiler import Compiler, Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.graph import DataAgent
from agent.planner import ClarificationRequest, Planner
from agent.state import TurnResult


def _live_agent() -> DataAgent:
    """真实会话 Agent：只读执行器与快照绑定走 eval/runner 同源（延迟 import，
    plan/compile 不触碰数据库）。快照 = 当前 git HEAD 的已锁 meta，缺则拒绝。"""
    from eval.runner import SNAPSHOT_DIR, build_budget, execute_sql, git_short_sha

    sha = git_short_sha()
    meta_path = SNAPSHOT_DIR / f"{sha}.meta.json"
    if not meta_path.is_file():
        raise SystemExit(
            f"[ask] 当前 HEAD {sha} 无锁定快照 meta（{meta_path}）——无法绑定评测数据；"
            "请先 make seed 锁定快照（AGENTS.md N6）"
        )
    meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    return DataAgent(executor=execute_sql, budget=build_budget(meta), snapshot_meta=meta)


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
    agent = _live_agent()
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
    model = SemanticModel()
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
    sql, _ = Compiler(SemanticModel()).compile(plan)
    print(sql)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.cli", description="Atlas Agent 确定性链路（Planner → Compiler）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="问句 → Plan")
    p_plan.add_argument("question", help="自然语言问句")
    p_plan.set_defaults(func=cmd_plan)

    p_compile = sub.add_parser("compile", help="query.plan.json → 只读 SQL")
    p_compile.add_argument("plan_file", help="Plan JSON 文件路径")
    p_compile.set_defaults(func=cmd_compile)

    p_ask = sub.add_parser("ask", help="多轮问数（真实 Doris + 锁定快照）")
    p_ask.add_argument("question", nargs="?", default=None, help="问句；省略进入交互会话")
    p_ask.set_defaults(func=cmd_ask)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
