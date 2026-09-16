/**
 * lib/order.ts 纯函数断言（0018 判据 9 的「四态分支判定 + 渲染顺序」）。
 *
 * 这是「工作台渲染顺序符合决策 ⑦（指标口径先于 SQL 先于数据）」的机器门槛：
 * 顺序一旦被改（例如把 SQL 提到指标口径前），本文件即红。只测纯函数，
 * 不由渲染快照断言（判据 9/10 的纪律）。
 */
import { describe, expect, it } from "vitest";

import { ANSWER_SECTIONS, TURN_KINDS, branchOf, type AnswerSection } from "../lib/order";

const rank = (section: AnswerSection): number => ANSWER_SECTIONS.indexOf(section);

describe("order.ts（0018 判据 9）", () => {
  it("answer 段落次序编码决策 ⑦：指标口径 → 行数/耗时 → SQL → 数据 → 截断声明 → 图表", () => {
    expect(rank("metric")).toBeLessThan(rank("counts"));
    expect(rank("counts")).toBeLessThan(rank("sql"));
    expect(rank("sql")).toBeLessThan(rank("data"));
    expect(rank("data")).toBeLessThan(rank("truncation"));
    // ⑥ 图表（P3，ADR-0025 决策 ②）：置于数据与截断声明之后（0018 ⑦ 锁定）
    expect(rank("truncation")).toBeLessThan(rank("chart"));
  });

  it("五态 kind 全部有分支，且 fields 非空", () => {
    for (const kind of TURN_KINDS) {
      const spec = branchOf(kind);
      expect(spec?.kind).toBe(kind);
      expect(spec?.fields.length).toBeGreaterThan(0);
    }
  });

  it("未知 kind 返回 null（不猜分支，调用方走原始 payload 展示）", () => {
    expect(branchOf("vibes")).toBeNull();
    expect(branchOf("")).toBeNull();
  });

  it("answer 分支的字段集与 §3.4 的顺序表逐字对齐", () => {
    expect(branchOf("answer")?.fields).toEqual([
      "metric",
      "explanation",
      "row_count",
      "latency_ms",
      "sql",
      "columns",
      "rows",
    ]);
  });

  it("blocked 分支必须消费 block_reason 与 validation_issues（§3.4 安全分支）", () => {
    expect(branchOf("blocked")?.fields).toEqual(["block_reason", "validation_issues"]);
  });

  it("error 分支消费 error 原文；clarify 分支消费 clarification；handoff 带 path 痕迹", () => {
    expect(branchOf("error")?.fields).toEqual(["error"]);
    expect(branchOf("clarify")?.fields).toEqual(["clarification"]);
    expect(branchOf("handoff")?.fields).toEqual(["handoff_reason", "path"]);
  });
});
