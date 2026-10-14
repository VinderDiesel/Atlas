/**
 * 查询历史（localStorage 持久化；非敏感 UI 数据，与 theme/ 同类豁免 no-persist 扫描）。
 *
 * 背景（ADR-0028 裁定 B）：服务端 `GET /api/v1/sessions` 依赖控制库 `runs` 表，
 * 而 `/ask` 端点不写 `runs`（仅 Agent 内存 checkpointer）。本模块在客户端侧补位——记录每次成功回合的问句与响应摘要，
 * 跨导航/跨刷新可回溯（关闭标签页不丢失，localStorage 手动清除才消失）。
 *
 * 三条纪律：
 * 1. **只存摘要**：`rows` 截断至 20 行、`chart`/`analysis` 丢弃（体积大且渲染依赖
 *    完整上下文，历史区只展示问句 + 指标 + 行数 + SQL 摘要）；
 * 2. **上限 50 条**：先进先出，超出自动淘汰最早条目；
 * 3. **纯函数 + 副作用隔离**：`loadHistory`/`saveHistory` 直接读写 localStorage，
 *    `appendHistory` 返回新数组（不原地修改），调用方负责 `setState`。
 *
 * 本文件**不 import React**——与 `state/session.ts` 同纪律，可被 vitest 直接断言。
 */

import type { Explanation, ModelDomain, TurnPayload } from "../api/types";

/**
 * 从历史条目还原 TurnPayload（用于点击历史条目时直接复现查询结果，不重新执行）。
 *
 * 历史条目是裁剪投影——`chart`/`analysis`/`narrative` 等在存储时已丢弃，
 * 还原时填 null/默认值；`engine`/`path`/`usage` 等元信息同样缺失，填占位值。
 * `explanation`（指标口径计划）已保留，还原时透传至 ExplanationBlock 展示。
 * 还原的 payload 只用于前端展示（数据表 + SQL + 指标计划），不再触发任何请求。
 */
export function toTurnPayload(entry: QueryHistoryEntry): TurnPayload {
  return {
    kind: entry.kind,
    session_id: entry.session_id,
    question: entry.question,
    turns_in_session: entry.turns_in_session,
    metric: entry.metric,
    sql: entry.sql,
    columns: entry.columns,
    rows: entry.rows,
    row_count: entry.row_count,
    latency_ms: entry.latency_ms,
    engine: "(history)",
    path: null,
    usage: {},
    validation_issues: [],
    explanation: entry.explanation,
    chart: null,
    clarification: null,
    block_reason: null,
    error: null,
    handoff_reason: null,
    snapshot_sha: entry.snapshot_sha,
    snapshot_bound_to_head: null,
    analysis: null,
  };
}

/** localStorage 键名（带项目前缀，避免同域其他应用冲突）。 */
const STORAGE_KEY = "atlas_query_history";

/** 历史条目上限（先进先出）。 */
const MAX_ENTRIES = 50;

/** 历史区展示的 rows 上限（存储侧截断，渲染侧不再二次裁）。 */
const MAX_ROWS_IN_HISTORY = 20;

/** 历史条目（TurnPayload 的裁剪投影 + 元信息）。 */
export interface QueryHistoryEntry {
  /** 条目 id（= session_id + "_" + turns_in_session，同会话内唯一）。 */
  id: string;
  /** 问句原文（用户输入，不裁剪）。 */
  question: string;
  /** 请求时的域（finance / retail）。 */
  model: ModelDomain;
  /** 响应 kind（answer / clarify / blocked / error / handoff）。 */
  kind: TurnPayload["kind"];
  /** 指标名（answer 轮有值，其余 null）。 */
  metric: string | null;
  /** 出口 SQL（answer 轮有值，其余 null；历史区截断至 200 字符）。 */
  sql: string | null;
  /** 行数（answer 轮有值，其余 0）。 */
  row_count: number;
  /** 执行耗时 ms（answer 轮有值，其余 0）。 */
  latency_ms: number;
  /** 会话 id（用于关联会话日志）。 */
  session_id: string;
  /** 第几轮（同会话内递增）。 */
  turns_in_session: number;
  /** 记录时刻（epoch ms；展示层格式化，本模块不碰时区）。 */
  timestamp: number;
  /** 裁剪后的 rows（≤ MAX_ROWS_IN_HISTORY；answer 轮才有数据）。 */
  rows: unknown[][];
  /** 列名（answer 轮才有）。 */
  columns: string[];
  /** 快照 sha（绑定证据，历史区展示用）。 */
  snapshot_sha: string | null;
  /**
   * 指标口径计划（Explanation 投影）：answer 轮存储，还原时透传至 ExplanationBlock。
   * 体积可控（filters/tables 为短字符串数组），整体存入 localStorage。
   */
  explanation: Explanation | null;
  /** 执行通道：ask=同步轻量（默认）；runs=异步审计运行。 */
  channel: "ask" | "runs";
  /** 审计运行的 run_id（仅 channel="runs" 时有值，ask 通道为 null）。 */
  run_id: string | null;
}

