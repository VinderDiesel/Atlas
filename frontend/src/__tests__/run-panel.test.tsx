/**
 * T06d RunPanel 组件冒烟（renderToString；SSR 不跑 effect，初始态即真实空态）
 * + 捕获引用提取纯函数。
 *
 * 断言面：
 * - collectArtifactIds：引用只来自事件流 STATE_SNAPSHOT 的脱敏 payload
 *   （去重保序；非数组/非字符串项忽略——不猜）；
 * - RunPanel 初始态：未认证提示 + 未选择运行（组件不自造数据）；
 * - RunDetailSection 数据驱动：available+result → 结果面；not_retained →
 *   只给提示不渲染结果（不伪造答案）；artifact 引用 → 钻取入口。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { RunEvent, RunView } from "../api/types";
import RunPanel, { RunDetailSection } from "../panels/runs/RunPanel";
import { RUN_GRAPH_SPEC, collectArtifactIds } from "../panels/runs/run-view-model";
import { emptyRunState, reduceRunEvent } from "../state/run-events";

const NODE_IDS = RUN_GRAPH_SPEC.nodes.map((n) => n.id);

function baseEvent(
  seq: number,
  event_type: RunEvent["event_type"],
  extra: Partial<RunEvent> = {},
): RunEvent {
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

const DETAIL: RunView = {
  run_id: "run-0001",
  session_id: "sess-1",
  release_id: "rel-1",
  status: "succeeded",
  result: {
    kind: "answer",
    question: "2013 Q2 总交易额",
    sql: "SELECT 1",
    columns: ["c"],
    rows: [[1]],
    row_count: 1,
  },
  result_availability: "available",
  data_identity: null,
  replay_of: null,
  last_seq: 5,
  trace_summary: { result_kind: "answer" },
};

describe("collectArtifactIds（引用只来自事件；不猜）", () => {
  it("从 STATE_SNAPSHOT payload 提取（去重保序）；忽略非数组项", () => {
    let state = emptyRunState(NODE_IDS);
    state = reduceRunEvent(
      state,
      baseEvent(1, "STATE_SNAPSHOT", { payload: { artifacts: ["a-1"] } }),
    );
    state = reduceRunEvent(
      state,
      baseEvent(2, "STATE_SNAPSHOT", { payload: { artifacts: ["a-1", "a-2"] } }),
    );
    state = reduceRunEvent(
      state,
      baseEvent(3, "STATE_SNAPSHOT", { payload: { artifacts: "bad" } }),
    );
    expect(collectArtifactIds(state)).toEqual(["a-1", "a-2"]);
  });

  it("空归约态 → 空数组（未捕获的运行不编造引用）", () => {
    expect(collectArtifactIds(emptyRunState(NODE_IDS))).toEqual([]);
  });
});

describe("RunPanel（SSR 初始态；组件不自造数据）", () => {
  it("未认证：友好空状态（token 空串不发请求）", () => {
    const html = renderToString(<RunPanel token="" />);
    expect(html).toContain("运行历史");
    expect(html).toContain("尚未登录");
  });
});

describe("RunDetailSection（数据驱动渲染面）", () => {
  it("available 且有 result：渲染结果面（SQL / 行数 / 表格）", () => {
    const html = renderToString(
      <RunDetailSection
        detail={DETAIL}
        runState={emptyRunState(NODE_IDS)}
        token="T"
        artifactViews={{}}
        onDrillArtifact={() => {}}
      />,
    );
    expect(html).toContain("SELECT 1");
    expect(html).toContain("返回 1 行");
  });

  it("not_retained：只给提示，不渲染结果（不伪造答案）", () => {
    const html = renderToString(
      <RunDetailSection
        detail={{ ...DETAIL, result: null, result_availability: "not_retained" }}
        runState={emptyRunState(NODE_IDS)}
        token="T"
        artifactViews={{}}
        onDrillArtifact={() => {}}
      />,
    );
    expect(html).toContain("未保留");
    expect(html).not.toContain("SELECT 1");
  });

  it("捕获引用来自事件流：给出钻取入口（未点击不渲染正文）", () => {
    let state = emptyRunState(NODE_IDS);
    state = reduceRunEvent(
      state,
      baseEvent(1, "STATE_SNAPSHOT", { payload: { artifacts: ["a-1"] } }),
    );
    const html = renderToString(
      <RunDetailSection
        detail={DETAIL}
        runState={state}
        token="T"
        artifactViews={{}}
        onDrillArtifact={() => {}}
      />,
    );
    expect(html).toContain("a-1");
    expect(html).toContain("查看捕获正文");
  });
});
