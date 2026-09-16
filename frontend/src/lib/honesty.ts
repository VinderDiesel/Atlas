/**
 * 诚实性标志位的纯函数判定（0018 判据 10 的 vitest 断言对象）。
 *
 * 两类消费方：
 * - 治理面板（P2/P3）五类标志位：值域 skipped 空壳、报告 structured 降级、
 *   报告 dry 标注、同义词 empty_placeholder 横幅、快照 is_latest 标志
 *   （设计页 §3.3 的渲染义务表）；
 * - 工作台（P1）截断声明（§3.4 ⑤）：HTTP 响应不截断，行数上限来自 Plan 的
 *   `limit`；「渲染了多少行」是前端自身常量（RENDER_CAP）。
 *
 * 纪律：不 import React / fetch；文案是渲染义务的一部分——改文案先读设计页
 * §3.3/§3.4，并同步 __tests__/honesty.test.ts 的逐字断言。
 */

// ---------------------------------------------------------------------------
// 值域（§3.3 第 1 行：skipped 空壳与 skip_reason 原文）
// ---------------------------------------------------------------------------

export interface ValueDomainFacts {
  /** /api/v1/governance/values 的 status：实测枚举只有 `registered` | `skipped`。 */
  status: string | null;
  skip_reason: string | null;
  values_count: number;
}

export type ValueDomainGroup = "covered" | "skipped";

/** 分组只由 status 驱动，与 values_count 无关——空数组也不得被读成「0 个值」。 */
export function valueDomainGroup(item: ValueDomainFacts): ValueDomainGroup {
  return item.status === "skipped" ? "skipped" : "covered";
}

/** 跳过组提示（引用 skip_reason 原文）；非跳过条目返回 `null`。 */
export function valueSkipNote(item: ValueDomainFacts): string | null {
  if (item.status !== "skipped") {
    return null;
  }
  const reason = item.skip_reason?.trim();
  return `值域未采集：${reason !== undefined && reason !== "" ? reason : "<skip_reason 缺失>"}`;
}

export interface ValueDomainPartition<T extends ValueDomainFacts> {
  covered: T[];
  skipped: T[];
}

/**
 * 值域清单分两组（保持响应原顺序）——values 子页的「已覆盖 / 已跳过」两分组
 * 数据源。§3.3 第 1 行的渲染义务：跳过组**默认展开**且显示 skip_reason 原文；
 * 禁止平铺、禁止折叠进「高级」。本函数只输出分组事实，「展开」由组件的
 * `defaultActiveKey` 承担（两键都默认激活——见 ValuesSection）。
 */
export function partitionValueDomains<T extends ValueDomainFacts>(
  items: readonly T[],
): ValueDomainPartition<T> {
  const covered: T[] = [];
  const skipped: T[] = [];
  for (const item of items) {
    (valueDomainGroup(item) === "skipped" ? skipped : covered).push(item);
  }
  return { covered, skipped };
}

// ---------------------------------------------------------------------------
// 报告（§3.3 第 2/3 行：structured 降级与 dry 标注）
// ---------------------------------------------------------------------------

export interface ReportFacts {
  /** /api/v1/governance/reports 的 structured：主报告才为 true。 */
  structured: boolean;
  /** 文件名模式标签（sha 段已替换为 `<sha>`）。 */
  pattern: string;
  /** 主报告 body 的 dry（非主报告无此键 → null）。 */
  dry?: boolean | null;
}

/** 非主报告走降级展示（不得伪造统一表头、不把 raw 摊平成表格列）。 */
export function reportDegradeNote(report: ReportFacts): string | null {
  return report.structured
    ? null
    : `非统一结构报告（模式：${report.pattern}），以下为原始 JSON`;
}

/** dry 标注：dry=true 时出现；禁止把 `ex: "n/a"` 读成 0 或 0%。 */
export function dryNote(report: ReportFacts): string | null {
  return report.dry === true ? "dry 运行：未连 DB，EX 不参与统计" : null;
}

export interface ReportStructuredFacts {
  /** 后端判定：主报告才为 true。 */
  structured: boolean;
}

export interface ReportPartition<T> {
  /** 主报告（文件名 = 纯 sha，后端解析 body 的 structured=true 组）。 */
  main: T[];
  /** 非主报告（原始 JSON + 模式标签降级组）。 */
  degraded: T[];
}

/**
 * 报告清单分两组（保持响应原顺序）——reports 子页的两个分组数据源。
 * 分组只由 `structured` 驱动（后端判定），前端不得按文件名模式自行分类
 * （模式标签是展示列，不是分类依据；分组错位会让降级声明贴错行）。
 */
export function partitionReports<T extends ReportStructuredFacts>(
  items: readonly T[],
): ReportPartition<T> {
  const main: T[] = [];
  const degraded: T[] = [];
  for (const item of items) {
    (item.structured ? main : degraded).push(item);
  }
  return { main, degraded };
}

