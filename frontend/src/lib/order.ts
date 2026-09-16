/**
 * 渲染顺序与四态分支判定（纯函数；0018 判据 9 的 vitest 断言对象）。
 *
 * 纪律（设计页 §4.1）：本文件不 import React / fetch，只做「kind → 分支」
 * 与「answer → 段落次序」的确定性判定。组件按 ANSWER_SECTIONS 的次序渲染，
 * 次序被改动时 __tests__/order.test.ts 即红——把决策 ⑦ 的渲染顺序
 * 从「人工走查」变成「机器门槛」（设计页 §8 探针 3）。
 */

/** 回合终端类型（与 agent/state.py 的 TurnKind 逐字对齐，5 态）。 */
export type TurnKind = "answer" | "clarify" | "blocked" | "error" | "handoff";

export const TURN_KINDS: readonly TurnKind[] = [
  "answer",
  "clarify",
  "blocked",
  "error",
  "handoff",
];

/**
 * answer 分支的渲染段落，数组序即决策 ⑦ 锁定序：
 * ① 指标口径 → ② 行数/耗时 → ③ 出口 SQL → ④ 数据 → ⑤ 截断声明 → ⑥ 图表。
 * （⑥ 图表由 ADR-0025 决策 ② 的独立 `chart` 键驱动（P3）；置于数据之后、
 * 截断声明之后再收口——`chart === null` 时不渲染占位，见 ChartBlock。）
 */
export const ANSWER_SECTIONS = ["metric", "counts", "sql", "data", "truncation", "chart"] as const;

export type AnswerSection = (typeof ANSWER_SECTIONS)[number];

export interface BranchSpec {
  readonly kind: TurnKind;
  /** 该分支必须消费的 `_turn_payload` 字段（数组序 = 展示序；§3.4 四态分支表）。 */
  readonly fields: readonly string[];
}

const BRANCH_FIELDS: Record<TurnKind, readonly string[]> = {
  // explanation 承载 metric_expression / dimensions / time / filters（§3.4 ①）
  answer: ["metric", "explanation", "row_count", "latency_ms", "sql", "columns", "rows"],
  clarify: ["clarification"],
  blocked: ["block_reason", "validation_issues"],
  error: ["error"],
  // handoff：原因 + path 的候选链痕迹（§3.4 降级分支）
  handoff: ["handoff_reason", "path"],
};

/**
 * kind → 分支规格；未知 kind 返回 `null`。
 *
 * 返回 null 而非抛错：契约演进（新 kind）时调用方应展示原始 payload 并标注
 * 未知分支，而不是白屏——未知分支不猜（诚实性优先于渲染完备性）。
 */
export function branchOf(kind: string): BranchSpec | null {
  const fields = (BRANCH_FIELDS as Record<string, readonly string[] | undefined>)[kind];
  if (fields === undefined) {
    return null;
  }
  return { kind: kind as TurnKind, fields };
}
