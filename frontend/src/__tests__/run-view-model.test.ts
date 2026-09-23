/**
 * T06d 运行展示纯逻辑断言（图布局 / 文案决策；组件只做渲染）。
 *
 * 纪律：
 * - **不伪造答案**：`availabilityNotice` 只在 `available` 且有结果时返回 null
 *   （正常渲染）；其余四态给明确文案（pending/not_retained/expired/restricted），
 *   `available` 但服务端未返回内容时给兜底文案——不渲染「没有结果」。
 * - **定义与执行不混淆**：图模板（RUN_GRAPH_SPEC）与执行状态（RunViewState）
 *   分开——模板边恒在（taken=false），只有 EDGE_TAKEN 事件走过的边标 taken。
 *   节点模板与 `agent/graph.py::GRAPH_NODE_IDS` 同源（8 节点）。
 * - **状态只来自事件**：`statusTag(null)`（未收事件）为「未知」，不猜运行中。
 */
import { describe, expect, it } from "vitest";

import { emptyRunState, reduceRunEvent } from "../state/run-events";
import type { RunEvent } from "../api/types";
import {
  RUN_GRAPH_SPEC,
  availabilityNotice,
  layoutRunGraph,
  payloadSummary,
  statusTag,
} from "../panels/runs/run-view-model";

function baseEvent(seq: number, event_type: RunEvent["event_type"], extra: Partial<RunEvent> = {}): RunEvent {
  return {
    schema_version: 1,
    run_id: "run-0001",
    seq,
    event_id: `evt-${seq}`,
    occurred_at: `2026-09-22T10:00:${String(seq).padStart(2, "0")}+08:00`,
    node_id: null,
    node_run_id: null,
    parent_node_run_id: null,
    attempt: null,
    event_type,
    release_id: "rel-1",
    payload: {},
    ...extra,
  };
}

describe("statusTag（状态只来自事件；未知不猜）", () => {
  it("六个状态映射中文标签与颜色；null（未收事件）为未知", () => {
    expect(statusTag("succeeded")).toEqual({ label: "成功", color: "green" });
    expect(statusTag("failed")).toEqual({ label: "失败", color: "red" });
    expect(statusTag("running")).toEqual({ label: "运行中", color: "blue" });
    expect(statusTag("queued")).toEqual({ label: "排队中", color: "default" });
    expect(statusTag("blocked")).toEqual({ label: "已拦截", color: "orange" });
    expect(statusTag("interrupted")).toEqual({ label: "已中断", color: "volcano" });
    expect(statusTag(null)).toEqual({ label: "未知", color: "default" });
  });
});

describe("availabilityNotice（不伪造答案；可用才渲染结果）", () => {
  it("available 且有结果 → null（正常渲染）；available 无结果 → 诚实兜底", () => {
    expect(availabilityNotice("available", true)).toBeNull();
    expect(availabilityNotice("available", false)).toContain("服务端未返回内容");
  });

  it("pending/not_retained/expired/restricted → 明确文案", () => {
    expect(availabilityNotice("pending", false)).toContain("尚未结束");
    expect(availabilityNotice("not_retained", false)).toContain("未保留");
    expect(availabilityNotice("expired", false)).toContain("过期");
    expect(availabilityNotice("restricted", false)).toContain("受限");
  });
});

describe("RUN_GRAPH_SPEC（固定模板；与 agent/graph.py::GRAPH_NODE_IDS 同源）", () => {
  it("8 个定义节点与关键模板边", () => {
    expect(RUN_GRAPH_SPEC.nodes.map((n) => n.id)).toEqual([
      "plan",
      "retrieve",
      "generate",
      "validate",
      "execute",
      "explain",
      "clarify",
      "handoff",
    ]);
    for (const edge of [
      ["plan", "retrieve"],
      ["retrieve", "generate"],
      ["generate", "validate"],
      ["validate", "execute"],
      ["execute", "explain"],
      ["plan", "execute"],
      ["plan", "clarify"],
      ["retrieve", "handoff"],
    ]) {
      expect(RUN_GRAPH_SPEC.templateEdges).toContainEqual(edge);
    }
  });
});

describe("layoutRunGraph（定义 vs 执行；未到达节点不编造时间）", () => {
  it("初态：模板节点全 not_reached；模板边全 taken=false", () => {
    const graph = layoutRunGraph(emptyRunState(RUN_GRAPH_SPEC.nodes.map((n) => n.id)));
    expect(graph.nodes).toHaveLength(8);
    expect(graph.nodes.every((n) => n.status === "not_reached")).toBe(true);
    expect(graph.edges).toHaveLength(RUN_GRAPH_SPEC.templateEdges.length);
    expect(graph.edges.every((e) => !e.taken)).toBe(true);
  });

  it("执行状态来自事件：NODE_STARTED 的节点 running；EDGE_TAKEN 的边 taken", () => {
    let state = reduceRunEvent(
      emptyRunState(RUN_GRAPH_SPEC.nodes.map((n) => n.id)),
      baseEvent(1, "RUN_ACCEPTED"),
    );
    state = reduceRunEvent(
      state,
      baseEvent(2, "NODE_STARTED", { node_id: "retrieve", node_run_id: "nr-1" }),
    );
    state = reduceRunEvent(
      state,
      baseEvent(3, "EDGE_TAKEN", { node_id: "retrieve", payload: { to: "generate" } }),
    );
    const graph = layoutRunGraph(state);
    expect(graph.nodes.find((n) => n.id === "retrieve")?.status).toBe("running");
    expect(graph.nodes.find((n) => n.id === "plan")?.status).toBe("not_reached");
    const taken = graph.edges.filter((e) => e.taken);
    expect(taken).toHaveLength(1);
    expect([taken[0].source, taken[0].target]).toEqual(["retrieve", "generate"]);
  });

  it("节点坐标来自模板（不重排事实；只读图固定布局）", () => {
    const graph = layoutRunGraph(emptyRunState());
    const plan = graph.nodes.find((n) => n.id === "plan");
    const generate = graph.nodes.find((n) => n.id === "generate");
    expect(plan?.position.x).toBeLessThan(generate?.position.x ?? 0);
  });
});

describe("payloadSummary（时间线的安全摘要；脱敏通道内容）", () => {
  it("浅层键值展开；空 payload 为空串", () => {
    expect(payloadSummary({ status: "succeeded", result_kind: "answer" })).toBe(
      "status=succeeded, result_kind=answer",
    );
    expect(payloadSummary({})).toBe("");
  });

  it("嵌套值 JSON 内联；超长截断（时间线列不膨胀）", () => {
    expect(payloadSummary({ artifacts: ["a-1", "a-2"] })).toBe('artifacts=["a-1","a-2"]');
    const long = payloadSummary({ text: "x".repeat(400) });
    expect(long.length).toBeLessThanOrEqual(161);
    expect(long.endsWith("…")).toBe(true);
  });
});
