/**
 * 运行状态仪表头（Run Status Instrument Header）。
 *
 * 将 RunView 元信息展示为仪器读数面板：标题行（run_id + 状态 + 结果种类）
 * 与读数行（session / last_seq / release / availability / replay_of）。
 *
 * 纪律：
 * - 只读 `detail` 已有字段，不推断执行时间（时刻只来自事件流的 occurred_at）；
 * - null 字段如实显示「—」（release_id / replay_of 缺失不编造）；
 * - 标识符与数字一律等宽（atlas-mono），状态色与 statusTag 同源。
 */
import { Tag, Typography } from "antd";

import type { RunView } from "../api/types";
import { statusTag } from "../panels/runs/run-view-model";

const { Text } = Typography;

interface Props {
  /** GET /runs/{id} 视图（固定 10 键；result 裁剪由服务端 ACL 决定）。 */
  detail: RunView;
}

/** 单个读数（标签 + 等宽值）。 */
function Gauge({ label, value }: { label: string; value: string }) {
  return (
    <span className="atlas-gauge">
      <span className="atlas-gauge__label">{label}</span>
      <span className="atlas-gauge__value atlas-mono">{value}</span>
    </span>
  );
}

export default function RunStatusHeader({ detail }: Props) {
  const meta = statusTag(detail.status);
  return (
    <div className="atlas-run-header" data-testid="run-status-header">
      <div className="atlas-run-header__title">
        <span className={`atlas-run-card__dot atlas-run-card__dot--${meta.color}`} />
        <Text className="atlas-mono atlas-run-header__id">{detail.run_id}</Text>
        <Tag color={meta.color}>{meta.label}</Tag>
        {detail.trace_summary.result_kind !== null && (
          <Tag color="geekblue">{detail.trace_summary.result_kind}</Tag>
        )}
      </div>
      <div className="atlas-run-header__gauges">
        <Gauge label="session" value={detail.session_id} />
        <Gauge label="last_seq" value={String(detail.last_seq)} />
        <Gauge label="release" value={detail.release_id ?? "—"} />
        <Gauge label="availability" value={detail.result_availability} />
        <Gauge label="replay_of" value={detail.replay_of ?? "—"} />
      </div>
    </div>
  );
}
