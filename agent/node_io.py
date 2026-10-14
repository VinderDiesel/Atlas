"""节点 I/O 提取模块（问数流程可视化 Phase 1）。

定义每个图节点的 input_keys / output_keys（NODE_IO_SPEC），提供 extract_node_io
纯函数从 TurnState 提取结构化快照。供事件接缝（graph.py _node_traced）和前端
NodeInspector 消费。

D07 约束放宽（仅 node_io 通道）：
- Plan 结构 / SQL / 路由决策 / 验证结果 → 允许进入
- 执行结果行（rows）/ 原始 Prompt / 问句原文 → 不进入
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Literal

# ---------------------------------------------------------------------------
# NODE_IO_SPEC：每个图节点声明需要捕获的输入/输出 state 键
# ---------------------------------------------------------------------------

NODE_IO_SPEC: dict[str, dict[str, list[str]]] = {
    "plan": {
        "input": ["plan_override", "last_plan", "unmatched", "turns"],
        "output": ["plan", "unmatched", "reason"],
    },
    "retrieve": {
        "input": ["plan", "candidates"],
        "output": ["candidates"],
    },
    "generate": {
        "input": ["plan", "candidates", "attempts"],
        "output": ["plan", "attempts"],
    },
    "validate": {
        "input": ["plan", "validated", "attempts"],
        "output": ["plan", "validated", "sql", "reason"],
    },
    "execute": {
        "input": ["plan", "sql", "validated"],
        "output": ["sql", "columns", "row_count", "latency_ms", "time_column"],
    },
    "explain": {
        "input": ["plan", "row_count"],
        "output": ["explanation"],
    },
    "clarify": {
        "input": ["plan", "reason", "clarification"],
        "output": ["clarification", "reason"],
    },
    "handoff": {
        "input": ["reason", "plan"],
        "output": ["handoff_request"],
    },
}


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def _serialize_value(value: Any) -> Any:
    """将 state 值转为 JSON 可序列化形式。

    - dataclass（Plan / ClarificationRequest / TimeSpec / Filter 等）→ dict（递归，
      内部 tuple 也转为 list——asdict 保留 tuple，此处统一处理）
    - tuple → list（JSON 无 tuple 类型）
    - None / str / int / float / bool → 原样
    - 其他 → repr() 兜底
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _serialize_value(asdict(value))
    if isinstance(value, tuple):
        return [_serialize_value(v) for v in value]
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return repr(value)


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def extract_node_io(
    node_id: str,
    state: dict[str, Any],
    *,
    kind: Literal["input", "output"],
) -> dict[str, Any]:
    """从 TurnState（dict 形式）提取指定节点的输入/输出快照。

    参数
    ----
    node_id : 图节点标识符（plan / retrieve / generate / validate / execute /
              explain / clarify / handoff）。
    state   : TurnState 字典（节点执行前 = input；节点执行后合并 = output）。
    kind    : "input" 或 "output"，决定读取 NODE_IO_SPEC 的哪个键列表。

    返回
    ----
    dict[str, Any]：仅包含 spec 声明键的序列化快照。未知 node_id 或缺失键
    返回空 dict（不抛异常）。值为 None 的键保留（与缺失区分）。
    """
    spec = NODE_IO_SPEC.get(node_id)
    if spec is None:
        return {}
    keys = spec.get(kind, [])
    result: dict[str, Any] = {}
    for key in keys:
        if key in state:
            result[key] = _serialize_value(state[key])
    return result
