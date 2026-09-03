"""真实会话 Agent 工厂：CLI（agent/cli.py）与 HTTP API（serving/api.py）共源。

create_live_agent() 原为 agent/cli.py 的 _live_agent（ADR-0012 提取为工厂）：
只读执行器与快照绑定走 eval/runner 同源（延迟 import——plan/compile 路径不触碰
数据库，本模块只在 ask 场景被调用）。快照 = 当前 git HEAD 的已锁 meta，缺则抛
SnapshotUnavailable——CLI 捕获后打印并 exit 1，HTTP 层捕获后转 503；不再用
SystemExit 中断（SystemExit 只属于 CLI 出口语义，HTTP 进程内不该裸奔）。
"""

from __future__ import annotations

import json
from typing import Any

from agent.graph import DataAgent


class SnapshotUnavailable(Exception):
    """当前 HEAD 无锁定快照 meta（HTTP 层转 503，见 ADR-0012）。"""


def create_live_agent() -> DataAgent:
    """真实会话 Agent：真 Doris 执行器 + 当前 HEAD 锁定快照的预算与 meta。"""
    from eval.runner import SNAPSHOT_DIR, build_budget, execute_sql, git_short_sha

    sha = git_short_sha()
    meta_path = SNAPSHOT_DIR / f"{sha}.meta.json"
    if not meta_path.is_file():
        raise SnapshotUnavailable(
            f"当前 HEAD {sha} 无锁定快照 meta（{meta_path}）——无法绑定评测数据；"
            "请先 make seed 锁定快照（AGENTS.md N6）"
        )
    meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    return DataAgent(executor=execute_sql, budget=build_budget(meta), snapshot_meta=meta)
