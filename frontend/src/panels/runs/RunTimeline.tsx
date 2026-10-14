/**
 * 运行时间线（T06d；ADR-0031 D07③「图与时间线由同一事件流归约」）。
 *
 * 数据面与图同源（`RunViewState.timeline`，由 `reduceRunEvent` 归约）——本组件
 * **不自行请求**：单一事件流是运行事实的唯一前端来源，时间线不是第二数据源。
 *
 * 展示形态（Phase 2a）：事件日志（Event Log）——每条事件一行：状态圆点 +
 * seq 徽章 + 事件类型 Tag + 节点 + 服务端时刻 + 安全摘要。
 *
 * 列纪律：
 * - `seq` 是唯一序证据（服务端事务递增）；`occurred_at` 是服务端时钟原文
 *   （前端不从 UI 时间推断执行，也不本地化改写）；
 * - 摘要列走 `payloadSummary`（脱敏通道内容；超长截断），不内联原始 payload；
 * - 空态文案诚实（运行刚创建或事件流未连接），不伪造行。
 */
import { Tag, Typography } from "antd";

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
    <ol className="atlas-event-log">
      {events.map((event) => {
        const color = EVENT_COLORS[event.event_type];
        return (
          <li key={event.seq} className="atlas-event-log__item">
            <span
              className={`atlas-event-log__dot atlas-event-log__dot--${color}`}
              aria-hidden="true"
            />
            <span className="atlas-event-log__seq atlas-mono">{`#${event.seq}`}</span>
            <Tag color={color} className="atlas-event-log__tag">
              {event.event_type}
            </Tag>
            <span className="atlas-event-log__node atlas-mono">
              {event.node_id === null ? "—" : event.node_id}
            </span>
            <span className="atlas-event-log__time atlas-mono">{event.occurred_at}</span>
            <span className="atlas-event-log__summary">
              {payloadSummary(event.payload) || "—"}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
