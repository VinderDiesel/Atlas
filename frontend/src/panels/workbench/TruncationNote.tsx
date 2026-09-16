/**
 * ⑤ 截断声明（设计页 §3.4 ⑤）：渲染上限提示 + 「可能被 Plan 的 limit 截断」
 * 不确定语气提示（G3 未进契约的降级形态，降级记录在 KL）。
 *
 * 判定全部来自 lib/honesty.ts（本组件只渲染，不改判）；两个提示都为 null 时
 * 不渲染任何内容（不占位、不暗示）。
 */
import { Alert, Space, Typography } from "antd";

import type { TruncationState } from "../../lib/honesty";

interface Props {
  state: TruncationState;
}

export default function TruncationNote({ state }: Props) {
  if (state.renderNote === null && state.limitNote === null) {
    return null;
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {state.renderNote !== null && <Alert type="info" showIcon message={state.renderNote} />}
      {state.limitNote !== null && <Alert type="warning" showIcon message={state.limitNote} />}
      <Typography.Text type="secondary">
        完整数据出口：atlas query --format json（同一只读网关）。
      </Typography.Text>
    </Space>
  );
}
