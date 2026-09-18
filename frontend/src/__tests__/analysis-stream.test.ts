/**
 * ④a 流式消费的纯 reducer + SSE 帧解析断言（ADR-0028 决策 ④·执行模型 A）。
 *
 * 风格与 house 一致：纯函数 + `toEqual`（0018 判据 9 禁快照式断言；此处不涉及 DOM）。
 * 关键契约（单一事实源 N1）：
 * - **终态逐字一致**：全成流的 `final` 必须逐字等于 STATE_SNAPSHOT 携带的
 *   TurnPayload 对象——reducer 不得重组/裁剪/补算任何字段（用带独特标记的对象
 *   驱动，任何重算都会破坏 deep-equal 而变红）。
 * - **渐进来自 TOOL_CALL_RESULT**：liveSteps 按角色序记录 phase/latency/rows，
 *   不重算贡献、不改后端原串。
 * - **失败即断流**：RUN_ERROR 后无 STATE_SNAPSHOT → final null、error 有值。
 * - **plan 无分析意图**：仅 RUN_STARTED/RUN_FINISHED，final null（由调用侧回落
 *   request/response 取完整 TurnPayload）。
 * - **无 EventSource/token-in-query**：reader 用 fetch + ReadableStream（由
 *   no-persist.test.ts 的存储扫描 + 本文件的 header 断言共同守门）。
 */
import { describe, expect, it } from "vitest";

import type {
  AnalysisStreamEvent,
  RunErrorData,
  TurnPayload,
} from "../api/types";
import {
  initialStreamState,
  parseSseFrame,
  reduceAnalysisEvents,
  streamAnalysis,
} from "../api/analysis-stream";

/** 单源不变量的探针对象：独特标记 + 嵌套结构，任何重组都会破坏 deep-equal。 */
const snapshotPayload = {
  __marker: "STATE_SNAPSHOT.verbatim",
  kind: "answer",
  analysis: { status: "ok", totals: { delta: "300.00" } },
} as unknown as TurnPayload;

const ROLES = [
  "baseline_total",
  "current_total",
  "current_by_dimension",
  "baseline_by_dimension",
];

/** 全成流：RUN_STARTED → 4×(STARTED/RESULT/FINISHED) → STATE_SNAPSHOT → RUN_FINISHED。 */
function fullOkStream(): AnalysisStreamEvent[] {
  const events: AnalysisStreamEvent[] = [{ name: "RUN_STARTED", data: { intent: "change_contribution" } }];
  ROLES.forEach((role, i) => {
    events.push({ name: "STEP_STARTED", data: { role } });
    events.push({
      name: "TOOL_CALL_RESULT",
      data: { role, columns: ["v"], rows: [[String(i)]], row_count: 1 },
    });
    events.push({ name: "STEP_FINISHED", data: { role, status: "answer", latency_ms: 10 + i } });
  });
  events.push({ name: "STATE_SNAPSHOT", data: snapshotPayload });
  events.push({ name: "RUN_FINISHED", data: { status: "ok" } });
  return events;
}

describe("reduceAnalysisEvents · 全成流（单一事实源）", () => {
  const state = reduceAnalysisEvents(fullOkStream());

  it("终态 final 逐字等于 STATE_SNAPSHOT 载荷（reducer 零重算）", () => {
    expect(state.final).toBe(snapshotPayload);
    expect(state.final).toEqual(snapshotPayload);
  });

  it("四个成功步按角色序记录（渐进渲染面）", () => {
    expect(state.steps.map((s) => s.role)).toEqual(ROLES);
    expect(state.steps.every((s) => s.phase === "finished")).toBe(true);
    expect(state.steps.map((s) => s.latency_ms)).toEqual([10, 11, 12, 13]);
  });

  it("runStatus/done/error：ok 收流、无错误", () => {
    expect(state.runStatus).toBe("ok");
    expect(state.done).toBe(true);
    expect(state.error).toBeNull();
  });
});

describe("reduceAnalysisEvents · 无分析意图（plan None）", () => {
  const state = reduceAnalysisEvents([
    { name: "RUN_STARTED", data: { intent: null } },
    { name: "RUN_FINISHED", data: { status: "unavailable" } },
  ]);

  it("无 STATE_SNAPSHOT → final null（调用侧回落 request/response）", () => {
    expect(state.final).toBeNull();
    expect(state.steps).toEqual([]);
    expect(state.runStatus).toBe("unavailable");
    expect(state.done).toBe(true);
  });
});

