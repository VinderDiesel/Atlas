/**
 * lib/honesty.ts 纯函数断言：
 * - 0018 判据 10：治理面板对值域 skipped 空壳、报告 structured/dry、
 *   同义词 empty_placeholder、快照 is_latest 的显式降级（五类标志位，§3.3）；
 * - §3.4 ⑤：工作台截断声明的两个文案（G3 未进契约 → 不确定语气降级）。
 *
 * 文案是渲染义务的一部分：改动 lib/honesty.ts 的文案必须同步本文件的
 * 逐字断言（改文案即改诚实性表述，设计页 §3.3 引言）。
 */
import { describe, expect, it } from "vitest";

import {
  LATEST_SNAPSHOT_BADGE,
  RENDER_CAP,
  dryNote,
  extractLimit,
  latestSnapshotSha,
  partitionReports,
  partitionValueDomains,
  reportDegradeNote,
  synonymsBanner,
  truncationState,
  valueDomainGroup,
  valueSkipNote,
} from "../lib/honesty";

describe("honesty.ts 治理五标志位（0018 判据 10）", () => {
  it("值域分组由 status 驱动：skipped 必进跳过组，空数组也不得读成「0 个值」", () => {
    expect(
      valueDomainGroup({ status: "skipped", skip_reason: "distinct=2715 超过阈值 200", values_count: 0 }),
    ).toBe("skipped");
    expect(valueDomainGroup({ status: "registered", skip_reason: null, values_count: 4 })).toBe("covered");
  });

  it("跳过提示引用 skip_reason 原文；缺失时如实标注缺失，不补造原因", () => {
    expect(
      valueSkipNote({ status: "skipped", skip_reason: "distinct=2715 超过阈值 200", values_count: 0 }),
    ).toBe("值域未采集：distinct=2715 超过阈值 200");
    expect(valueSkipNote({ status: "registered", skip_reason: null, values_count: 4 })).toBeNull();
    expect(valueSkipNote({ status: "skipped", skip_reason: null, values_count: 0 })).toBe(
      "值域未采集：<skip_reason 缺失>",
    );
  });

  it("值域分组函数：保持响应原顺序分两组（跳过组数据源；展开由组件默认键承担）", () => {
    const items = [
      { status: "registered", skip_reason: null, values_count: 4, field: "ExchangeID" },
      { status: "skipped", skip_reason: "distinct=2715 超过阈值 200", values_count: 0, field: "Branch" },
      { status: "registered", skip_reason: null, values_count: 9, field: "status" },
    ];
    const { covered, skipped } = partitionValueDomains(items);
    expect(covered.map((item) => item.field)).toEqual(["ExchangeID", "status"]);
    expect(skipped.map((item) => item.field)).toEqual(["Branch"]);
    expect(partitionValueDomains([])).toEqual({ covered: [], skipped: [] });
  });

  it("报告：structured=false 走降级文案并携带 pattern；主报告为 null", () => {
    expect(reportDegradeNote({ structured: false, pattern: "rls-verify-<sha>", dry: null })).toBe(
      "非统一结构报告（模式：rls-verify-<sha>），以下为原始 JSON",
    );
    expect(reportDegradeNote({ structured: true, pattern: "<sha>", dry: false })).toBeNull();
  });

  it("报告：dry=true 的标注必须出现，且不是「EX=0」的说法", () => {
    const note = dryNote({ structured: true, pattern: "<sha>", dry: true });
    expect(note).toBe("dry 运行：未连 DB，EX 不参与统计");
    expect(dryNote({ structured: true, pattern: "<sha>", dry: false })).toBeNull();
    expect(dryNote({ structured: false, pattern: "x-<sha>", dry: null })).toBeNull();
  });

  it("同义词：empty_placeholder=true 直接引用 authority_note 原文", () => {
    const note = "中文同义词的权威源在 ossie 模型的 ai_context.synonyms；本文件为空占位不代表无同义词";
    expect(synonymsBanner({ empty_placeholder: true, authority_note: note })).toBe(note);
    expect(synonymsBanner({ empty_placeholder: false, authority_note: note })).toBeNull();
    expect(synonymsBanner({ empty_placeholder: true, authority_note: null })).toBe("空占位（权威源说明缺失）");
  });

  it("快照清单：最新标志由 is_latest_by_created_at 驱动，不按文件名排序", () => {
    const items = [
      { sha: "dc4f350", is_latest_by_created_at: false }, // 字典序最大（实测）
      { sha: "a11d779", is_latest_by_created_at: true }, // created_at 最新（实测）
      { sha: "b933e20", is_latest_by_created_at: false },
    ];
    expect(latestSnapshotSha(items)).toBe("a11d779");
    expect(latestSnapshotSha(items.map((i) => ({ ...i, is_latest_by_created_at: false })))).toBeNull();
  });

  it("快照徽标文案必须写明「按 created_at」（防文件名排序陷阱）", () => {
    expect(LATEST_SNAPSHOT_BADGE).toBe("最新（按 created_at）");
  });

  it("报告清单分组由 structured 驱动：主报告 / 降级组保序（不重排响应）", () => {
    const items = [
      { name: "7c966e9", structured: true },
      { name: "rls-verify-7d48dcb", structured: false },
      { name: "a207284", structured: true },
      { name: "baseline-compiler-7d48dcb", structured: false },
    ];
    const { main, degraded } = partitionReports(items);
    expect(main.map((i) => i.name)).toEqual(["7c966e9", "a207284"]);
    expect(degraded.map((i) => i.name)).toEqual(["rls-verify-7d48dcb", "baseline-compiler-7d48dcb"]);
    expect(partitionReports([])).toEqual({ main: [], degraded: [] });
  });
});

