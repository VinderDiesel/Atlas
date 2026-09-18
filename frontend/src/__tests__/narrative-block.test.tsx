/**
 * NarrativeBlock 接地叙述渲染断言（ADR-0029 ③④；B4「组件按 grounded/fallback 呈现」）。
 *
 * 风格与 house 一致：`renderToString` + `toContain`（0018 判据 9/10 禁快照式断言）。
 * 关键契约断言：
 * - **降级不伪装**（③）：fallback/未接地标注「确定性模板，非模型生成」，不出现「模型补充」。
 * - **接地发货**（①）：grounded=true 呈现溯源（tier/model）与「不构成业务因果解释」。
 * - **缺失即 null**（⑤）：narrative 为 undefined（off/未请求）不渲染任何叙述 DOM。
 * - **零改写**（N1）：后端原样 text 逐字出现，前端不再算。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { NarrativeBlock as NarrativePayload } from "../api/types";
import NarrativeBlock from "../panels/workbench/NarrativeBlock";

const grounded: NarrativePayload = {
  text: "East 分支佣金收入最高，达 123.5 万元。",
  model: "atlas-instruct",
  tier: "self_hosted",
  grounded: true,
  fallback: false,
  reason_code: null,
};

const fallbackTemplate: NarrativePayload = {
  text: "按分支统计的佣金收入结果已返回。",
  model: "",
  tier: "none",
  grounded: false,
  fallback: true,
  reason_code: "self_hosted_unavailable",
};

describe("NarrativeBlock · 缺失（off / 未请求）", () => {
  it("narrative undefined → 不渲染任何叙述 DOM", () => {
    const html = renderToString(<NarrativeBlock narrative={undefined} />);
    expect(html).toBe("");
  });
});

describe("NarrativeBlock · 降级模板（fallback / 未接地）", () => {
  const html = renderToString(<NarrativeBlock narrative={fallbackTemplate} />);

  it("如实标注确定性模板，绝不伪装成模型生成（③）", () => {
    expect(html).toContain("确定性模板");
    expect(html).toContain("非模型生成");
    expect(html).not.toContain("模型补充叙述");
  });

  it("呈现 reason_code 与模板原文（不重算）", () => {
    expect(html).toContain("self_hosted_unavailable");
    expect(html).toContain("按分支统计的佣金收入结果已返回。");
  });
});

describe("NarrativeBlock · 接地发货（grounded=true）", () => {
  const html = renderToString(<NarrativeBlock narrative={grounded} />);

  it("呈现溯源（tier/model）并声明不构成因果（①）", () => {
    expect(html).toContain("模型补充叙述");
    expect(html).toContain("self_hosted");
    expect(html).toContain("atlas-instruct");
    expect(html).toContain("不构成业务因果解释");
  });

  it("后端原样文本逐字出现，含其接地数字（N1：前端不改写）", () => {
    expect(html).toContain("123.5 万元");
    expect(html).not.toContain("确定性模板");
  });
});
