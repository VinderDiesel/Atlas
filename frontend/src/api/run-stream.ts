/**
 * T06 运行事件流客户端（ADR-0031 D07/D13：`GET /runs/{id}/events`）。
 *
 * 三块职责：
 * 1. `parseRunEventFrame`（纯）：解析服务端帧 `id: {seq}\nevent: {type}\ndata:
 *    {json}\n\n`——**data 是唯一事实源**（12 键 RunEvent 自带 seq/event_type），
 *    id/event 行仅提示；心跳 `: ping`、半帧、坏 JSON、缺 seq/event_type 的
 *    data 一律返回 null（跳过，不崩溃）。
 * 2. `connectOnce`（IO）：单次连接。**token 只进 Authorization 头、不进 URL**
 *    （N9；不用 EventSource——它不支持自定义头）。非 2xx 抛 `ApiError`
 *    （错误原文透传，与 client.ts 同一实现）。
 * 3. `streamRunEvents`（IO）：断线自动续读循环。未收终态事件
 *    （RUN_FINISHED/RUN_INTERRUPTED）时用最后 seq 重连（after_seq query）——
 *    **只读不重跑**（永不重发 POST /runs）；建立期错误（401/403/404/410）不
 *    重试；续读次数有上限（默认 3，不无限重连）；`AbortSignal` 取消后不续读，
 *    错误如实抛出。
 */
import { ApiError, parseRetryAfter, readDetail } from "./client";
import { API } from "./endpoints";
import type { RunEvent } from "./types";

/** 终态事件（收到即无需续读——run 已终局，D07）。 */
const TERMINAL_EVENTS: ReadonlySet<string> = new Set(["RUN_FINISHED", "RUN_INTERRUPTED"]);

const DEFAULT_MAX_RESUMES = 3;

export interface RunStreamOptions {
  /** 初始续读游标（after_seq；默认 0 从头）。 */
  afterSeq?: number;
  /** 取消信号；中止后不续读，错误如实抛出。 */
  signal?: AbortSignal;
  /** 断线自动续读上限（默认 3；0 = 不续读）。 */
  maxResumes?: number;
}

/**
 * 解析单帧（`\n\n` 分帧后的一段）为 RunEvent；非事件帧（心跳等）返回 null。
 *
 * 只认 `data:` 行（可能多行拼接）：data 必须是 JSON 对象且含数字 `seq` 与
 * 字符串 `event_type`——其余字段原样透出（契约外键不裁剪）。
 */
export function parseRunEventFrame(frame: string): RunEvent | null {
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
    // id:/event: 仅提示（data 自带全部字段）；": ping" 心跳与空行忽略
  }
  if (dataLines.length === 0) {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) {
    return null;
  }
  const candidate = data as Record<string, unknown>;
  if (typeof candidate["seq"] !== "number" || typeof candidate["event_type"] !== "string") {
    return null;
  }
  return data as RunEvent;
}

/** 续读 URL：after_seq 只在 >0 时附带（0 = 从头，省略以保持 URL 干净）。 */
function eventsPath(runId: string, afterSeq: number): string {
  const path = API.runEvents.replace("{run_id}", encodeURIComponent(runId));
  return afterSeq > 0 ? `${path}?after_seq=${afterSeq}` : path;
}

/** 单次连接：GET + ReadableStream 读取器，按序产出解析后的事件。 */
async function* connectOnce(
  runId: string,
  token: string,
  afterSeq: number,
  signal?: AbortSignal,
): AsyncGenerator<RunEvent, void, void> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (token !== "") {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(eventsPath(runId, afterSeq), { method: "GET", headers, signal });
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
      const evt = parseRunEventFrame(frame);
      if (evt !== null) {
        yield evt;
      }
      idx = buffer.indexOf("\n\n");
    }
  }
  buffer += decoder.decode();
  const tail = parseRunEventFrame(buffer);
  if (tail !== null) {
    yield tail;
  }
}

/**
 * 以流式方式请求 `/runs/{id}/events`，按序产出运行事件（跨连接续读）。
 *
 * @param runId   运行 ID（URL 转义后进路径；不触发任何执行）。
 * @param token   Bearer token（空串则不注入 Authorization，由后端 401 语义说话）。
 * @param options 续读游标 / AbortSignal / 续读上限（见 RunStreamOptions）。
 * @yields        已解析的运行事件（seq 递增；重复帧由调用侧 reducer 去重）。
 * @throws ApiError 建立期非 2xx（401/403/404/410 原文透传，不续读）。
 */
export async function* streamRunEvents(
  runId: string,
  token: string,
  options: RunStreamOptions = {},
): AsyncGenerator<RunEvent, void, void> {
  const maxResumes = options.maxResumes ?? DEFAULT_MAX_RESUMES;
  let cursor = options.afterSeq ?? 0;
  let resumes = 0;
  for (;;) {
    let terminal = false;
    let failure: unknown = null;
    try {
      for await (const event of connectOnce(runId, token, cursor, options.signal)) {
        cursor = event.seq;
        if (TERMINAL_EVENTS.has(event.event_type)) {
          terminal = true;
        }
        yield event;
      }
    } catch (err) {
      failure = err;
    }
    if (terminal) {
      return; // run 已终局：流终结，不再重连
    }
    if (options.signal?.aborted) {
      if (failure !== null) {
        throw failure;
      }
      return;
    }
    if (failure instanceof ApiError) {
      throw failure; // 建立期错误（含 410 不可补齐）：如实抛出，不重试
    }
    if (resumes >= maxResumes) {
      if (failure !== null) {
        throw failure;
      }
      return;
    }
    resumes += 1; // 断线续读：cursor 保持最后收到的 seq，只读不重跑
  }
}
