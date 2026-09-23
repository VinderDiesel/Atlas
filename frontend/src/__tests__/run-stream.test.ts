/**
 * T06c 运行事件流客户端断言（ADR-0031 D07/D13：GET /runs/{id}/events）。
 *
 * 关键契约：
 * - **帧格式**：服务端 `id: {seq}\nevent: {type}\ndata: {json}\n\n`；data 是
 *   唯一事实源（12 键 RunEvent，id/event 行仅提示）。心跳 `: ping`、半帧、
 *   坏 JSON、缺 seq 的 data 一律跳过（不崩溃）。
 * - **token 只在 Authorization 头、不进 URL**（N9；不用 EventSource——它不支持
 *   自定义头）。
 * - **断线续读**：未收终态事件（RUN_FINISHED/RUN_INTERRUPTED）时用最后 seq 重连
 *   （after_seq query），**只读不重跑**（永不重发 POST）；收终态后不再重连；
 *   续读次数有上限（不无限重连）。
 * - **建立期错误不重试**：非 2xx（401/403/404/410）抛 ApiError 原文透传。
 * - **用户取消**（AbortSignal）不续读，错误如实抛出。
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { parseRunEventFrame, streamRunEvents, type RunStreamOptions } from "../api/run-stream";
import type { RunEvent, RunEventType } from "../api/types";

const RUN_ID = "run-0001";
const RELEASE_ID = "rel-0001";

function baseEvent(seq: number, event_type: RunEventType, extra: Partial<RunEvent> = {}): RunEvent {
  return {
    schema_version: 1,
    run_id: RUN_ID,
    seq,
    event_id: `evt-${seq}`,
    occurred_at: `2026-09-22T10:00:${String(seq).padStart(2, "0")}+08:00`,
    node_id: null,
    node_run_id: null,
    parent_node_run_id: null,
    attempt: null,
    event_type,
    release_id: RELEASE_ID,
    payload: {},
    ...extra,
  };
}

const acceptedEvent = baseEvent(1, "RUN_ACCEPTED");
const startedEvent = baseEvent(2, "RUN_STARTED");
const finishedEvent = baseEvent(3, "RUN_FINISHED", { payload: { status: "succeeded" } });

/** 服务端帧（与 `serving/control/router.py::_sse_event_frame` 逐字同构）。 */
function frame(event: RunEvent): string {
  return `id: ${event.seq}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** 用给定 chunk 数组造一个 SSE Response（每元素一个 chunk，可拆帧）。 */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) {
        controller.enqueue(encoder.encode(c));
      }
      controller.close();
    },
  });
  return { ok: true, status: 200, headers: new Headers(), body: stream } as unknown as Response;
}

interface FetchCall {
  url: string;
  init?: RequestInit;
}

/**
 * 按序消费响应工厂队列（超出后重复最后一个）；每次请求生成新 Response——
 * ReadableStream body 只能 getReader() 一次，复用同一对象会 ERR_INVALID_STATE。
 */
async function collect(
  responses: Array<() => Response>,
  token: string,
  options: RunStreamOptions = {},
): Promise<{ events: RunEvent[]; calls: FetchCall[] }> {
  const calls: FetchCall[] = [];
  const orig = globalThis.fetch;
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(u), init });
    const make = responses[Math.min(calls.length - 1, responses.length - 1)];
    return Promise.resolve(make());
  }) as unknown as typeof fetch;
  try {
    const events: RunEvent[] = [];
    for await (const evt of streamRunEvents(RUN_ID, token, options)) {
      events.push(evt);
    }
    return { events, calls };
  } finally {
    globalThis.fetch = orig;
  }
}

describe("parseRunEventFrame（data 是唯一事实源）", () => {
  it("解析 id/event/data 三行帧为完整 RunEvent", () => {
    expect(parseRunEventFrame(frame(startedEvent))).toEqual(startedEvent);
  });

  it("无 event 行时仍认 data（event 行仅提示，data 自带 event_type）", () => {
    const noEventLine = `id: 2\ndata: ${JSON.stringify(startedEvent)}\n\n`;
    expect(parseRunEventFrame(noEventLine)).toEqual(startedEvent);
  });

  it("心跳/空帧/坏 JSON/缺 seq 或 event_type → null（跳过不崩溃）", () => {
    expect(parseRunEventFrame(": ping")).toBeNull();
    expect(parseRunEventFrame("")).toBeNull();
    expect(parseRunEventFrame('event: RUN_STARTED\ndata: {oops')).toBeNull();
    expect(parseRunEventFrame('event: RUN_STARTED\ndata: {"event_type":"RUN_STARTED"}')).toBeNull();
    expect(parseRunEventFrame('event: RUN_STARTED\ndata: {"seq":2}')).toBeNull();
  });
});

describe("streamRunEvents（GET + ReadableStream；token 只在头不进 URL）", () => {
  it("GET 请求 + Authorization/Accept 头；token 不进 URL", async () => {
    const { events, calls } = await collect(
      [() => sseResponse([frame(startedEvent), frame(finishedEvent)])],
      "TOK123",
    );
    expect(events.map((e) => e.seq)).toEqual([2, 3]);
    expect(calls[0].init?.method).toBe("GET");
    const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer TOK123");
    expect(headers.Accept).toBe("text/event-stream");
    expect(calls[0].url).not.toContain("TOK123");
  });

  it("默认不带 after_seq；显式 afterSeq 时携带续读游标", async () => {
    const plain = await collect([() => sseResponse([frame(finishedEvent)])], "");
    expect(plain.calls[0].url).not.toContain("after_seq");
    const headers = (plain.calls[0].init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();

    const resumed = await collect([() => sseResponse([frame(finishedEvent)])], "T", { afterSeq: 2 });
    expect(resumed.calls[0].url).toContain("after_seq=2");
  });

  it("跨 chunk 拆帧重组（半帧不丢、心跳不产出事件）", async () => {
    const { events } = await collect(
      [
        () =>
          sseResponse([
            ": ping\n\n" + frame(acceptedEvent).slice(0, 24), // 心跳 + 半帧
            frame(acceptedEvent).slice(24) + frame(finishedEvent), // 补完 + 下一帧
          ]),
      ],
      "",
    );
    expect(events.map((e) => e.seq)).toEqual([1, 3]);
  });

  it("非 2xx（410 event_gap）抛 ApiError 原文透传，且不重试", async () => {
    const detail = "事件已过保留期，无法从游标 1 补齐";
    const response = {
      ok: false,
      status: 410,
      headers: new Headers(),
      json: () => Promise.resolve({ detail }),
    } as unknown as Response;
    await expect(collect([() => response], "T")).rejects.toThrowError(ApiError);
    const calls: FetchCall[] = [];
    const orig = globalThis.fetch;
    globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(u), init });
      return Promise.resolve(response);
    }) as unknown as typeof fetch;
    try {
      await expect(
        (async () => {
          for await (const _ of streamRunEvents(RUN_ID, "T")) {
            void _;
          }
        })(),
      ).rejects.toThrowError(new ApiError(410, detail, null));
    } finally {
      globalThis.fetch = orig;
    }
    expect(calls).toHaveLength(1);
  });

  it("断线自动续读：未收终态用最后 seq 重连（只读、不重跑），收终态后停止", async () => {
    const { events, calls } = await collect(
      [
        () => sseResponse([frame(acceptedEvent), frame(startedEvent)]), // 断线（无终态，流结束）
        () => sseResponse([frame(finishedEvent)]), // 续读命中
      ],
      "T",
    );
    expect(events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(calls).toHaveLength(2);
    expect(calls[1].url).toContain("after_seq=2");
    expect(calls.every((c) => c.init?.method === "GET")).toBe(true);
  });

  it("收到终态事件后即使流结束也不再重连", async () => {
    const { events, calls } = await collect(
      [() => sseResponse([frame(acceptedEvent), frame(finishedEvent)])],
      "T",
    );
    expect(events.map((e) => e.seq)).toEqual([1, 3]);
    expect(calls).toHaveLength(1);
  });

  it("续读有上限：持续无终态断线不无限重连", async () => {
    const { calls } = await collect([() => sseResponse([])], "T", { maxResumes: 2 });
    expect(calls).toHaveLength(3); // 1 次初始 + 2 次续读
  });

  it("用户取消（AbortError）不续读，错误如实抛出", async () => {
    const controller = new AbortController();
    controller.abort();
    const abortError = new DOMException("The operation was aborted.", "AbortError");
    const calls: FetchCall[] = [];
    const orig = globalThis.fetch;
    globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(u), init });
      return Promise.reject(abortError);
    }) as unknown as typeof fetch;
    try {
      await expect(
        (async () => {
          for await (const _ of streamRunEvents(RUN_ID, "T", { signal: controller.signal })) {
            void _;
          }
        })(),
      ).rejects.toThrow();
    } finally {
      globalThis.fetch = orig;
    }
    expect(calls).toHaveLength(1);
  });
});
