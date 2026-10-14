/**
 * 节点详情检查器（Phase 1 问数流程可视化）。
 *
 * 展示单个 LangGraph 节点的输入/输出快照：默认结构化摘要（Plan 显示 metric/
 * dimensions/time/filters；SQL 截断 80 字符；数组显示项数），点击「展开 JSON」
 * 查看完整数据。同时列出该节点的子工具调用（TOOL_STARTED/TOOL_FINISHED 事件）。
 */
import { Collapse, Descriptions, Empty, Tag, Typography } from "antd";
import type { RunEvent } from "../../api/types";
import type { RunNodeView } from "../../state/run-events";
import { formatNodeIO, NODE_LABELS, type FormattedIOEntry } from "./node-io-view";

const { Text, Paragraph } = Typography;

interface Props {
  /** 选中的节点视图（含 input_io / output_io）。 */
  node: RunNodeView;
  /** 该节点关联的子工具调用事件（TOOL_STARTED/TOOL_FINISHED）。 */
  toolEvents: RunEvent[];
  /** 路由决策（node_id → reason；用于解释为何跳过某些节点）。 */
  routingDecisions?: Record<string, string>;
  /** 图模板边（用于计算未执行节点的跳过原因；[source, target][]）。 */
  templateEdges?: Array<[string, string]>;
}

/** 状态徽标颜色映射。 */
const STATUS_COLORS: Record<string, string> = {
  finished: "green",
  failed: "red",
  running: "blue",
  skipped: "orange",
  not_reached: "default",
};

/** 渲染单侧（input 或 output）的 I/O 数据。 */
function IOSection({
  title,
  io,
}: {
  title: string;
  io: Record<string, unknown> | null;
}) {
  if (io === null) {
    return (
      <div style={{ marginBottom: 12 }}>
        <Text strong>{title}</Text>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="详情不可用"
          style={{ margin: "4px 0" }}
        />
      </div>
    );
  }
  const entries = formatNodeIO(io);
  if (entries.length === 0) {
    return (
      <div style={{ marginBottom: 12 }}>
        <Text strong>{title}</Text>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="无数据"
          style={{ margin: "4px 0" }}
        />
      </div>
    );
  }
  return (
    <div style={{ marginBottom: 12 }}>
      <Text strong>{title}</Text>
      <Descriptions
        size="small"
        column={1}
        bordered
        style={{ marginTop: 4 }}
        items={entries.map((entry) => ({
          key: entry.key,
          label: <Text code>{entry.key}</Text>,
          children: <IOValue entry={entry} raw={io[entry.key]} />,
        }))}
      />
    </div>
  );
}

/** 渲染单个 I/O 值（摘要 + 可选 JSON 展开）。 */
function IOValue({ entry, raw }: { entry: FormattedIOEntry; raw: unknown }) {
  if (!entry.expandable) {
    return <Text>{entry.display}</Text>;
  }
  return (
    <div>
      <Text>{entry.display}</Text>
      <Collapse
        size="small"
        style={{ marginTop: 4 }}
        items={[
          {
            key: "json",
            label: "展开 JSON",
            children: (
              <Paragraph>
                <pre style={{ margin: 0, fontSize: 12, maxHeight: 300, overflow: "auto" }}>
                  {JSON.stringify(raw, null, 2)}
                </pre>
              </Paragraph>
            ),
          },
        ]}
      />
    </div>
  );
}

/** 节点详情检查器主组件。 */
export function NodeInspector({ node, toolEvents, routingDecisions = {}, templateEdges = [] }: Props) {
  const label = NODE_LABELS[node.node_id] ?? node.node_id;
  const routingReason = routingDecisions[node.node_id];

  // 计算未执行节点的跳过原因：找到路由到别处的上游节点
  let skipReason: string | null = null;
  if (node.status === "not_reached") {
    for (const [source, target] of templateEdges) {
      if (target === node.node_id) {
        const decision = routingDecisions[source];
        if (decision) {
          const sourceLabel = NODE_LABELS[source] ?? source;
          skipReason = `上游节点「${sourceLabel}」路由到其他分支：${decision}`;
          break;
        }
      }
    }
  }

  return (
    <div style={{ padding: "8px 0" }} data-testid="node-inspector">
      <Descriptions
        size="small"
        column={3}
        style={{ marginBottom: 12 }}
        items={[
          {
            key: "name",
            label: "节点",
            children: (
              <span>
                <Text strong>{label}</Text>
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  ({node.node_id})
                </Text>
              </span>
            ),
          },
          {
            key: "status",
            label: "状态",
            children: (
              <Tag color={STATUS_COLORS[node.status] ?? "default"}>{node.status}</Tag>
            ),
          },
          {
            key: "seq",
            label: "事件序",
            children:
              node.started_seq !== null
                ? `#${node.started_seq}${node.ended_seq !== null ? ` → #${node.ended_seq}` : ""}`
                : "—",
          },
        ]}
      />

      {routingReason && (
        <div style={{ marginBottom: 12 }}>
          <Text strong>路由决策</Text>
          <div style={{ marginTop: 4, padding: "8px 12px", background: "var(--atlas-code-bg, #0a0f1a)", border: "1px solid var(--atlas-border, #1e2d42)", borderRadius: 4 }}>
            <Text style={{ color: "var(--atlas-text, #e2e8f0)" }}>{routingReason}</Text>
          </div>
        </div>
      )}

      {skipReason && (
        <div style={{ marginBottom: 12 }}>
          <Text strong>未执行原因</Text>
          <div style={{ marginTop: 4, padding: "8px 12px", background: "var(--atlas-code-bg, #0a0f1a)", border: "1px solid var(--atlas-border, #1e2d42)", borderRadius: 4 }}>
            <Text style={{ color: "var(--atlas-text, #e2e8f0)" }}>{skipReason}</Text>
          </div>
        </div>
      )}

      <IOSection title="输入" io={node.input_io} />
      <IOSection title="输出" io={node.output_io} />

      {toolEvents.length > 0 && (
        <div>
          <Text strong>子工具调用</Text>
          <Descriptions
            size="small"
            column={1}
            bordered
            style={{ marginTop: 4 }}
            items={toolEvents.map((e, i) => ({
              key: String(i),
              label: (
                <Tag color={e.event_type === "TOOL_STARTED" ? "blue" : "green"}>
                  {e.event_type.replace("TOOL_", "")}
                </Tag>
              ),
              children: <Text code>{String(e.payload["name"] ?? "—")}</Text>,
            }))}
          />
        </div>
      )}
    </div>
  );
}
