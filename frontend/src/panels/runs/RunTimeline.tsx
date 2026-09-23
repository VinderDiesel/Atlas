/**
 * 运行时间线（T06d；ADR-0031 D07③「图与时间线由同一事件流归约」）。
 *
 * 数据面与图同源（`RunViewState.timeline`，由 `reduceRunEvent` 归约）——本组件
 * **不自行请求**：单一事件流是运行事实的唯一前端来源，时间线不是第二数据源。
 *
 * 列纪律：
 * - `seq` 是唯一序证据（服务端事务递增）；`occurred_at` 是服务端时钟原文
 *   （前端不从 UI 时间推断执行，也不本地化改写）；
 * - 摘要列走 `payloadSummary`（脱敏通道内容；超长截断），不内联原始 payload；
 * - 空态文案诚实（运行刚创建或事件流未连接），不伪造行。
 */
import { Table, Tag, Typography } from "antd";

import type { RunEvent, RunEventType } from "../../api/types";

import { payloadSummary } from "./run-view-model";

interface Props {
  /** 已归约的事件序列（按 seq 升序；来自 RunViewState.timeline）。 */
  events: readonly RunEvent[];
}

/** 事件类型 → Tag 色（纯展示映射；类型字面量与 contracts.py 同源 13 个）。 */
const EVENT_COLORS: Record<RunEventType, string> = {
  RUN_ACCEPTED: "default",
  RUN_STARTED: "blue",
  NODE_STARTED: "blue",
  NODE_FINISHED: "green",
  NODE_FAILED: "red",
  NODE_SKIPPED: "orange",
  EDGE_TAKEN: "purple",
  TOOL_STARTED: "geekblue",
  TOOL_FINISHED: "geekblue",
  FALLBACK: "volcano",
  STATE_SNAPSHOT: "gold",
  RUN_FINISHED: "green",
  RUN_INTERRUPTED: "red",
};

export default function RunTimeline({ events }: Props) {
  if (events.length === 0) {
    return (
      <Typography.Text type="secondary">
        尚无事件（运行刚创建，或事件流尚未连接）
      </Typography.Text>
    );
  }
  return (
    <Table
      size="small"
      rowKey="seq"
      pagination={false}
      scroll={{ y: 320 }}
      dataSource={events as RunEvent[]}
      columns={[
        { title: "seq", dataIndex: "seq", width: 64 },
        {
          title: "事件",
          dataIndex: "event_type",
          width: 168,
          render: (value: RunEventType) => <Tag color={EVENT_COLORS[value]}>{value}</Tag>,
        },
        {
          title: "节点",
          dataIndex: "node_id",
          width: 112,
          render: (value: string | null) => (value === null ? "—" : value),
        },
        { title: "时刻（服务端时钟）", dataIndex: "occurred_at", width: 216 },
        {
          title: "摘要",
          key: "payload",
          render: (_: unknown, event: RunEvent) => payloadSummary(event.payload) || "—",
        },
      ]}
    />
  );
}
