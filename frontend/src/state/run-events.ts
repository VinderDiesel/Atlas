/**
 * T06 运行事件归约（ADR-0031 D07③：图与时间线由同一事件流归约）。
 *
 * 与后端 `serving/control/events.py::reduce_events` 同语义的增量版：同一
 * `RunEvent` 序列折叠成 `RunViewState`，RunGraph（节点/边）与 RunTimeline
 * （按 seq 的事件序列）出自同一状态——不做 UI 时间推断、不合并第二数据源。
 *
 * 纪律：
 * - **按 seq 去重**：`seq ≤ lastSeq` 的帧一律忽略——重复帧不产生第二次状态
 *   变化，迟到旧帧不回溯新状态；
 * - **事件驱动**：run 状态只由 RUN_ACCEPTED/RUN_STARTED/RUN_FINISHED/
 *   RUN_INTERRUPTED 改变（未收事件时 `status` 为 null，不猜「运行中」）；
 * - **未执行节点**：定义模板（definedNodeIds）中未收 NODE_* 事件的节点保持
 *   `not_reached`（不编造开始/结束时间）；`skipped` 只来自显式 NODE_SKIPPED；
 * - **契约外事件不猜**：未知 event_type 完全保持原状态（镜像 analysis-stream
 *   reducer 的累积态纪律；lastSeq 也不前进，后续合法帧仍按 seq 继续）。
 */
import type { ResultKind, RunEvent, RunNodeStatus, RunStatus } from "../api/types";

/** 单节点视图（镜像 events.py:NodeView；seq 是列级证据，不换算时间）。 */
export interface RunNodeView {
  node_id: string;
  status: RunNodeStatus;
  node_run_id: string | null;
  started_seq: number | null;
  ended_seq: number | null;
}

/** 运行渲染态（图 + 时间线 + run 级状态同一事实源）。 */
export interface RunViewState {
  /** 首个事件声明（防串流比较用；未收事件为 null）。 */
  runId: string | null;
  /** run 级状态；未收事件为 null（不从 UI 时间推断「运行中」）。 */
  status: RunStatus | null;
  /** 已归约的最大 seq（去重水位）。 */
  lastSeq: number;
  /** node_id → 节点视图（含定义模板中的 not_reached 节点）。 */
  nodes: Record<string, RunNodeView>;
  /** 已走的边（EDGE_TAKEN，按到达序；`[from, to]`）。 */
  edges: Array<[string, string]>;
  /** 按 seq 累积的已归约事件序列（时间线数据面；契约外事件不进入）。 */
  timeline: RunEvent[];
  /** STATE_SNAPSHOT 的安全摘要（result_kind）；未收为 null。 */
  resultKind: ResultKind | null;
  /** 收到 RUN_FINISHED / RUN_INTERRUPTED 后为 true（流终结信号）。 */
  finished: boolean;
}

const RUN_STATUSES: readonly string[] = [
  "queued",
  "running",
  "succeeded",
  "blocked",
  "failed",
  "interrupted",
];

const RESULT_KINDS: readonly string[] = ["answer", "clarify", "handoff", "blocked", "error"];

/** 契约内「只累积时间线」的事件（无图/状态变化）。 */
const TIMELINE_ONLY_EVENTS: ReadonlySet<string> = new Set([
  "TOOL_STARTED",
  "TOOL_FINISHED",
  "FALLBACK",
]);

function asRunStatus(value: unknown): RunStatus | null {
  return typeof value === "string" && RUN_STATUSES.includes(value) ? (value as RunStatus) : null;
}

function asResultKind(value: unknown): ResultKind | null {
  return typeof value === "string" && RESULT_KINDS.includes(value)
    ? (value as ResultKind)
    : null;
}

/**
 * 初态：定义模板节点全为 `not_reached`（不编造开始/结束时间）。
 *
 * @param definedNodeIds 定义图节点清单（固定模板；未到达节点显示「未执行」）。
 */
export function emptyRunState(definedNodeIds: string[] = []): RunViewState {
  const nodes: Record<string, RunNodeView> = {};
  for (const node_id of definedNodeIds) {
    nodes[node_id] = {
      node_id,
      status: "not_reached",
      node_run_id: null,
      started_seq: null,
      ended_seq: null,
    };
  }
  return {
    runId: null,
    status: null,
    lastSeq: 0,
    nodes,
    edges: [],
    timeline: [],
    resultKind: null,
    finished: false,
  };
}

/** 单事件折叠（不可变）；同一 state 可安全重复调用（去重幂等）。 */
export function reduceRunEvent(state: RunViewState, event: RunEvent): RunViewState {
  if (event.seq <= state.lastSeq) {
    return state; // 去重：重复/迟到帧不产生第二次状态变化
  }
  const base = {
    runId: state.runId ?? event.run_id,
    lastSeq: event.seq,
    timeline: [...state.timeline, event],
  };
  switch (event.event_type) {
    case "RUN_ACCEPTED":
      return { ...state, ...base, status: "queued" };
    case "RUN_STARTED":
      return { ...state, ...base, status: "running" };
    case "NODE_STARTED": {
      const node_id = event.node_id;
      if (node_id === null) {
        return { ...state, ...base };
      }
      const view: RunNodeView = {
        node_id,
        status: "running",
        node_run_id: event.node_run_id,
        started_seq: event.seq,
        ended_seq: null,
      };
      return { ...state, ...base, nodes: { ...state.nodes, [node_id]: view } };
    }
    case "NODE_FINISHED":
    case "NODE_FAILED":
    case "NODE_SKIPPED": {
      const node_id = event.node_id;
      if (node_id === null) {
        return { ...state, ...base };
      }
      const status: RunNodeStatus =
        event.event_type === "NODE_FINISHED"
          ? "finished"
          : event.event_type === "NODE_FAILED"
            ? "failed"
            : "skipped";
      const current = state.nodes[node_id] ?? null;
      const view: RunNodeView = {
        node_id,
        status,
        node_run_id: event.node_run_id ?? current?.node_run_id ?? null,
        started_seq: current?.started_seq ?? null,
        ended_seq: event.seq,
      };
      return { ...state, ...base, nodes: { ...state.nodes, [node_id]: view } };
    }
    case "EDGE_TAKEN": {
      const target = event.payload["to"];
      if (event.node_id === null || typeof target !== "string") {
        return { ...state, ...base };
      }
      return { ...state, ...base, edges: [...state.edges, [event.node_id, target]] };
    }
    case "STATE_SNAPSHOT": {
      const kind = asResultKind(event.payload["result_kind"]);
      return { ...state, ...base, resultKind: kind ?? state.resultKind };
    }
    case "RUN_FINISHED": {
      const status = asRunStatus(event.payload["status"]);
      return { ...state, ...base, status: status ?? state.status, finished: true };
    }
    case "RUN_INTERRUPTED": {
      const status = asRunStatus(event.payload["status"]) ?? "interrupted";
      return { ...state, ...base, status, finished: true };
    }
    default:
      // 契约内时间线类事件只累积序列；契约外类型完全保持原状（不猜语义）
      return TIMELINE_ONLY_EVENTS.has(event.event_type) ? { ...state, ...base } : state;
  }
}
