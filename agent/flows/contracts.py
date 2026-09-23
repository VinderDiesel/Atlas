"""ADR-0031 D05 流程合同：声明式 DAG、节点/边/预算与运行时结果。

范围与边界
----------
- 本模块只做**声明式合同校验**：Schema 版本、ID 唯一性、entry/边的引用、可达性、
  无环、分支穷尽互斥、预算上限、注册表匹配。不构造 LangGraph、不执行节点、不读取
  发布制品；端口兼容（数据依赖在所有路径存在）与执行支配关系随图编译接线。
- 节点实现是**注册代码**：`REGISTERED_NODE_TYPES` 是运行版本的一部分（D05
  「节点实现是注册代码，不是用户上传代码」），制品不能新增节点类型、版本或实现
  ID；未知节点/版本一律拒绝（D06）。
- 分支首版只允许节点状态的 `equals`/`in`，必须穷尽且互斥覆盖五个状态——失败出口
  （blocked/error）必须有明确去向，不执行任意表达式。
- `NodeResult` 的 `output` 判别联合按节点输出合同在运行内核校验；本层只保证形状
  （JSON 对象或 null）。`PlanCandidate` 不等于已授权可执行 Plan（D05）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

# D05 节点类型词表（含尚未注册的 understand/switch/subflow；可用性由注册表决定）
NodeType = Literal[
    "rule_plan",
    "understand",
    "retrieve",
    "bind_plan",
    "execute_plan",
    "analysis",
    "explain",
    "chart",
    "clarify",
    "handoff",
    "switch",
    "subflow",
]
NodeStatus = Literal["ok", "clarify", "unmatched", "blocked", "error"]
NODE_STATUSES: tuple[NodeStatus, ...] = ("ok", "clarify", "unmatched", "blocked", "error")

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_IDENT = r"^[a-z][a-z0-9_]{0,63}$"
_FLOW_ID = r"^[a-z][a-z0-9_.-]{0,63}$"
_IMPLEMENTATION = r"^[a-z][a-z0-9_.]{0,127}$"
_CONTRACT = r"^[A-Za-z][A-Za-z0-9_|+]{0,127}$"
_REASON = r"^[a-z][a-z0-9_]{0,63}$"


@dataclass(frozen=True)
class NodeTypeRegistration:
    """已注册的节点类型版本与实现白名单（随运行代码发布）。"""

    node_type: NodeType
    type_version: int
    implementations: tuple[str, ...]


# 当前已实现能力（2026-09）：understand 属 D09 试点、switch/subflow 属编辑图，
# 均未注册；新增能力必须先在此登记（代码版本），默认模板才可声明。
REGISTERED_NODE_TYPES: tuple[NodeTypeRegistration, ...] = (
    NodeTypeRegistration("rule_plan", 1, ("atlas.planner.rules",)),
    NodeTypeRegistration("retrieve", 1, ("atlas.retrieval.bm25",)),
    NodeTypeRegistration("bind_plan", 1, ("atlas.generator.validated_candidate",)),
    NodeTypeRegistration("execute_plan", 1, ("atlas.execution.protected",)),
    NodeTypeRegistration("analysis", 1, ("atlas.analysis.four_step",)),
    NodeTypeRegistration("explain", 1, ("atlas.explain.template",)),
    NodeTypeRegistration("chart", 1, ("atlas.chart.rules",)),
    NodeTypeRegistration("clarify", 1, ("atlas.clarify.rules",)),
    NodeTypeRegistration("handoff", 1, ("atlas.handoff.template",)),
)

_REGISTERED: dict[tuple[str, int], NodeTypeRegistration] = {
    (item.node_type, item.type_version): item for item in REGISTERED_NODE_TYPES
}


class BudgetSpec(BaseModel):
    """首版共用总预算（D05 防无限循环的初始上限，不是性能承诺）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model_calls: int = Field(default=4, ge=0, le=4)
    node_attempts: int = Field(default=2, ge=1, le=2)
    tool_calls: int = Field(default=3, ge=0, le=3)
    sql_calls: int = Field(default=4, ge=0, le=4)
    run_seconds: int = Field(default=180, ge=1, le=180)
    model_input_tokens: int = Field(default=2048, ge=1, le=2048)
    model_output_tokens: int = Field(default=512, ge=1, le=512)
    model_total_tokens: int = Field(default=10240, ge=1, le=10240)


