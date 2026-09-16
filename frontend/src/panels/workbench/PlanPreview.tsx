/**
 * PlanPreview：`/api/v1/plan` 的产物（工作台的「只看计划不执行」）。
 *
 * 「执行此计划」动作消费 `/api/v1/plan/execute`（0022 决策 ③）：执行收到的 Plan
 * **原文**，不提供编辑字段——手改 Plan 的编辑面属 P2 的 PlanEditor 语义，P1 以
 * dev-plan 的 6 组件清单为准（消费并入本组件）。
 */
import { Alert, Button, Descriptions, Space } from "antd";

import type { ClarifyPayload, PlanPayload } from "../../api/types";

interface Props {
  kind: "plan" | "clarify";
  plan: PlanPayload | null;
  clarification: ClarifyPayload | null;
  executing: boolean;
  onExecute: (plan: PlanPayload) => void;
  /** 点击澄清候选：回填输入框并重发（与 ask 的 clarify 分支同交互）。 */
  onPickCandidate: (candidate: string) => void;
}

export default function PlanPreview({
  kind,
  plan,
  clarification,
  executing,
  onExecute,
  onPickCandidate,
}: Props) {
  if (kind === "clarify") {
    return (
      <Alert
        type="warning"
        showIcon
        message="Plan 未生成：需要澄清（Planner 不猜）"
        description={
          <Space direction="vertical">
            {(clarification?.reasons ?? ["（无澄清详情）"]).map((reason, i) => (
              <span key={`${i}-${reason}`}>{reason}</span>
            ))}
            {clarification !== null && clarification.candidates.length > 0 && (
              <Space wrap>
                {clarification.candidates.map((candidate) => (
                  <Button key={candidate} size="small" onClick={() => onPickCandidate(candidate)}>
                    {candidate}
                  </Button>
                ))}
              </Space>
            )}
          </Space>
        }
      />
    );
  }
  if (plan === null) {
    return null;
  }
  const items = [
    { key: "metric", label: "指标", children: plan.metric },
    {
      key: "dims",
      label: "维度",
      children: plan.dimensions.length > 0 ? plan.dimensions.join("、") : "（无）",
    },
    {
      key: "time",
      label: "时间",
      children:
        plan.time !== null ? `${plan.time.value} · ${plan.time.granularity}` : "（无）",
    },
    {
      key: "filters",
      label: "过滤",
      children:
        plan.filters.length > 0
          ? plan.filters.map((f) => `${f.column} ${f.op} ${String(f.value)}`).join("；")
          : "（无）",
    },
    {
      key: "order",
      label: "排序",
      children:
        plan.order_by.length > 0
          ? plan.order_by.map((o) => `${o.column} ${o.desc ? "DESC" : "ASC"}`).join("、")
          : "（无）",
    },
    { key: "limit", label: "行数上限（limit）", children: String(plan.limit) },
  ];
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Descriptions size="small" column={2} bordered items={items} />
      <Button type="primary" ghost loading={executing} onClick={() => onExecute(plan)}>
        执行此计划
      </Button>
    </Space>
  );
}
