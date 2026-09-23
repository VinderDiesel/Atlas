"""CLI：``python -m agent.flows validate <path>`` 校验流程定义。

退出码：0 = 通过，1 = 有问题，2 = 用法错误。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.flows.registry import node_catalog
from agent.flows.validate import validate_flow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.flows", description="流程定义工具")
    sub = parser.add_subparsers(dest="command")
    val = sub.add_parser("validate", help="校验流程定义")
    val.add_argument("path", type=Path, help="流程 JSON 文件路径")

    args = parser.parse_args(argv)
    if args.command != "validate":
        parser.print_help()
        return 2

    data = json.loads(args.path.read_text())
    issues = validate_flow(data, node_catalog())
    if not issues:
        print(f"OK: {args.path}")
        return 0
    for issue in issues:
        loc = issue.node_id or issue.edge_id or "-"
        print(f"[{issue.code}] {loc}: {issue.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
