"""ADR-0031 D05 节点目录：注册节点类型、版本、实现白名单与端口合同。

范围与边界
----------
- `node_catalog()` 返回已注册节点类型的只目录，供 `validate_flow` 消费。
- 端口合同（input_contract / output_contract）按 D05 表格声明；终端节点
  （clarify/handoff/explain/chart）输入合同视为通配，不参与端口兼容校验。
- 新增节点类型必须同时更新 `REGISTERED` 与本模块的端口表格（代码版本）。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.flows.contracts import REGISTERED_NODE_TYPES


@dataclass(frozen=True)
class NodeCatalogEntry:
    """已注册节点类型目录条目。"""

    node_type: str
    type_version: int
    implementations: tuple[str, ...]
    input_contract: str
    output_contract: str


# D05 端口合同表（与 contracts.py REGISTERED_NODE_TYPES 一一对应）
_PORT_TABLE: dict[str, tuple[str, str]] = {
    "rule_plan": ("QuestionContext", "PlanCandidate|Clarification|Unmatched"),
    "understand": ("QuestionContext", "SemanticIntent"),
    "retrieve": ("RetrievalRequest", "CandidateSet"),
    "bind_plan": ("SemanticIntent+CandidateSet", "PlanCandidate|Clarification"),
    "execute_plan": ("PlanCandidate", "ExecutionResult"),
    "analysis": ("AnalysisPlan", "AnalysisResult"),
    "explain": ("ExecutionResult|AnalysisResult", "Explanation"),
    "chart": ("ExecutionResult|AnalysisResult", "ChartSpec"),
    "clarify": ("Clarification|ReasonCode", "ClarificationTurn"),
    "handoff": ("ReasonCode|Unmatched", "HandoffTurn"),
    "switch": ("Any", "Any"),
    "subflow": ("Any", "Any"),
}


def node_catalog() -> dict[str, NodeCatalogEntry]:
    """返回已注册节点类型目录（node_type → NodeCatalogEntry）。"""
    catalog: dict[str, NodeCatalogEntry] = {}
    for reg in REGISTERED_NODE_TYPES:
        ports = _PORT_TABLE.get(reg.node_type, ("Any", "Any"))
        catalog[reg.node_type] = NodeCatalogEntry(
            node_type=reg.node_type,
            type_version=reg.type_version,
            implementations=reg.implementations,
            input_contract=ports[0],
            output_contract=ports[1],
        )
    return catalog
