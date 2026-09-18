/**
 * ④a `/analyze/stream` 的前端消费面（ADR-0028 决策 ④·执行模型 A compute-then-stream）。
 *
 * 三块职责，各自可独立测试：
 * 1. `reduceAnalysisEvents`（纯）：把 SSE 事件序列折叠成渲染态。终态 `final`
 *    **逐字直存** STATE_SNAPSHOT 携带的完整 TurnPayload——单一事实源（N1）：
 *    流式终态必须与 request/response 的 `/analyze` 同物，前端零重算/零重组。
 * 2. `parseSseFrame`（纯）：手动解析 `event:`/`data:` 双行帧（不用 EventSource——
 *    它不支持 POST + Authorization，见下）。
 * 3. `streamAnalysis`（IO）：fetch + ReadableStream 读取器。**token 只进
 *    Authorization 头、不进 query**（§3.5 约束 3；N9）。非 2xx 抛 `ApiError`
 *    （错误原文透传，与 client.ts 同一实现，不另起炉灶）。
 *
 * 纪律：
 * - 事件名词表镜像 serving/api.py（借鉴 AG-UI 非兼容，N2：不声称 AG-UI 兼容）；
 * - 契约外事件名不猜（reducer 累积态保持、parseSseFrame 原样透出 name）；
 * - 被拒步无 TOOL_CALL_RESULT（N3：被拒 SQL 不出网），故其 phase 停在 "started"。
 */
import { ApiError, parseRetryAfter, readDetail } from "./client";
import { API } from "./endpoints";
import type {
  AnalysisStreamEvent,
  AnalysisStreamEventName,
  AskBody,
  RunErrorData,
  RunFinishedData,
  RunStartedData,
  StepFinishedData,
  StepStartedData,
  ToolCallResultData,
  TurnPayload,
} from "./types";

// ---------------------------------------------------------------------------
// 渲染态 + 纯 reducer
// ---------------------------------------------------------------------------

/** 单步渐进阶段：started → (成功步) result → finished；被拒步停在 started。 */
export type StepPhase = "started" | "result" | "finished";

export interface LiveStep {
  role: string;
  phase: StepPhase;
  /** 成功步收尾时的后端原值（不重算）。 */
  latency_ms: number | null;
  columns: string[];
  rows: unknown[][];
  row_count: number | null;
}

export interface AnalysisStreamState {
  intent: string | null;
  /** 按事件到达序累积的角色步（渐进渲染面）。 */
  steps: LiveStep[];
  /** = STATE_SNAPSHOT 载荷逐字直存（单一事实源；无 STATE_SNAPSHOT 时为 null）。 */
  final: TurnPayload | null;
  /** RUN_FINISHED.status（ok|unavailable|blocked|error）；未收流为 null。 */
  runStatus: string | null;
  /** RUN_ERROR 载荷；成功/无错为 null。 */
  error: RunErrorData | null;
  /** 收到 RUN_FINISHED 或 RUN_ERROR 后为 true（流终结）。 */
  done: boolean;
}

export function initialStreamState(): AnalysisStreamState {
  return { intent: null, steps: [], final: null, runStatus: null, error: null, done: false };
}

/** 折叠单事件（不可变）；契约外事件名原样返回（不猜语义）。 */
function reduceOne(state: AnalysisStreamState, evt: AnalysisStreamEvent): AnalysisStreamState {
  switch (evt.name) {
    case "RUN_STARTED": {
      const d = evt.data as RunStartedData;
      return { ...state, intent: d.intent ?? null };
    }
    case "STEP_STARTED": {
      const d = evt.data as StepStartedData;
      const step: LiveStep = {
        role: d.role,
        phase: "started",
        latency_ms: null,
        columns: [],
        rows: [],
        row_count: null,
      };
      return { ...state, steps: [...state.steps, step] };
    }
    case "TOOL_CALL_RESULT": {
      const d = evt.data as ToolCallResultData;
      return {
        ...state,
        steps: state.steps.map((s) =>
          s.role === d.role && s.phase === "started"
            ? { ...s, phase: "result", columns: d.columns, rows: d.rows, row_count: d.row_count }
            : s,
        ),
      };
    }
    case "STEP_FINISHED": {
      const d = evt.data as StepFinishedData;
      return {
        ...state,
        steps: state.steps.map((s) =>
          s.role === d.role ? { ...s, phase: "finished", latency_ms: d.latency_ms } : s,
        ),
      };
    }
    case "STATE_SNAPSHOT":
      // 单一事实源：直存后端完整 TurnPayload，零重算零重组（N1）。
      return { ...state, final: evt.data as TurnPayload };
    case "RUN_FINISHED": {
      const d = evt.data as RunFinishedData;
      return { ...state, runStatus: d.status, done: true };
    }
    case "RUN_ERROR": {
      const d = evt.data as RunErrorData;
      return { ...state, error: d, done: true };
    }
    default:
      return state;
  }
}

/** 把已按序到达的事件序列折叠成渲染态（纯；无副作用）。 */
export function reduceAnalysisEvents(events: AnalysisStreamEvent[]): AnalysisStreamState {
  return events.reduce(reduceOne, initialStreamState());
}

// ---------------------------------------------------------------------------
// SSE 帧解析（手动；EventSource 不支持 POST + Authorization）
// ---------------------------------------------------------------------------

/**
 * 解析单帧（`\n\n` 分帧后的一段）：抽 `event:` 名与 `data:` JSON。
 * 无 event 行、无 data 行、或 data 非 JSON → null（跳过心跳/半帧/坏帧）。
 */
export function parseSseFrame(frame: string): AnalysisStreamEvent | null {
  let name: string | null = null;
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      name = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
    // 其余（": ping" 心跳、id:/retry:、空行）按 SSE 语义忽略
  }
  if (name === null || dataLines.length === 0) {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  return { name: name as AnalysisStreamEventName, data };
}

// ---------------------------------------------------------------------------
// fetch + ReadableStream 读取器（POST-SSE；token 只在头，不进 query）
// ---------------------------------------------------------------------------

/**
 * 以流式方式请求 `/analyze/stream`，按序产出解析后的事件。
 *
 * @param body   与 `/analyze` 逐字同构的请求体（ADR-0026 决策⑥）。
 * @param token  Bearer token（空串则不注入 Authorization，由后端 401 语义说话）。
 * @param signal 可选 AbortSignal（切换/重发时中断上一条流）。
 * @yields       已解析的 SSE 事件（含契约外名，原样透出）。
 * @throws ApiError 非 2xx（错误原文透传）或响应无 body。
 */
export async function* streamAnalysis(
  body: AskBody,
  token: string,
  signal?: AbortSignal,
): AsyncGenerator<AnalysisStreamEvent, void, void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (token !== "") {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(API.analyzeStream, { method: "POST", headers, body: JSON.stringify(body), signal });
  if (!res.ok) {
    throw new ApiError(res.status, await readDetail(res), parseRetryAfter(res));
  }
  if (res.body === null) {
    throw new ApiError(res.status, "流式响应无 body（ReadableStream 不可用）", null);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let idx = buffer.indexOf("\n\n");
    while (idx !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const evt = parseSseFrame(frame);
      if (evt !== null) {
        yield evt;
      }
      idx = buffer.indexOf("\n\n");
    }
  }
  buffer += decoder.decode();
  const tail = parseSseFrame(buffer);
  if (tail !== null) {
    yield tail;
  }
}
