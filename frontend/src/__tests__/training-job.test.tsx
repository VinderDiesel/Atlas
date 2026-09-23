/**
 * T14 TrainingJob SSR 冒烟测试。
 *
 * 口径：与 FeedbackQueue/NodeExperiment 测试同纪律——renderToString 验证组件可挂载，
 * 不测交互（交互需 @testing-library/react，首版不引入）。
 */

import { describe, it, expect } from "vitest";
import { renderToString } from "react-dom/server";
import React from "react";

import { TrainingJobPanel } from "../panels/optimization/TrainingJob";

describe("TrainingJobPanel SSR smoke", () => {
  it("renders without crashing", () => {
    const html = renderToString(<TrainingJobPanel />);
    expect(html).toContain("训练配方");
  });

  it("shows recipe details", () => {
    const html = renderToString(<TrainingJobPanel />);
    expect(html).toContain("intent_v1");
    expect(html).toContain("Qwen/Qwen2.5-7B-Instruct");
  });

  it("shows training instructions", () => {
    const html = renderToString(<TrainingJobPanel />);
    expect(html).toContain("训练需要显式预算批准");
  });

  it("shows blocked explanation", () => {
    const html = renderToString(<TrainingJobPanel />);
    expect(html).toContain("无 GPU 环境显示 BLOCKED 状态");
  });

  it("shows job history table", () => {
    const html = renderToString(<TrainingJobPanel />);
    expect(html).toContain("训练任务历史");
  });
});
