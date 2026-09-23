/**
 * T09c: FlowDefinition → React Flow 节点/边布局工具。
 *
 * 将声明式 FlowDefinition 转为 React Flow 消费的 nodes/edges 数组。
 * 布局采用简单的层级分配（BFS 从 entry 开始），不改变执行语义。
 */

import type { Node, Edge } from "@xyflow/react";

import type { FlowDefinition, FlowEdge, FlowNode } from "../../api/flows";

export type { FlowNode } from "../../api/flows";

// 受保护节点类型（execute_plan 不可绕过）
const PROTECTED_TYPES = new Set(["execute_plan"]);

export interface FlowNodeData extends Record<string, unknown> {
  nodeType: string;
  implementationId: string;
  inputContract: string;
  outputContract: string;
  isEntry: boolean;
  isTerminal: boolean;
  isProtected: boolean;
  label: string;
}

/**
 * 将 FlowDefinition 转为 React Flow 的 nodes 与 edges。
 */
export function flowToNodesEdges(
  flow: FlowDefinition,
): { nodes: Node<FlowNodeData>[]; edges: Edge[] } {
  // 确定终端节点（无出边）
  const hasOutgoing = new Set(flow.edges.map((e: FlowEdge) => e.from_node));
  const terminalIds = new Set(
    flow.nodes.filter((n: FlowNode) => !hasOutgoing.has(n.node_id)).map((n: FlowNode) => n.node_id),
  );

  // BFS 层级布局
  const levels = new Map<string, number>();
  const queue = [flow.entry];
  levels.set(flow.entry, 0);
  while (queue.length > 0) {
    const current = queue.shift()!;
    const currentLevel = levels.get(current)!;
    for (const edge of flow.edges) {
      if (edge.from_node === current && !levels.has(edge.to_node)) {
        levels.set(edge.to_node, currentLevel + 1);
        queue.push(edge.to_node);
      }
    }
  }

  // 按层级分组
  const byLevel = new Map<number, FlowNode[]>();
  for (const node of flow.nodes) {
    const level = levels.get(node.node_id) ?? 0;
    if (!byLevel.has(level)) byLevel.set(level, []);
    byLevel.get(level)!.push(node);
  }

  // 生成 React Flow 节点
  const nodes: Node<FlowNodeData>[] = [];
  for (const [level, levelNodes] of byLevel) {
    levelNodes.forEach((node, idx) => {
      nodes.push({
        id: node.node_id,
        position: { x: idx * 250, y: level * 150 },
        data: {
          nodeType: node.node_type,
          implementationId: node.implementation_id,
          inputContract: node.input_contract,
          outputContract: node.output_contract,
          isEntry: node.node_id === flow.entry,
          isTerminal: terminalIds.has(node.node_id),
          isProtected: PROTECTED_TYPES.has(node.node_type),
          label: `${node.node_id} (${node.node_type})`,
        },
      });
    });
  }

  // 生成 React Flow 边
  const edges: Edge[] = flow.edges.map((edge: FlowEdge, idx: number) => ({
    id: `e-${idx}`,
    source: edge.from_node,
    target: edge.to_node,
    label: edge.condition.values.join("/"),
    animated: edge.condition.values.includes("ok"),
  }));

  return { nodes, edges };
}
