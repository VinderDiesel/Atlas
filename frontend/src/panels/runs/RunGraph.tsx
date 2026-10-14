/**
 * 运行关系图（T06d；ADR-0031 D07②「图按固定模板可展开查看」）。
 *
 * 只读图（T06 账本；T09 才开放编辑）：节点/边 = run-view-model 的固定模板
 * 与 `RunViewState` 合并——模板恒在，未到达节点不编造时间，taken 边只来自
 * EDGE_TAKEN 事件（不从 UI 时间推断）。
 *
 * 依赖许可（ADR-0031 许可门）：@xyflow/react 12.x（MIT，webkid GmbH）——
 * `proOptions.hideAttribution` 显式保持 false，版权 attribution 不隐藏。
 */
import {
  ReactFlow,
  ReactFlowProvider,
  MarkerType,
  Position,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Space, Typography } from "antd";

import type { RunNodeStatus } from "../../api/types";
import type { RunViewState } from "../../state/run-events";

import { layoutRunGraph } from "./run-view-model";

interface Props {
  /** 事件归约态（图与时间线同一事实源；组件不自行请求）。 */
  state: RunViewState;
  /** 当前选中的节点 ID（Phase 1 可视化；null = 无选中）。 */
  selectedNodeId?: string | null;
  /** 节点点击回调（父组件用于展示 NodeInspector）。 */
  onNodeClick?: (nodeId: string) => void;
}

const { Text } = Typography;

/** 节点状态展示元数据（图例与节点共用；色值取全局变量，不另立色板）。 */
const NODE_STATUS_META: Record<RunNodeStatus, { label: string; color: string }> = {
  not_reached: { label: "未执行", color: "var(--atlas-muted)" },
  running: { label: "运行中", color: "var(--atlas-primary)" },
  finished: { label: "已完成", color: "var(--atlas-success)" },
  failed: { label: "失败", color: "var(--atlas-danger)" },
  skipped: { label: "跳过", color: "var(--atlas-accent)" },
};

type RunFlowNode = Node<{ label: string; status: RunNodeStatus; selected?: boolean }, "runNode">;

function RunNodeCard({ data }: NodeProps<RunFlowNode>) {
  const meta = NODE_STATUS_META[data.status];
  const isSelected = data.selected ?? false;
  const cls = [
    "atlas-run-node",
    `atlas-run-node--${data.status}`,
    isSelected ? "atlas-run-node--selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls}>
      <div className="atlas-run-node__label">{data.label}</div>
      <div className="atlas-run-node__status">{meta.label}</div>
    </div>
  );
}

const NODE_TYPES: NodeTypes = { runNode: RunNodeCard };

export default function RunGraph({ state, selectedNodeId, onNodeClick }: Props) {
  const layout = layoutRunGraph(state);
  const nodes: RunFlowNode[] = layout.nodes.map((node) => ({
    id: node.id,
    type: "runNode",
    position: node.position,
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    data: { label: node.label, status: node.status, selected: node.id === selectedNodeId },
    draggable: false,
    connectable: false,
    selectable: true,
  }));
  const edges: Edge[] = layout.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    animated: edge.taken,
    markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12 },
    style: edge.taken
      ? {
          stroke: "var(--atlas-primary)",
          strokeWidth: 2.5,
          filter: "drop-shadow(0 0 4px rgba(110, 231, 245, 0.5))",
        }
      : { stroke: "var(--atlas-border)", strokeWidth: 1 },
  }));

  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space size="middle" wrap>
        {Object.entries(NODE_STATUS_META).map(([status, meta]) => (
          <Text key={status} style={{ fontSize: 12 }}>
            <span className="atlas-legend-dot" style={{ background: meta.color }} />
            {meta.label}
          </Text>
        ))}
        <Text type="secondary" style={{ fontSize: 12 }}>
          加粗高亮 = 已走过的边（EDGE_TAKEN）
        </Text>
      </Space>
      <div className="atlas-graph">
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={true}
            fitView
            proOptions={{ hideAttribution: false }}
            style={{ width: "100%", height: "100%" }}
            onNodeClick={(_event, node) => {
              if (onNodeClick) {
                onNodeClick(node.id);
              }
            }}
          />
        </ReactFlowProvider>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        点击节点查看输入/输出详情。节点/边是与 Agent 图定义同源的固定模板；未到达节点
        显示「未执行」（不编造开始/结束时间），实际路径以事件流的 EDGE_TAKEN 为准。
      </Text>
    </Space>
  );
}
