/**
 * 接地叙述块（NarrativeBlock）：消费 ADR-0029 的条件键 `TurnPayload.narrative`。
 *
 * 诚实边界（本组件的存在意义，改渲染前先读）：
 * - **降级不伪装**（③）：`fallback=true` 或 `grounded=false` 时，文本是**确定性模板**
 *   （非 LLM 生成），一律以「参考摘要（模板）」如实标注，绝不呈现为 AI 洞察。
 * - **溯源**（①）：`grounded=true` 才是自托管模型生成的补充文本，展示 `tier`/`model`
 *   溯源，并声明「数值已接地于本次结果，不构成因果解释」。
 * - **零重算 / 不扩写**（N1）：只渲染后端原样 `text`，前端不拼接、不再算、不改写数字。
 * - `narrative` 缺失（off / 未请求）时组件渲染 null，不占位、不推断。
 */
import { Alert, Card, Space, Tag, Typography } from "antd";

import type { NarrativeBlock as NarrativePayload } from "../../api/types";

interface Props {
  narrative: NarrativePayload | undefined;
}

export default function NarrativeBlock({ narrative }: Props) {
  if (narrative === undefined) {
    return null;
  }
  const shipped = narrative.grounded && !narrative.fallback;

  if (!shipped) {
    // 降级 / 未接地：确定性模板，如实标注，不伪装成生成文本（③）。
    return (
      <Alert
        type="info"
        showIcon
        message="参考摘要（确定性模板，非模型生成）"
        description={
          <Space direction="vertical" size={0}>
            <Typography.Text>{narrative.text}</Typography.Text>
            <Typography.Text type="secondary">
              reason_code={narrative.reason_code ?? "未知"} · tier={narrative.tier}
            </Typography.Text>
          </Space>
        }
      />
    );
  }

  // 接地发货：自托管模型生成的补充叙述，带溯源，声明不构成因果（①②）。
  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <span>模型补充叙述</span>
          <Tag color="blue">tier={narrative.tier}</Tag>
          <Tag>model={narrative.model}</Tag>
        </Space>
      }
    >
      <Space direction="vertical" size={0}>
        {/* 后端原样文本，前端不重算、不改写数字（N1） */}
        <Typography.Paragraph style={{ marginBottom: 0 }}>
          {narrative.text}
        </Typography.Paragraph>
        <Typography.Text type="secondary">
          数值已接地于本次查询结果；为定性补充，不构成业务因果解释。
        </Typography.Text>
      </Space>
    </Card>
  );
}