// ---------------------------------------------------------------------------
// 同义词（§3.3 第 4 行：empty_placeholder 横幅）
// ---------------------------------------------------------------------------

export interface SynonymsFacts {
  /** /api/v1/governance/synonyms 的空占位标志。 */
  empty_placeholder: boolean;
  /** 权威源说明（后端常量，按 locale 给出）。 */
  authority_note: string | null;
}

/** 空占位横幅直接引用 authority_note 原文（不得说「中文暂无同义词」）。 */
export function synonymsBanner(facts: SynonymsFacts): string | null {
  if (!facts.empty_placeholder) {
    return null;
  }
  const note = facts.authority_note?.trim();
  return note !== undefined && note !== "" ? note : "空占位（权威源说明缺失）";
}

// ---------------------------------------------------------------------------
// 快照清单（§3.3 第 6 行：is_latest_by_created_at）
// ---------------------------------------------------------------------------

export interface SnapshotListFacts {
  sha: string;
  /** 后端按 created_at 降序排序后标记的首条；前端不得自行排序推导。 */
  is_latest_by_created_at: boolean;
}

/**
 * 快照清单的「最新」徽标文案（§3.3 第 6 行）——必须写明「按 created_at」：
 * 字典序最大 ≠ created_at 最新（0019 背景的实测陷阱），含糊的「最新」会被
 * 读成文件名排序的结论。
 */
export const LATEST_SNAPSHOT_BADGE = "最新（按 created_at）";

/** 「最新」由后端标志驱动；无标志返回 `null`（不按文件名排序后取首行）。 */
export function latestSnapshotSha(items: readonly SnapshotListFacts[]): string | null {
  const latest = items.find((item) => item.is_latest_by_created_at);
  return latest === undefined ? null : latest.sha;
}

// ---------------------------------------------------------------------------
// 截断声明（§3.4 ⑤；G3 未进契约 → 降级为不确定语气，该降级记录在 KL）
// ---------------------------------------------------------------------------

/** 前端表格渲染上限——**前端自身常量**，不得声称是后端上限（HTTP 响应不截断）。 */
export const RENDER_CAP = 500;

const LIMIT_RE = /\bLIMIT\s+(\d+)/gi;

/**
 * 取 SQL 的**最后一个** LIMIT 值；无则 `null`。
 *
 * 编译器所有形态都是单参 LIMIT，且最后一个即语义行数上限：基础 SELECT 由
 * sqlglot `Limit` 发射（agent/compiler.py:793），lag / lag_by_dims / cumulative
 * 三处为外层 f-string 拼接（同文件 :1245/:1277/:1300，内层 base 的 LIMIT 已被
 * 移除）。两参 `LIMIT m, n` 不在发射形态内——不做猜测。
 */
export function extractLimit(sql: string | null): number | null {
  if (sql === null) {
    return null;
  }
  let last: number | null = null;
  for (const match of sql.matchAll(LIMIT_RE)) {
    last = Number.parseInt(match[1], 10);
  }
  return last;
}

export interface TruncationFacts {
  /** 后端已加载的行数（= payload.row_count；HTTP 不截断，即全量已到达浏览器）。 */
  rowCount: number;
  /** 出口 SQL（非 answer 轮为 null）。 */
  sql: string | null;
}

export interface TruncationState {
  renderCap: number;
  /** 表格实际渲染的行数（= min(rowCount, RENDER_CAP)）。 */
  renderedRows: number;
  /** rowCount 超过渲染上限时的提示；否则 `null`。 */
  renderNote: string | null;
  /** SQL 的最后一个 LIMIT（= Plan 的 limit）；未携带 SQL 时为 `null`。 */
  limit: number | null;
  /** `rowCount === limit` 时的不确定语气提示；否则 `null`（G3 降级）。 */
  limitNote: string | null;
}

/**
 * 截断状态：产生「渲染上限」与「可能被 Plan 的 limit 截断」两个提示。
 *
 * 两个提示的触发条件互不相同（前者 rowCount 超前端常量，后者恰好等于 SQL 的
 * 最后一个 LIMIT）；同时命中时都展示。文案区分「已加载」与「渲染」——
 * 完整数据的出口只能是 `atlas query --format json` 或收窄 limit 重发（§3.4）。
 */
export function truncationState(facts: TruncationFacts): TruncationState {
  const limit = extractLimit(facts.sql);
  const renderNote =
    facts.rowCount > RENDER_CAP
      ? `已加载全部 ${facts.rowCount} 行，表格仅渲染前 ${RENDER_CAP} 行（前端渲染上限）`
      : null;
  const limitNote =
    limit !== null && facts.rowCount === limit
      ? `结果可能被 Plan 的 limit 截断（当前 = ${limit}）`
      : null;
  return {
    renderCap: RENDER_CAP,
    renderedRows: Math.min(facts.rowCount, RENDER_CAP),
    renderNote,
    limit,
    limitNote,
  };
}