class NodeSpec(BaseModel):
    """节点声明（D05 字段全集）；`config_ref` 是制品内内容摘要，不是位置引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=_IDENT)
    node_type: NodeType
    type_version: int = Field(ge=1)
    implementation_id: str = Field(pattern=_IMPLEMENTATION)
    config_ref: Digest | None = None
    input_contract: str = Field(pattern=_CONTRACT)
    output_contract: str = Field(pattern=_CONTRACT)
    max_attempts: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def verify_registered(self) -> Self:
        """未知类型/版本/实现一律拒绝（D06 静态校验第一道）。"""
        registration = _REGISTERED.get((self.node_type, self.type_version))
        if registration is None:
            raise ValueError(f"未注册的节点类型或版本：{self.node_type} v{self.type_version}")
        if self.implementation_id not in registration.implementations:
            raise ValueError(f"未注册的节点实现：{self.implementation_id}")
        return self


class EdgeCondition(BaseModel):
    """分支条件：只允许节点状态的 equals/in；穷尽互斥由 FlowDefinition 校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Literal["equals", "in"]
    values: tuple[NodeStatus, ...] = Field(min_length=1, max_length=len(NODE_STATUSES))

    @model_validator(mode="after")
    def verify_values(self) -> Self:
        if self.op == "equals" and len(self.values) != 1:
            raise ValueError("equals 分支只允许一个状态值")
        if len(set(self.values)) != len(self.values):
            raise ValueError("分支状态不得重复")
        return self


class EdgeSpec(BaseModel):
    """声明式边：源节点状态 → 目标节点；不执行任意表达式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    from_node: str = Field(pattern=_IDENT)
    to_node: str = Field(pattern=_IDENT)
    condition: EdgeCondition


def _verify_graph(flow: FlowDefinition) -> None:
    """结构校验：唯一 ID、entry/边引用、无自环、分支穷尽互斥、可达、DAG。"""
    node_ids = [node.node_id for node in flow.nodes]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("node_id 必须唯一")
    known = set(node_ids)
    if flow.entry not in known:
        raise ValueError("entry 必须指向已声明节点")
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in flow.edges:
        if edge.from_node not in known or edge.to_node not in known:
            raise ValueError("边必须引用已声明节点")
        if edge.from_node == edge.to_node:
            raise ValueError("边不允许自环")
        adjacency[edge.from_node].append(edge.to_node)
    for node_id in node_ids:
        covered = [
            status
            for edge in flow.edges
            if edge.from_node == node_id
            for status in edge.condition.values
        ]
        if covered and sorted(covered) != sorted(NODE_STATUSES):
            raise ValueError(f"节点 {node_id} 的分支必须穷尽且互斥覆盖 {list(NODE_STATUSES)}")
    reachable = {flow.entry}
    stack = [flow.entry]
    while stack:
        for target in adjacency[stack.pop()]:
            if target not in reachable:
                reachable.add(target)
                stack.append(target)
    if reachable != known:
        raise ValueError("存在从 entry 不可达的节点")
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in flow.edges:
        indegree[edge.to_node] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in adjacency[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(node_ids):
        raise ValueError("流程必须是 DAG（检测到环）")


class FlowDefinition(BaseModel):
    """D05 流程定义：默认模板先证明旧行为等价，图编译层后续单独接线。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    flow_id: str = Field(pattern=_FLOW_ID)
    revision: int = Field(ge=1)
    entry: str = Field(pattern=_IDENT)
    nodes: tuple[NodeSpec, ...] = Field(min_length=1)
    edges: tuple[EdgeSpec, ...]
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    @model_validator(mode="after")
    def verify_graph(self) -> Self:
        _verify_graph(self)
        return self


class UsageSpec(BaseModel):
    """节点实际消耗（D05）：规则单查询只消耗实际调用，不人为补满预算。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    sql_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)


class NodeResult(BaseModel):
    """D05 节点结果；`output` 的判别联合按节点输出合同在运行内核校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: NodeStatus
    output: dict[str, Any] | None = None
    reason_code: str | None = Field(default=None, pattern=_REASON)
    usage: UsageSpec = Field(default_factory=UsageSpec)
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = ()
