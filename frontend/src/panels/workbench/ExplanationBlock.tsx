/**
 * ① 指标口径块（设计页 §3.4 answer 段 ①；渲染位置由 ANSWER_SECTIONS 驱动）。
 *
 * 展示 metric + metric_expression + dimensions/time/filters，并附归因附加项
 * （tables / data_version / data_refreshed_at / path·engine / policy_effect）。
 * explanation 为 null 时不编造口径，只如实标注缺失。
 */
import { Descriptions, Typography } from "antd";
import type { ReactNode } from "react";

import type { Explanation } from "../../api/types";

interface Props {
  metric: string | null;
  explanation: Explanation | null;
}

export default function ExplanationBlock({ metric, explanation }: Props) {
  if (explanation === null) {
    return (
      <Typography.Text type="secondary">
        无归因（explanation 为 null）：指标 = {metric ?? "未知"}
      </Typography.Text>
    );
  }
  const items: { key: string; label: string; children: ReactNode }[] = [
    { key: "metric", label: "指标", children: explanation.metric },
    {
      key: "expr",
      label: "口径（表达式）",
      children: explanation.metric_expression !== "" ? explanation.metric_expression : "（语义层无表达式）",
    },
    {
      key: "dims",
      label: "维度",
      children: explanation.dimensions.length > 0 ? explanation.dimensions.join("、") : "（无）",
    },
    { key: "time", label: "时间", children: explanation.time ?? "（无）" },
    {
      key: "filters",
      label: "过滤",
      children: explanation.filters.length > 0 ? explanation.filters.join("；") : "（无）",
    },
    {
      key: "tables",
      label: "数据表",
      children: explanation.tables.length > 0 ? explanation.tables.join("、") : "（无）",
    },
    { key: "ver", label: "数据版本", children: explanation.data_version ?? "（未绑定快照）" },
    { key: "at", label: "数据刷新于", children: explanation.data_refreshed_at ?? "（未提供）" },
    { key: "route", label: "链路", children: `${explanation.path} · ${explanation.engine}` },
  ];
  if (explanation.policy_effect !== undefined) {
    items.push({ key: "policy", label: "行级策略", children: explanation.policy_effect });
  }
  return <Descriptions size="small" column={2} bordered items={items} />;
}
