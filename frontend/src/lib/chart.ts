/**
 * 面板 5 的确定性映射（纯函数；0018 判据 10 的 vitest 断言对象）。
 *
 * 消费后端 `chart` 键（ADR-0025 决策 ② 的 spec；chart.py `render_chart` 产物）：
 * - **零推断**：图表类型（bar/line/table）与轴选择（x/y）是 chart.py 的裁定，
 *   本文件不做任何形态推断——没有 `data.length > N` 改图型之类的分支
 *   （0025 决策 ①④⑥；设计页 §6.1「前端零图表类型决策」）；
 * - **零发明**：轴键原样取自 spec（x 可能是 `(行序)` 字面量；y 恒 1 元，
 *   不扩多序列）；数值字符串（`_jsonable` 的 Decimal → str 形态）转 number
 *   是保真呈现，null 保留 null（不补 0 不插值——chart.py 折线语义）；
 * - **table 渲染义务**（0025 决策 ④、工作项 6 前端半侧）：`note` 恒渲染、
 *   `skipped > 0` 时截断计数必须出现——组件只渲染 tableFallbackLines 的
 *   返回值，义务收敛在此（vitest 即门槛）。
 *
 * 纪律：不 import React / fetch（设计页 §4.1；组件在 panels/chart/ 消费本文件）。
 */

/** bar/line spec 的消费形状（结构上被 api/types.ts 的 ChartBarSpec | ChartLineSpec 满足）。 */
export interface SeriesSpecFacts {
  /** x 轴列名（无维度轴时为 `(行序)` 字面量）。 */
  x: string;
  /** y 轴列名（恒 1 元；取首个且仅取首个）。 */
  y: readonly string[];
  /** 已执行 rows 的轴投影（原序）。 */
  data: readonly { x: unknown; y: unknown }[];
}

export interface ChartSeries {
  /** spec 语义列名（展示/断言用；`(行序)` 时为该字面量）。 */
  xKey: string;
  /** 首个 y 语义列名（0025 决策 ⑥ 不扩多序列）。 */
  yKey: string;
  /**
   * Recharts 行数据（原序；y 已转数值或 null）。
   * **行键恒为 "x"/"y"**（chart.py 的 data 行形状）——Recharts 的 dataKey
   * 直接消费这两个字面量；`xKey`/`yKey` 语义列名不得用作行键（否则数据点
   * 取不到值，2026-09-16 P3 走查实测）。
   */
  points: { x: unknown; y: number | null }[];
}

/**
 * 单点 y 值 → number | null（忠实转换，不发明）：
 * - number：有限值原样，NaN/Inf → null（坏数不画，与 chart.py 拒绝语义对齐）；
 * - string：非空串按数值解析（`_jsonable` 的 Decimal → str 形态），非数值 → null；
 * - null/undefined/布尔/空串 → null（**不补 0**——折线留缺口，不伪造数据点）。
 */
export function chartNumber(raw: unknown): number | null {
  if (typeof raw === "number") {
    return Number.isFinite(raw) ? raw : null;
  }
  if (typeof raw === "string" && raw.trim() !== "") {
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/**
 * spec → Recharts 映射（零推断：语义名原样透传，data 保序；行键恒
 * "x"/"y" 见 ChartSeries——语义名与行键分离）。
 */
export function chartSeries(spec: SeriesSpecFacts): ChartSeries {
  return {
    xKey: spec.x,
    yKey: spec.y[0],
    points: spec.data.map((point) => ({ x: point.x, y: chartNumber(point.y) })),
  };
}

/** table 分支的渲染义务输入（api/types.ts 的 ChartTableSpec 满足之）。 */
export interface TableFallbackFacts {
  /** chart.py 的 note 恒存键（降级原因原文）。 */
  note: string;
  /** 被截断的行数（0 = 未截断）。 */
  skipped: number;
}

/**
 * table 分支的渲染行（数组序 = 展示序）：
 * 第一条恒为 `note` 原文；`skipped > 0` 时追加截断计数（数字来自 spec，
 * 文案不得声称后端细节——只说「未包含在图表 spec 中」这一事实）。
 */
export function tableFallbackLines(spec: TableFallbackFacts): string[] {
  const lines = [spec.note];
  if (spec.skipped > 0) {
    lines.push(`另有 ${spec.skipped} 行未包含在图表 spec 中（降级截断）`);
  }
  return lines;
}
