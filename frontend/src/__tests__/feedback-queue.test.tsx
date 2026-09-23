/**
 * T12 FeedbackQueue SSR 冒烟测试。
 *
 * 口径：与 NodeExperiment 测试同纪律——renderToString 验证组件可挂载，
 * 不测交互（交互需 @testing-library/react，首版不引入）。
 */

import { describe, it, expect } from "vitest";
import { renderToString } from "react-dom/server";
import React from "react";

import { FeedbackQueuePanel } from "../panels/optimization/FeedbackQueue";

describe("FeedbackQueuePanel SSR smoke", () => {
  it("renders without crashing", () => {
    const html = renderToString(<FeedbackQueuePanel />);
    expect(html).toContain("反馈审核队列");
  });

  it("shows pending review alert", () => {
    const html = renderToString(<FeedbackQueuePanel />);
    expect(html).toContain("审核队列显示所有待审核反馈");
  });

  it("shows immutability warning", () => {
    const html = renderToString(<FeedbackQueuePanel />);
    expect(html).toContain("不可变审核证据");
  });

  it("shows review instructions", () => {
    const html = renderToString(<FeedbackQueuePanel />);
    expect(html).toContain("通过（approved）");
    expect(html).toContain("拒绝（rejected）");
    expect(html).toContain("撤销（revoke）");
  });

  it("renders empty table", () => {
    const html = renderToString(<FeedbackQueuePanel />);
    expect(html).toContain("暂无待审核反馈");
  });
});
