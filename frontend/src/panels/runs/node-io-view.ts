/**
 * 节点 I/O 结构化摘要（Phase 1 可视化）。
 *
 * 将原始 node_io 字典转为人类可读的分层摘要：Plan 对象显示 metric/dimensions/
 * time/filters_count；SQL 截断为前 80 字符；数组显示项数；标量直接展示。
 * 完整 JSON 在「展开」面板中查看。
 */

/** 格式化后的单条 I/O 条目。 */
export interface FormattedIOEntry {
  key: string;
  display: string;
  /** 是否为复合值（Plan/数组等），前端可据此决定是否显示展开按钮。 */
  expandable: boolean;
}

/** 节点中文标签（与 agent/graph.py GRAPH_NODE_IDS 对齐）。 */
export const NODE_LABELS: Record<string, string> = {
  plan: "计划",
  retrieve: "检索",
  generate: "生成",
  validate: "校验",
  execute: "执行",
  explain: "解释",
  clarify: "反问",
  handoff: "人工接管",
};

/** 将 Plan 对象格式化为紧凑摘要。 */
function formatPlan(plan: Record<string, unknown>): string {
  const metric = (plan["metric"] as string) ?? "?";
  const dims = (plan["dimensions"] as string[]) ?? [];
  const time = plan["time"] as Record<string, unknown> | null;
  const filters = (plan["filters"] as unknown[]) ?? [];
  const parts: string[] = [`指标: ${metric}`];
  if (dims.length > 0) {
    parts.push(`维度: ${dims.join(", ")}`);
  }
  if (time) {
    parts.push(`时间: ${time["granularity"]}/${time["value"]}`);
  }
  if (filters.length > 0) {
    parts.push(`过滤: ${filters.length} 项`);
  }
  const limit = plan["limit"] as number | undefined;
  if (limit !== undefined && limit !== 100) {
    parts.push(`LIMIT: ${limit}`);
  }
  return parts.join(" | ");
}

/** 截断长字符串。 */
function truncate(value: string, maxLen: number = 80): string {
  if (value.length <= maxLen) return value;
  return value.slice(0, maxLen) + "…";
}

/** 将单个 node_io 值格式化为可读字符串。 */
function formatValue(key: string, value: unknown): { display: string; expandable: boolean } {
  if (value === null || value === undefined) {
    return { display: "—", expandable: false };
  }
  // Plan 对象
  if (key === "plan" && typeof value === "object" && !Array.isArray(value)) {
    return { display: formatPlan(value as Record<string, unknown>), expandable: true };
  }
  // ClarificationRequest 对象
  if (key === "clarification" && typeof value === "object" && !Array.isArray(value)) {
    const clar = value as Record<string, unknown>;
    const kind = clar["kind"] as string ?? "?";
    const reasons = (clar["reasons"] as string[]) ?? [];
    return { display: `[${kind}] ${reasons.join("; ")}`, expandable: true };
  }
  // SQL 字符串
  if (key === "sql" && typeof value === "string") {
    return { display: truncate(value), expandable: value.length > 80 };
  }
  // 数组
  if (Array.isArray(value)) {
    return { display: `${value.length} 项`, expandable: true };
  }
  // 对象（非 Plan/Clarification）
  if (typeof value === "object") {
    const json = JSON.stringify(value, null, 2);
    return { display: truncate(json, 60), expandable: true };
  }
  // 标量
  return { display: String(value), expandable: false };
}

/** 将 node_io 字典转为结构化摘要条目列表。 */
export function formatNodeIO(io: Record<string, unknown>): FormattedIOEntry[] {
  return Object.entries(io).map(([key, value]) => ({
    key,
    ...formatValue(key, value),
  }));
}
