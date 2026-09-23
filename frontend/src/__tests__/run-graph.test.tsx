/**
 * T06d RunGraph 组件冒烟（renderToString）。
 *
 * 只读图（ADR-0031 D07②；许可门：@xyflow/react MIT + attribution 保留）：
 * - SSR 渲染 React Flow 容器（"react-flow" 标记）不抛错——构建链与运行链
 *   同一转译路径（app.test.tsx 的冒烟理由）；
 * - 图例说明节点状态语义与 taken 边口径（模板恒在、未到达不编造）；
 * - attribution 未被隐藏（许可门于 2026-09-22 核验：MIT + attribution 保留）；
 * - **SSR 边界（实测）**：React Flow 在无浏览器尺寸测量时不渲染节点/边 DOM
 *   （`react-flow__nodes` 为空容器）——state→布局映射由 run-view-model 纯函数
 *   锁定，图内节点渲染面由 T06e 浏览器验证覆盖。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import RunGraph from "../panels/runs/RunGraph";
import { RUN_GRAPH_SPEC } from "../panels/runs/run-view-model";
import { emptyRunState } from "../state/run-events";

const NODE_IDS = RUN_GRAPH_SPEC.nodes.map((n) => n.id);

describe("RunGraph（只读图：模板恒在，状态与 taken 来自事件）", () => {
  it("SSR 冒烟：渲染 React Flow 容器 + 图例 + 固定模板说明", () => {
    const html = renderToString(<RunGraph state={emptyRunState(NODE_IDS)} />);
    expect(html).toContain("react-flow");
    expect(html).toContain("未执行");
    expect(html).toContain("EDGE_TAKEN");
  });

  it("许可门：attribution 保留（ADR-0031 许可核验的可执行证据）", () => {
    const html = renderToString(<RunGraph state={emptyRunState(NODE_IDS)} />);
    expect(html).toContain("react-flow__attribution");
    expect(html).toContain("React Flow");
  });
});
