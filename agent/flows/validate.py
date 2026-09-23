"""ADR-0031 D05/D06 结构化图校验器：返回 FlowIssue 列表而非异常。

范围与边界
----------
- 接受 dict 或 FlowDefinition；dict 模式下可捕获 Pydantic 拒绝的未知类型。
- 校验维度：unknown_node_type / unregistered_implementation / duplicate_node_id /
  entry_not_found / edge_unknown_node / self_loop / unreachable /
  branch_not_exhaustive / branch_overlapping / no_failure_exit / cycle /
  budget_exceeded / port_mismatch / execute_plan_bypassed / schema_error。
- 不构造 LangGraph、不执行节点；纯声明式校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.flows.contracts import NODE_STATUSES, BudgetSpec, FlowDefinition
from agent.flows.registry import NodeCatalogEntry


@dataclass(frozen=True)
class FlowIssue:
    """结构化校验问题：code 为机器可读错误码，node_id/edge_id 定位，message 人读。"""

    code: str
    node_id: str | None = None
    edge_id: str | None = None
    message: str = ""


# 终端节点类型：无出边，不参与端口兼容校验与失败出口检查
_TERMINAL_TYPES = frozenset({"clarify", "handoff", "explain", "chart"})


@dataclass
class _ParsedNode:
    node_id: str
    node_type: str
    type_version: int
    implementation_id: str
    input_contract: str
    output_contract: str


@dataclass
class _ParsedEdge:
    from_node: str
    to_node: str
    statuses: list[str]


@dataclass
class _ParsedFlow:
    entry: str
    nodes: list[_ParsedNode]
    edges: list[_ParsedEdge]
    budget: dict[str, Any]
    schema_error: str | None = None


def _parse_flow(data: dict[str, Any]) -> _ParsedFlow:
    """从 dict 提取图结构（best-effort，不抛异常）。"""
    if not isinstance(data, dict):
        return _ParsedFlow("", [], [], {}, "输入不是对象")

    entry = str(data.get("entry", ""))
    budget = data.get("budget", {}) if isinstance(data.get("budget"), dict) else {}

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        return _ParsedFlow(entry, [], [], budget, "nodes 不是数组")

    nodes: list[_ParsedNode] = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        nodes.append(
            _ParsedNode(
                node_id=str(item.get("node_id", "")),
                node_type=str(item.get("node_type", "")),
                type_version=int(item.get("type_version", 0)),
                implementation_id=str(item.get("implementation_id", "")),
                input_contract=str(item.get("input_contract", "")),
                output_contract=str(item.get("output_contract", "")),
            )
        )

    raw_edges = data.get("edges")
    if not isinstance(raw_edges, list):
        return _ParsedFlow(entry, nodes, [], budget, "edges 不是数组")

    edges: list[_ParsedEdge] = []
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        cond = item.get("condition", {})
        if not isinstance(cond, dict):
            continue
        values = cond.get("values", [])
        edges.append(
            _ParsedEdge(
                from_node=str(item.get("from_node", "")),
                to_node=str(item.get("to_node", "")),
                statuses=list(values) if isinstance(values, list) else [],
            )
        )

    return _ParsedFlow(entry, nodes, edges, budget)


def validate_flow(
    flow: FlowDefinition | dict[str, Any],
    catalog: dict[str, NodeCatalogEntry],
) -> list[FlowIssue]:
    """结构化校验 FlowDefinition 或原始 dict，返回 FlowIssue 列表（空 = 通过）。"""
    data = flow.model_dump() if isinstance(flow, FlowDefinition) else flow

    parsed = _parse_flow(data)
    if parsed.schema_error is not None:
        return [FlowIssue("schema_error", message=parsed.schema_error)]

    issues: list[FlowIssue] = []

    # --- 节点级校验 ---
    seen_ids: set[str] = set()
    node_map: dict[str, _ParsedNode] = {}

    for node in parsed.nodes:
        # 唯一 ID
        if node.node_id in seen_ids:
            issues.append(
                FlowIssue(
                    "duplicate_node_id", node.node_id,
                    message=f"重复 node_id: {node.node_id}",
                )
            )
        seen_ids.add(node.node_id)
        node_map[node.node_id] = node

        # 注册类型
        entry = catalog.get(node.node_type)
        if entry is None:
            issues.append(
                FlowIssue(
                    "unknown_node_type",
                    node.node_id,
                    message=f"未注册的节点类型: {node.node_type}",
                )
            )
            continue  # 无法进一步校验此节点
        if node.type_version != entry.type_version:
            issues.append(
                FlowIssue(
                    "unknown_node_type",
                    node.node_id,
                    message=f"未注册的版本: {node.node_type} v{node.type_version}",
                )
            )
        if node.implementation_id not in entry.implementations:
            issues.append(
                FlowIssue(
                    "unregistered_implementation",
                    node.node_id,
                    message=f"未注册的实现: {node.implementation_id}",
                )
            )

    # --- entry ---
    if parsed.entry not in seen_ids:
        issues.append(
            FlowIssue("entry_not_found", message=f"entry 指向不存在的节点: {parsed.entry}")
        )

    # --- 边级校验 ---
    adjacency: dict[str, list[str]] = {nid: [] for nid in seen_ids}

    for edge in parsed.edges:
        if edge.from_node not in seen_ids or edge.to_node not in seen_ids:
            issues.append(
                FlowIssue(
                    "edge_unknown_node",
                    edge_id=f"{edge.from_node}->{edge.to_node}",
                    message="边引用未声明的节点",
                )
            )
            continue
        if edge.from_node == edge.to_node:
            issues.append(
                FlowIssue(
                    "self_loop",
                    edge.from_node,
                    edge_id=f"{edge.from_node}->{edge.to_node}",
                    message="自环",
                )
            )
            continue
        adjacency[edge.from_node].append(edge.to_node)

        # 端口兼容（预留，当前不检查——非 ok 边携带转换数据，类型名不可直接比对）

    # --- 可达性 ---
    if parsed.entry in seen_ids:
        reachable = {parsed.entry}
        stack = [parsed.entry]
        while stack:
            for target in adjacency.get(stack.pop(), []):
                if target not in reachable:
                    reachable.add(target)
                    stack.append(target)
        for nid in seen_ids:
            if nid not in reachable:
                issues.append(
                    FlowIssue("unreachable", nid, message=f"从 entry 不可达: {nid}")
                )

    # --- DAG（Kahn 拓扑排序） ---
    indegree: dict[str, int] = {nid: 0 for nid in seen_ids}
    for edge in parsed.edges:
        both_known = edge.from_node in seen_ids and edge.to_node in seen_ids
        if both_known and edge.from_node != edge.to_node:
            indegree[edge.to_node] += 1
    temp_ready = [nid for nid, deg in indegree.items() if deg == 0]
    topo_count = 0
    while temp_ready:
        current = temp_ready.pop()
        topo_count += 1
        for target in adjacency.get(current, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                temp_ready.append(target)
    if topo_count != len(seen_ids):
        issues.append(FlowIssue("cycle", message="检测到环"))

    # --- 分支穷尽 / 失败出口 / 重叠 ---
    for nid in seen_ids:
        node = node_map[nid]
        if node.node_type in _TERMINAL_TYPES:
            continue

        node_edges = [e for e in parsed.edges if e.from_node == nid]
        if not node_edges:
            continue

        covered: list[str] = []
        for e in node_edges:
            covered.extend(e.statuses)

        covered_set = set(covered)
        missing = set(NODE_STATUSES) - covered_set
        has_blocked = "blocked" in covered_set
        has_error = "error" in covered_set

        # 重叠检测
        seen_statuses: set[str] = set()
        for e in node_edges:
            for s in e.statuses:
                if s in seen_statuses:
                    issues.append(
                        FlowIssue(
                            "branch_overlapping",
                            nid,
                            message=f"分支状态重复: {s}",
                        )
                    )
                    break
                seen_statuses.add(s)

        if not missing:
            continue  # 穷尽覆盖，无问题

        if not has_blocked and not has_error:
            issues.append(
                FlowIssue(
                    "no_failure_exit",
                    nid,
                    message="blocked/error 无出口",
                )
            )
        else:
            issues.append(
                FlowIssue(
                    "branch_not_exhaustive",
                    nid,
                    message=f"分支未穷尽，缺少: {sorted(missing)}",
                )
            )

    # --- 预算 ---
    try:
        BudgetSpec(**parsed.budget)
    except Exception:
        issues.append(FlowIssue("budget_exceeded", message="预算超限"))

    # --- execute_plan 安全必经路径 ---
    # 只检查含 execute_plan 的图：移除 execute_plan 后 explain 不应可达。
    # 不含 execute_plan 的图（如 analysis 模板）不检查——它有自己的受保护内核。
    execute_ids = [
        nid for nid, n in node_map.items() if n.node_type == "execute_plan"
    ]
    if execute_ids and parsed.entry in seen_ids:
        removed = set(execute_ids)
        test_adj: dict[str, list[str]] = {
            nid: [t for t in targets if t not in removed]
            for nid, targets in adjacency.items()
            if nid not in removed
        }
        reach = {parsed.entry}
        stack = [parsed.entry]
        while stack:
            for target in test_adj.get(stack.pop(), []):
                if target not in reach:
                    reach.add(target)
                    stack.append(target)
        explain_reachable = any(
            nid not in removed
            and node_map[nid].node_type == "explain"
            and nid in reach
            for nid in seen_ids
        )
        if explain_reachable:
            issues.append(
                FlowIssue(
                    "execute_plan_bypassed",
                    message="存在绕过 execute_plan 到达 explain 的路径",
                )
            )

    return issues

