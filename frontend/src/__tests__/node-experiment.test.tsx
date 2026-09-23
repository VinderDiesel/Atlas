/**
 * T11 节点实验面板冒烟测试。
 *
 * 测试范围：
 * - 面板 renderToString 成功（转译链不断裂）
 * - 关键文本出现在 SSR 输出中
 *
 * 与 app.test.tsx 同口径：renderToString 冒烟而非 @testing-library 交互。
 */

import { describe, it, expect } from "vitest";
import { renderToString } from "react-dom/server";
import React from "react";
import { NodeExperimentPanel } from "../panels/optimization/NodeExperiment";

describe("NodeExperimentPanel", () => {
  it("renders to string without errors", () => {
    const html = renderToString(<NodeExperimentPanel />);
    expect(html).toBeTruthy();
    expect(html.length).toBeGreaterThan(100);
  });

  it("contains key safety text", () => {
    const html = renderToString(<NodeExperimentPanel />);
    expect(html).toContain("实验不修改 active 发布");
  });

  it("contains form labels", () => {
    const html = renderToString(<NodeExperimentPanel />);
    expect(html).toContain("Baseline Release ID");
    expect(html).toContain("Candidate Release ID");
    expect(html).toContain("节点类型");
    expect(html).toContain("数据集版本");
  });

  it("contains submit button", () => {
    const html = renderToString(<NodeExperimentPanel />);
    expect(html).toContain("运行实验");
  });

  it("contains history section with pending message", () => {
    const html = renderToString(<NodeExperimentPanel />);
    expect(html).toContain("实验历史");
    expect(html).toContain("API 集成待完成");
  });
});
