/**
 * T06c 运行事件归约断言（ADR-0031 D07③：图与时间线由同一 reducer 归约）。
 *
 * 纪律（与后端 `serving/control/events.py::reduce_events` 同语义，镜像不重算）：
 * - **按 seq 去重**：`seq ≤ lastSeq` 的帧（重复/迟到）一律忽略——重复帧不得
 *   产生第二次状态变化，迟到旧帧不得回溯新状态（不从 UI 时间推断执行）。
 * - **事件驱动**：RUN_ACCEPTED → queued、RUN_STARTED → running、RUN_FINISHED/
 *   RUN_INTERRUPTED 收终态与终态标记；未收终态事件时 `finished` 为 false。
 * - **未执行节点**：定义模板里的节点未收任何 NODE_* 事件时保持 `not_reached`，
 *   不编造开始/结束时间；`skipped` 只来自显式 NODE_SKIPPED。
 * - **契约外事件不猜**：未知 event_type 不改变状态（等价于 analysis-stream
 *   的 reducer 纪律：累积态保持）。
 * - **同源断言**：timeline 是归约过的同一事件序列（图/时间线不合并第二数据源）。
 */
import { describe, expect, it } from "vitest";

import { emptyRunState, reduceRunEvent } from "../state/run-events";
import type { RunEvent, RunEventType } from "../api/types";

const RUN_ID = "run-0001";
const RELEASE_ID = "rel-0001";

/** 合法基线事件（D07 schema_version=1；node 字段全 null；同 run/release ID）。 */
function baseEvent(
  seq: number,
  event_type: RunEventType,
  extra: Partial<RunEvent> = {},
): RunEvent {
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

/** RUN_ACCEPTED（seq=1）/ RUN_STARTED（seq=2）——账本红测夹具。 */
const acceptedEvent = baseEvent(1, "RUN_ACCEPTED");
const startedEvent = baseEvent(2, "RUN_STARTED");

describe("reduceRunEvent · 按 seq 去重（账本红测）", () => {
  it("重复帧只归约一次", () => {
    const accepted = reduceRunEvent(emptyRunState(), acceptedEvent);
    const once = reduceRunEvent(accepted, startedEvent);
    const twice = reduceRunEvent(once, startedEvent);
    expect(twice).toEqual(once);
    expect(twice.status).toBe("running");
  });

  it("状态只由事件驱动：accepted → queued、started → running", () => {
    const accepted = reduceRunEvent(emptyRunState(), acceptedEvent);
    expect(accepted.status).toBe("queued");
    expect(accepted.lastSeq).toBe(1);
    const started = reduceRunEvent(accepted, startedEvent);
    expect(started.status).toBe("running");
    expect(started.lastSeq).toBe(2);
  });

  it("乱序迟到帧不回溯状态（seq ≤ lastSeq 一律忽略）", () => {
    const once = reduceRunEvent(reduceRunEvent(emptyRunState(), acceptedEvent), startedEvent);
    const late = reduceRunEvent(once, acceptedEvent);
    expect(late).toEqual(once);
    expect(late.status).toBe("running");
  });

  it("空事件流即初态（未收事件不推断状态）", () => {
    const state = emptyRunState();
    expect(state.status).toBeNull();
    expect(state.lastSeq).toBe(0);
    expect(state.finished).toBe(false);
  });
});

describe("reduceRunEvent · 节点视图（未执行节点不编造时间）", () => {
  it("NODE_STARTED → running；NODE_FINISHED → finished（保留 seq 证据）", () => {
    let state = reduceRunEvent(emptyRunState(), acceptedEvent);
    state = reduceRunEvent(
      state,
      baseEvent(2, "NODE_STARTED", { node_id: "plan", node_run_id: "nr-1" }),
    );
    expect(state.nodes["plan"]).toEqual({
      node_id: "plan",
      status: "running",
      node_run_id: "nr-1",
      started_seq: 2,
      ended_seq: null,
    });
    state = reduceRunEvent(
      state,
      baseEvent(3, "NODE_FINISHED", { node_id: "plan", node_run_id: "nr-1" }),
    );
    expect(state.nodes["plan"]).toEqual({
      node_id: "plan",
      status: "finished",
      node_run_id: "nr-1",
      started_seq: 2,
      ended_seq: 3,
    });
  });

  it("定义模板中未收事件的节点保持 not_reached（不编造开始/结束时间）", () => {
    const state = reduceRunEvent(emptyRunState(["plan", "compile"]), startedEvent);
    expect(state.nodes["compile"]).toEqual({
      node_id: "compile",
      status: "not_reached",
      node_run_id: null,
      started_seq: null,
      ended_seq: null,
    });
  });

  it("NODE_SKIPPED 只来自显式事件；EDGE_TAKEN 记录已走的边（payload.to）", () => {
    let state = reduceRunEvent(emptyRunState(["plan", "compile"]), startedEvent);
    state = reduceRunEvent(
      state,
      baseEvent(3, "EDGE_TAKEN", { node_id: "retrieve", payload: { to: "plan" } }),
    );
    state = reduceRunEvent(state, baseEvent(4, "NODE_SKIPPED", { node_id: "compile" }));
    expect(state.edges).toEqual([["retrieve", "plan"]]);
    expect(state.nodes["compile"]?.status).toBe("skipped");
  });
});

describe("reduceRunEvent · 终态与同源时间线", () => {
  it("STATE_SNAPSHOT 记 result_kind；RUN_FINISHED 收终态与终态标记", () => {
    let state = reduceRunEvent(emptyRunState(), acceptedEvent);
    state = reduceRunEvent(
      state,
      baseEvent(2, "STATE_SNAPSHOT", { payload: { status: "succeeded", result_kind: "answer" } }),
    );
    expect(state.resultKind).toBe("answer");
    expect(state.finished).toBe(false);
    state = reduceRunEvent(state, baseEvent(3, "RUN_FINISHED", { payload: { status: "succeeded" } }));
    expect(state.status).toBe("succeeded");
    expect(state.finished).toBe(true);
  });

  it("RUN_INTERRUPTED → interrupted 终态（恢复不自动重跑的事实面）", () => {
    const state = reduceRunEvent(
      reduceRunEvent(emptyRunState(), acceptedEvent),
      baseEvent(2, "RUN_INTERRUPTED", { payload: { status: "interrupted" } }),
    );
    expect(state.status).toBe("interrupted");
    expect(state.finished).toBe(true);
  });

  it("timeline 是归约过的同一事件序列（图与时间线同源）", () => {
    const state = reduceRunEvent(reduceRunEvent(emptyRunState(), acceptedEvent), startedEvent);
    expect(state.timeline.map((e) => e.seq)).toEqual([1, 2]);
    expect(state.timeline.map((e) => e.event_type)).toEqual(["RUN_ACCEPTED", "RUN_STARTED"]);
  });

  it("契约外事件类型不猜：状态保持原样（不崩溃）", () => {
    const once = reduceRunEvent(emptyRunState(), acceptedEvent);
    const unknown = { ...startedEvent, event_type: "FUTURE_EVENT" } as unknown as RunEvent;
    const after = reduceRunEvent(once, unknown);
    expect(after).toEqual(once);
  });
});
