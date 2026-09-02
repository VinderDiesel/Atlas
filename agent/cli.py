"""Agent 确定性链路命令行入口（AGENTS.md 常用命令：make plan / make compile）。

MVP 范围：
- plan：问句 → Plan（确定性 Planner）；歧义时打印 ClarificationRequest 并退出码 1
- compile：query.plan.json → 只读 SQL（确定性 Compiler）

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

from agent.compiler import Compiler, Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.planner import ClarificationRequest, Planner


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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