/** 从 TurnPayload 构造历史条目（裁剪大字段，控制体积）。 */
export function entryFromPayload(
  question: string,
  model: ModelDomain,
  payload: TurnPayload,
  channel: "ask" | "runs" = "ask",
  runId: string | null = null,
): QueryHistoryEntry {
  const trimmedRows = payload.rows.slice(0, MAX_ROWS_IN_HISTORY);
  const trimmedSql = payload.sql !== null && payload.sql.length > 200
    ? payload.sql.slice(0, 200) + "…"
    : payload.sql;
  return {
    id: `${payload.session_id}_${payload.turns_in_session}`,
    question,
    model,
    kind: payload.kind,
    metric: payload.metric,
    sql: trimmedSql,
    row_count: payload.row_count,
    latency_ms: payload.latency_ms,
    session_id: payload.session_id,
    turns_in_session: payload.turns_in_session,
    timestamp: Date.now(),
    rows: trimmedRows,
    columns: payload.columns,
    snapshot_sha: payload.snapshot_sha,
    explanation: payload.explanation,
    channel,
    run_id: runId,
  };
}

/** 从 localStorage 读取历史（损坏 / 缺失 → 空数组，不抛异常）。 */
export function loadHistory(): QueryHistoryEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) {
      return [];
    }
    const parsed = JSON.parse(raw) as QueryHistoryEntry[];
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed;
  } catch {
    // 存储损坏或不可用：静默回落空数组（不阻塞 UI）
    return [];
  }
}

/** 写入历史（整体替换；调用方须先 append 再截断，本函数不做业务逻辑）。 */
export function saveHistory(entries: QueryHistoryEntry[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // 存储不可用（隐私模式 / 配额满）：不阻塞查询，静默丢弃
  }
}

/**
 * 追加一条历史并返回新数组（FIFO 淘汰，上限 MAX_ENTRIES）。
 *
 * 纯函数：不修改入参，返回新数组。调用方负责 `setState(prev => appendHistory(prev, entry))`。
 * 同 id 条目覆盖（多轮续接时同一会话的后续轮次更新同一条目，不重复追加）。
 */
export function appendHistory(
  entries: readonly QueryHistoryEntry[],
  entry: QueryHistoryEntry,
): QueryHistoryEntry[] {
  // 同 id 覆盖（多轮续接：同会话后续轮次替换旧条目）
  const filtered = entries.filter((e) => e.id !== entry.id);
  const next = [...filtered, entry];
  // FIFO 淘汰
  return next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next;
}

/** 清空历史（用户主动操作；返回空数组，调用方负责 saveHistory([])）。 */
export function clearHistory(): QueryHistoryEntry[] {
  return [];
}

/**
 * 删除单条历史（按 id 移除；纯函数，返回新数组）。
 * 调用方负责 `setState(prev => removeHistoryEntry(prev, id))` + `saveHistory(next)`。
 */
export function removeHistoryEntry(
  entries: readonly QueryHistoryEntry[],
  id: string,
): QueryHistoryEntry[] {
  return entries.filter((e) => e.id !== id);
}

/** 格式化时刻（历史区展示用）。 */
export function formatHistoryTime(epochMs: number): string {
  return new Date(epochMs).toLocaleString();
}