describe("honesty.ts 截断声明（§3.4 ⑤；G3 降级不确定语气）", () => {
  it("extractLimit 取 SQL 最后一个 LIMIT（编译器所有形态的最后一个是语义上限）", () => {
    expect(extractLimit("SELECT a FROM t ORDER BY a LIMIT 100")).toBe(100);
    expect(extractLimit("WITH c AS (SELECT 1) SELECT x FROM c LIMIT 10000")).toBe(10000);
    expect(extractLimit("SELECT 1 LIMIT 10")).toBe(10);
    expect(extractLimit(null)).toBeNull();
    expect(extractLimit("SELECT 1")).toBeNull();
  });

  it("内层 CTE 与外层同时有 LIMIT 时取外层（最后一个）", () => {
    expect(extractLimit("SELECT * FROM (SELECT * FROM t LIMIT 5) sub LIMIT 100")).toBe(100);
  });

  it("RENDER_CAP=500 是前端自身常量（HTTP 响应不截断，不得声称后端上限）", () => {
    expect(RENDER_CAP).toBe(500);
  });

  it("row_count 未超渲染上限：无渲染提示，renderedRows = row_count", () => {
    const st = truncationState({ rowCount: 100, sql: "SELECT 1 LIMIT 100" });
    expect(st.renderNote).toBeNull();
    expect(st.renderedRows).toBe(100);
  });

  it("row_count 超过渲染上限：文案区分「已加载全部」与「渲染上限」", () => {
    const st = truncationState({ rowCount: 780, sql: "SELECT 1 LIMIT 10000" });
    expect(st.renderedRows).toBe(500);
    expect(st.renderNote).toBe("已加载全部 780 行，表格仅渲染前 500 行（前端渲染上限）");
  });

  it("row_count == limit：给不确定语气提示（可能被 Plan 的 limit 截断）", () => {
    const st = truncationState({ rowCount: 100, sql: "SELECT 1 LIMIT 100" });
    expect(st.limit).toBe(100);
    expect(st.limitNote).toBe("结果可能被 Plan 的 limit 截断（当前 = 100）");
  });

  it("row_count < limit：不提示截断（恰好相等才是信号）", () => {
    expect(truncationState({ rowCount: 99, sql: "SELECT 1 LIMIT 100" }).limitNote).toBeNull();
  });

  it("SQL 缺失（clarify/blocked 等非 answer 轮）与空结果：两个提示都不出现", () => {
    const st = truncationState({ rowCount: 0, sql: null });
    expect(st.limit).toBeNull();
    expect(st.renderNote).toBeNull();
    expect(st.limitNote).toBeNull();
  });
});
