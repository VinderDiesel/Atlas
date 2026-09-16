/**
 * lib/chart.ts 纯函数断言（0018 判据 10；面板 5 的确定性映射，0025 决策 ①~⑥）。
 *
 * 三条机器门槛：
 * - **零推断**：xKey/yKey 原样取自 spec——一旦有人在前端加「形态推断」
 *   （如按 data.length 换图型、按列名猜时间轴），本文件即红；
 * - **零发明**：数值字符串（`_jsonable` 的 Decimal → str 形态）转 number 是
 *   保真呈现；null 保留 null（不补 0 不插值——chart.py 的折线语义）；
 * - **table 渲染义务**（0025 决策 ④、工作项 6 前端半侧）：`note` 恒渲染、
 *   `skipped > 0` 时截断计数必须出现——渲染收敛到 tableFallbackLines 的返回值。
 */
import { describe, expect, it } from "vitest";

import { chartNumber, chartSeries, tableFallbackLines } from "../lib/chart";

describe("chart.ts 零推断映射（0025 决策 ①⑥）", () => {
  it("xKey/yKey 原样取自 spec：不猜测、不改写、不重算", () => {
    const series = chartSeries({
      x: "calendaryearid",
      y: ["total_trade_value"],
      data: [
        { x: 2014, y: "100.5" },
        { x: 2015, y: "120.5" },
      ],
    });
    expect(series.xKey).toBe("calendaryearid");
    expect(series.yKey).toBe("total_trade_value");
    expect(series.points).toEqual([
      { x: 2014, y: 100.5 },
      { x: 2015, y: 120.5 },
    ]);
  });

  it("无维度轴时 x 是「(行序)」字面量：原样透传，不重算行序", () => {
    const series = chartSeries({ x: "(行序)", y: ["cnt"], data: [{ x: 1, y: 2 }] });
    expect(series.xKey).toBe("(行序)");
  });

  it("行键恒为「x」「y」（chart.py 的 data 行形状）：语义列名不是行键", () => {
    // 2026-09-16 P3 走查回归：曾把 spec 语义列名当 Recharts dataKey 使用，
    // 行内取不到值 → 曲线无 d 路径、两轴刻度为空。行键由 chart.py 拍死。
    const series = chartSeries({
      x: "d_year",
      y: ["total_sales_price"],
      data: [{ x: 2000, y: "86269529.76" }],
    });
    expect(Object.keys(series.points[0] ?? {})).toEqual(["x", "y"]);
  });

  it("保持响应行序（chart.py：折线按行序连线，不排序不插值）", () => {
    const series = chartSeries({
      x: "d_year",
      y: ["v"],
      data: [
        { x: 2015, y: 5 },
        { x: 2013, y: 3 },
        { x: 2014, y: 4 },
      ],
    });
    expect(series.points.map((p) => p.x)).toEqual([2015, 2013, 2014]);
  });

  it("y 轴取首个且仅取首个（0025 决策 ⑥ 不扩多序列；其余序列不在前端发明）", () => {
    const series = chartSeries({
      x: "d",
      y: ["first_measure", "second_measure"],
      data: [{ x: 1, y: 10 }],
    });
    expect(series.yKey).toBe("first_measure");
  });
});

describe("chartNumber：_jsonable 形态 → number（null 不补 0）", () => {
  it("number 原样；非有限值 → null（坏数不画）", () => {
    expect(chartNumber(42)).toBe(42);
    expect(chartNumber(3.14)).toBe(3.14);
    expect(chartNumber(Number.NaN)).toBeNull();
    expect(chartNumber(Number.POSITIVE_INFINITY)).toBeNull();
    expect(chartNumber(Number.NEGATIVE_INFINITY)).toBeNull();
  });

  it("Decimal 字符串（保精度序列化形态，serving/api.py `_jsonable`）→ number", () => {
    expect(chartNumber("123.45")).toBe(123.45);
    expect(chartNumber("-0.5")).toBe(-0.5);
    expect(chartNumber("0")).toBe(0);
  });

  it("null/undefined/空串/非数值串/布尔 → null（不补 0 不插值）", () => {
    expect(chartNumber(null)).toBeNull();
    expect(chartNumber(undefined)).toBeNull();
    expect(chartNumber("")).toBeNull();
    expect(chartNumber("   ")).toBeNull();
    expect(chartNumber("abc")).toBeNull();
    expect(chartNumber(true)).toBeNull();
  });

  it("series 里的 NULL 点保留为 null（折线缺口，不补 0）", () => {
    const series = chartSeries({
      x: "d_year",
      y: ["v"],
      data: [
        { x: 2014, y: null },
        { x: 2015, y: "8" },
      ],
    });
    expect(series.points).toEqual([
      { x: 2014, y: null },
      { x: 2015, y: 8 },
    ]);
  });
});

describe("tableFallbackLines：table 分支的渲染义务（0025 决策 ④）", () => {
  it("note 恒渲染（第一条）；skipped=0 不追加", () => {
    expect(tableFallbackLines({ note: "结果不含数值列，以表格展示", skipped: 0 })).toEqual([
      "结果不含数值列，以表格展示",
    ]);
  });

  it("skipped > 0 追加截断计数（数字来自 spec 的 skipped，不编造）", () => {
    expect(
      tableFallbackLines({
        note: "行数 620 超过图表类目上限 200，降级表格（展示前 500 行）",
        skipped: 120,
      }),
    ).toEqual([
      "行数 620 超过图表类目上限 200，降级表格（展示前 500 行）",
      "另有 120 行未包含在图表 spec 中（降级截断）",
    ]);
  });
});
