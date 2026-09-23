/**
 * T06d RunTimeline 组件冒烟（renderToString；宿规：组件只做渲染，纯逻辑已由
 * run-view-model.test.ts 锁定，此处不重复纯函数断言）。
 *
 * 断言面（冒烟而非快照；0018 判据 10）：
 * - 空事件 → 诚实空态文案（不伪造行）；
 * - 非空 → seq / 事件类型 / 节点 / 服务端时刻 / 安全摘要（payloadSummary）
 *   逐项出现在真实 HTML——时间线是「同一事件流」的第二渲染面。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { RunEvent } from "../api/types";
import RunTimeline from "../panels/runs/RunTimeline";

function event(seq: number, event_type: RunEvent["event_type"], extra: Partial<RunEvent> = {}): RunEvent {
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

describe("RunTimeline（服务端事件序列；按 seq 展示）", () => {
  it("空事件：诚实空态（不伪造行）", () => {
    const html = renderToString(<RunTimeline events={[]} />);
    expect(html).toContain("尚无事件");
  });

  it("渲染事件行：seq / 类型 / 节点 / 服务端时刻 / payloadSummary 摘要", () => {
    const html = renderToString(
      <RunTimeline
        events={[
          event(1, "RUN_ACCEPTED"),
          event(2, "NODE_STARTED", { node_id: "retrieve", node_run_id: "nr-1" }),
          event(3, "STATE_SNAPSHOT", { payload: { status: "succeeded", result_kind: "answer" } }),
        ]}
      />,
    );
    expect(html).toContain("RUN_ACCEPTED");
    expect(html).toContain("NODE_STARTED");
    expect(html).toContain("STATE_SNAPSHOT");
    expect(html).toContain("retrieve");
    expect(html).toContain("10:00:02");
    expect(html).toContain("status=succeeded, result_kind=answer");
  });
});
