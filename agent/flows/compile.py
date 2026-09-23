"""ADR-0031 D05 FlowDefinition → LangGraph 拓扑编译器。

范围与边界
----------
- `compile_flow()` 从声明式 FlowDefinition 提取拓扑结构（FlowTopology），
  供等价性验证、画布预览与运行时装配消费。
- FlowTopology 提供纯拓扑查询：edge_target / terminal_nodes / paths_to_terminal。
- 不执行节点、不注入实现；节点实现由运行内核按 node_type 分派。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.flows.contracts import BudgetSpec, FlowDefinition


@dataclass(frozen=True)
class FlowTopology:
    """编译产物：声明式图的拓扑视图（纯数据结构，无行为）。"""

    entry: str
    node_ids: frozenset[str]
    terminal_nodes: frozenset[str]
    budget: BudgetSpec
    # _routing: (from_node, status) → to_node
    _routing: dict[tuple[str, str], str] = field(repr=False)
    # _adjacency: from_node → [to_node, ...]
    _adjacency: dict[str, list[str]] = field(repr=False)

    def edge_target(self, from_node: str, status: str) -> str | None:
        """查询从 from_node 在 status 下的路由目标（None = 无匹配边）。"""
        return self._routing.get((from_node, status))

    def paths_to_terminal(self, target: str) -> list[list[str]]:
        """枚举从 entry 到 target 的全部简单路径（DFS，防环安全）。"""
        results: list[list[str]] = []
        stack: list[tuple[str, list[str]]] = [(self.entry, [self.entry])]
        while stack:
            current, path = stack.pop()
            if current == target and current != self.entry:
                results.append(path)
                continue
            for next_node in self._adjacency.get(current, []):
                if next_node not in path:  # 简单路径（无环）
                    stack.append((next_node, [*path, next_node]))
        return results


def compile_flow(flow: FlowDefinition) -> FlowTopology:
    """从 FlowDefinition 编译 FlowTopology（纯拓扑，不执行节点）。"""
    node_ids = frozenset(n.node_id for n in flow.nodes)

    # 邻接表 + 路由表
    adjacency: dict[str, list[str]] = {n.node_id: [] for n in flow.nodes}
    routing: dict[tuple[str, str], str] = {}

    for edge in flow.edges:
        adjacency[edge.from_node].append(edge.to_node)
        for status in edge.condition.values:
            routing[(edge.from_node, status)] = edge.to_node

    # 终端节点：无出边
    has_outgoing = {e.from_node for e in flow.edges}
    terminal = frozenset(nid for nid in node_ids if nid not in has_outgoing)

    return FlowTopology(
        entry=flow.entry,
        node_ids=node_ids,
        terminal_nodes=terminal,
        budget=flow.budget,
        _routing=routing,
        _adjacency=adjacency,
    )
