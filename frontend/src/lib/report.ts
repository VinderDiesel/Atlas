/**
 * 报告体展开纯函数（工作项 9；主报告钻取的结构化渲染原料）。
 *
 * 纪律：
 * - 不 import React / fetch；不假设 runner.py 产物的嵌套形状（summary 按域分节、
 *   by_lang 内再按语言分节是**当前**形态，展开逻辑不得写死具体路径）；
 * - 0018 判据 10 的 vitest 断言对象是 `__tests__/report.test.ts`。
 */

/** 平铺行：点分路径（如 `finance.by_lang.zh.ex`）→ 值文本。 */
export interface FlatKvRow {
  path: string;
  value: string;
}

/**
 * 叶子值文本（诚实性口径）：
 * - `null` → `"null"`（与本表自己的空串 `""` 区分——缺失与显式 null 不同源）；
 * - 字符串原样（不加引号——引号会与值内容混淆）；
 * - 数组 / 空对象 → 紧凑 JSON（保原文；下标不是字段名，不按下标展开）；
 * - number / boolean → `String()`（0 与 false 如实显示，不误显示为缺失）。
 */
function leafText(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

/**
 * 递归展开为「路径 → 值」平铺行（深度优先，保持 JSON 原文的键序）。
 *
 * 非空对象递归下钻；其余（含空对象、数组、标量）作叶子成行。空对象成行
 * `{}` 是刻意的：静默消失会让「该节无数据」与「该节不存在」无法区分。
 * `prefix` 仅供递归内部拼接；顶层传空串时路径从首层键名开始。
 */
export function flattenKv(value: unknown, prefix = ""): FlatKvRow[] {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length > 0) {
      const rows: FlatKvRow[] = [];
      for (const [key, child] of entries) {
        rows.push(...flattenKv(child, prefix === "" ? key : `${prefix}.${key}`));
      }
      return rows;
    }
  }
  return [{ path: prefix === "" ? "(值)" : prefix, value: leafText(value) }];
}

/**
 * samples 表格的首选键序（runner.py 当前 11 键的展示顺序）。
 * 仅决定**列序**；缺席键不成列、契约外键追加——「不挑好看的键」的实现口径。
 */
export const SAMPLE_PREFERRED_KEYS: readonly string[] = [
  "id",
  "question",
  "domain",
  "lang",
  "ambiguous",
  "plan_ok",
  "ex",
  "row_count",
  "hash",
  "sql",
  "columns",
];

/**
 * 样本表列 = 首选键（存在者，按 SAMPLE_PREFERRED_KEYS 序）+ 其余键按首见顺序追加。
 *
 * 全键并集：任何样本出现过的键都必须成列（含契约外演进键），缺样本格显示
 * 「—」（由渲染层负责，本函数只输出列名）。
 */
export function sampleColumns(samples: readonly Record<string, unknown>[]): string[] {
  const columns: string[] = [];
  for (const key of SAMPLE_PREFERRED_KEYS) {
    if (samples.some((sample) => key in sample)) {
      columns.push(key);
    }
  }
  for (const sample of samples) {
    for (const key of Object.keys(sample)) {
      if (!columns.includes(key)) {
        columns.push(key);
      }
    }
  }
  return columns;
}

/** 单元格文本：null/ex 缺失 → 「—」，其余走 leafText 同口径。 */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return leafText(value);
}
