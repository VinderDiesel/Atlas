/**
 * lib/report.ts 纯函数断言（0018 判据 10；工作项 9 主报告钻取的结构化展开）。
 *
 * 三条机器门槛：
 * - **不假设结构**：summary 递归展开为「点分路径 → 值」平铺行——嵌套层数由
 *   runner.py 的产物决定，前端不写死 `finance.by_lang.zh.ex` 之类的具体路径；
 * - **不静默消失**：空对象显式成行（`{}`），数组整体作叶子（下标不是字段名，
 *   按下标展开会发明并不存在的键）；
 * - **不挑好看的键**：samples 列 = 首选键序（存在者）在前 + 其余键按首见顺序
 *   追加——任何样本里出现过的键都必须成列。
 */
import { describe, expect, it } from "vitest";

import { flattenKv, sampleColumns } from "../lib/report";

describe("flattenKv：递归展开为「路径 → 值」平铺行（不假设结构）", () => {
  it("两层嵌套：点分路径 + 保持插入序（与 JSON 原文顺序一致）", () => {
    const rows = flattenKv({
      finance: { total: 79, plan_acc: "73/73" },
      retail: { total: 27 },
    });
    expect(rows).toEqual([
      { path: "finance.total", value: "79" },
      { path: "finance.plan_acc", value: "73/73" },
      { path: "retail.total", value: "27" },
    ]);
  });

  it("三层嵌套（by_lang）：完整路径逐级拼接，不截断任何一层", () => {
    const rows = flattenKv({ finance: { by_lang: { zh: { ex: "65/65" } } } });
    expect(rows).toEqual([{ path: "finance.by_lang.zh.ex", value: "65/65" }]);
  });

  it("数组整体作叶子（紧凑 JSON）：下标不是字段名，不按下标展开", () => {
    const rows = flattenKv({ domains: ["finance", "retail"], empty: [] });
    expect(rows).toEqual([
      { path: "domains", value: '["finance","retail"]' },
      { path: "empty", value: "[]" },
    ]);
  });

  it("空对象显式成行（`{}`）：不得从展开表里静默消失", () => {
    expect(flattenKv({ side: {} })).toEqual([{ path: "side", value: "{}" }]);
  });

  it("五态不混淆：null → \"null\"、空串 → \"\"、false → \"false\"、0 → \"0\"", () => {
    expect(flattenKv({ a: null, b: "", c: false, d: 0, e: "0" })).toEqual([
      { path: "a", value: "null" },
      { path: "b", value: "" },
      { path: "c", value: "false" },
      { path: "d", value: "0" },
      { path: "e", value: "0" },
    ]);
  });

  it("顶层非对象（防御形态）：单行 path「(值)」——不发明键名", () => {
    expect(flattenKv(42)).toEqual([{ path: "(值)", value: "42" }]);
  });
});

describe("sampleColumns：首选键序 + 全键并集（不挑好看的键）", () => {
  it("首选键（存在者）在前，其余键按首见顺序追加；跨样本并集补齐", () => {
    const columns = sampleColumns([
      { id: "g-01", question: "…", sql: "SELECT 1", columns: ["a"], zz_custom: 1 },
      { row_count: 3, id: "g-02", hash: "abc" },
    ]);
    expect(columns).toEqual(["id", "question", "row_count", "hash", "sql", "columns", "zz_custom"]);
  });

  it("空样本集 → 空列（调用方渲染空态，不伪造表头）", () => {
    expect(sampleColumns([])).toEqual([]);
  });

  it("缺席的首选键不成列（不发明样本里没有的字段）", () => {
    expect(sampleColumns([{ id: "g-01" }])).toEqual(["id"]);
  });
});
