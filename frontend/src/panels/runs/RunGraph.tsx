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

type RunFlowNode = Node<{ label: string; status: RunNodeStatus }, "runNode">;

function RunNodeCard({ data }: NodeProps<RunFlowNode>) {
  const meta = NODE_STATUS_META[data.status];
  return (
    <div
      style={{
        minWidth: 72,
        padding: "6px 10px",
        borderRadius: 6,
        border: `1.5px solid ${meta.color}`,
        background: "var(--atlas-surface)",
        textAlign: "center",
      }}
    >
      <div style={{ fontWeight: 600 }}>{data.label}</div>
      <div style={{ fontSize: 11, color: meta.color }}>{meta.label}</div>
    </div>
  );
}

const NODE_TYPES: NodeTypes = { runNode: RunNodeCard };

export default function RunGraph({ state }: Props) {
  const layout = layoutRunGraph(state);
  const nodes: RunFlowNode[] = layout.nodes.map((node) => ({
    id: node.id,
    type: "runNode",
    position: node.position,
    data: { label: node.label, status: node.status },
    draggable: false,
    connectable: false,
    selectable: false,
  }));
  const edges: Edge[] = layout.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    animated: edge.taken,
    style: edge.taken
      ? { stroke: "var(--atlas-primary)", strokeWidth: 2 }
      : { stroke: "var(--atlas-border)" },
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
            elementsSelectable={false}
            fitView
            proOptions={{ hideAttribution: false }}
            style={{ width: "100%", height: "100%" }}
          />
        </ReactFlowProvider>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        节点/边是与 Agent 图定义同源的固定模板；未到达节点显示「未执行」（不编造
        开始/结束时间），实际路径以事件流的 EDGE_TAKEN 为准。
      </Text>
    </Space>
  );
}