describe("reduceAnalysisEvents · 失败步断流（RUN_ERROR）", () => {
  const state = reduceAnalysisEvents([
    { name: "RUN_STARTED", data: { intent: "change_contribution" } },
    { name: "STEP_STARTED", data: { role: ROLES[0] } },
    { name: "TOOL_CALL_RESULT", data: { role: ROLES[0], columns: [], rows: [], row_count: 0 } },
    { name: "STEP_FINISHED", data: { role: ROLES[0], status: "answer", latency_ms: 1 } },
    { name: "STEP_STARTED", data: { role: ROLES[1] } },
    { name: "RUN_ERROR", data: { reason_code: "guard_blocked", text: "分析步骤 current_total 未成功（blocked）" } },
  ]);

  it("RUN_ERROR → final null、error 有值、done", () => {
    expect(state.final).toBeNull();
    expect(state.error).toEqual<RunErrorData>({
      reason_code: "guard_blocked",
      text: "分析步骤 current_total 未成功（blocked）",
    });
    expect(state.done).toBe(true);
  });

  it("被拒步停在 started（无 TOOL_CALL_RESULT/STEP_FINISHED，N3 被拒 SQL 不出网）", () => {
    const blocked = state.steps.find((s) => s.role === ROLES[1]);
    expect(blocked?.phase).toBe("started");
    expect(state.steps.find((s) => s.role === ROLES[0])?.phase).toBe("finished");
  });
});

describe("reduceAnalysisEvents · 幂等初态", () => {
  it("空事件流即初态", () => {
    expect(reduceAnalysisEvents([])).toEqual(initialStreamState());
  });
});

describe("parseSseFrame（手动分帧，不用 EventSource）", () => {
  it("解析 event + data 双行帧", () => {
    const frame = 'event: TOOL_CALL_RESULT\ndata: {"role":"baseline_total","row_count":1}';
    expect(parseSseFrame(frame)).toEqual({
      name: "TOOL_CALL_RESULT",
      data: { role: "baseline_total", row_count: 1 },
    });
  });

  it(" STATE_SNAPSHOT 的嵌套 JSON 原样解析（不丢字段）", () => {
    const frame = 'event: STATE_SNAPSHOT\ndata: {"kind":"answer","analysis":{"status":"ok"}}';
    const evt = parseSseFrame(frame);
    expect(evt?.name).toBe("STATE_SNAPSHOT");
    expect(evt?.data).toEqual({ kind: "answer", analysis: { status: "ok" } });
  });

  it("无 event 行 / 空 data → null（跳过心跳与半帧）", () => {
    expect(parseSseFrame(": ping")).toBeNull();
    expect(parseSseFrame("event: RUN_FINISHED")).toBeNull();
  });

  it("未知事件名保留原样（契约外值不猜，交调用侧降级）", () => {
    const evt = parseSseFrame('event: MYSTERY\ndata: {"a":1}');
    expect(evt).toEqual({ name: "MYSTERY", data: { a: 1 } });
  });
});

describe("streamAnalysis（fetch + ReadableStream；token 只在头不进 URL）", () => {
  /** 用给定 chunk 数组造一个 SSE Response（每元素一个 ReadableStream chunk，可拆帧）。 */
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

  /** 临时替换 globalThis.fetch，回传捕获的 url/headers。 */
  async function collect(
    chunks: string[],
    token: string,
  ): Promise<{ events: AnalysisStreamEvent[]; url: string; headers: Record<string, string> }> {
    let url = "";
    let headers: Record<string, string> = {};
    const orig = globalThis.fetch;
    globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => {
      url = String(u);
      headers = (init?.headers ?? {}) as Record<string, string>;
      return Promise.resolve(sseResponse(chunks));
    }) as unknown as typeof fetch;
    try {
      const events: AnalysisStreamEvent[] = [];
      for await (const evt of streamAnalysis({ question: "q", model: "finance", session_id: "s" }, token)) {
        events.push(evt);
      }
      return { events, url, headers };
    } finally {
      globalThis.fetch = orig;
    }
  }

  it("按序产出事件；token 进 Authorization 头、不进 URL query", async () => {
    const { events, url, headers } = await collect(
      [
        'event: RUN_STARTED\ndata: {"intent":"change_contribution"}\n\n',
        'event: STATE_SNAPSHOT\ndata: {"kind":"answer"}\n\n',
        'event: RUN_FINISHED\ndata: {"status":"ok"}\n\n',
      ],
      "TOK123",
    );
    expect(events.map((e) => e.name)).toEqual(["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]);
    expect(headers.Authorization).toBe("Bearer TOK123");
    expect(headers.Accept).toBe("text/event-stream");
    expect(url).not.toContain("TOK123");
  });

  it("跨 chunk 拆帧重组（半帧不丢、不重复）", async () => {
    const { events } = await collect(
      [
        'event: TOOL_CALL_RESULT\ndata: {"role":"a"', // 半帧
        ',"row_count":1}\n\nevent: RUN_FINISHED\ndata: {"status":"ok"}\n\n', // 补完 + 下一帧
      ],
      "",
    );
    expect(events.map((e) => e.name)).toEqual(["TOOL_CALL_RESULT", "RUN_FINISHED"]);
    expect(events[0].data).toEqual({ role: "a", row_count: 1 });
  });

  it("空 token 不注入 Authorization（后端 401 语义说话）", async () => {
    const { headers } = await collect(['event: RUN_FINISHED\ndata: {"status":"ok"}\n\n'], "");
    expect(headers.Authorization).toBeUndefined();
  });
});
