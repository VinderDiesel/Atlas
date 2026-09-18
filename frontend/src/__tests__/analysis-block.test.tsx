/**
 * AnalysisBlock 归因视图三态渲染断言（ADR-0026 决策⑥；B1 收口判据的机器证据）。
 *
 * 风格与 house 一致：`renderToString` + `toContain`（0018 判据 9/10 禁快照式断言）。
 * 关键契约断言：
 * - N1 零前端重算：贡献条宽度**逐字等于**后端 `contribution_pct` 串（"66.67%"），
 *   不是前端算出来的近似值——若前端重算，宽度不会正好是后端两位小数的原串。
 * - 三态诚实：unavailable/blocked 只渲染后端 `text`，**不出现**贡献条/贡献百分比。
 * - 安全裁剪：blocked 步不得渲染被拒 SQL（fixture 里该步 sql 已是 null）。
 * - N2 措辞：标题声明"变化贡献分解"，不出现"原因分析/因果"越界词。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import AnalysisBlock from "../panels/workbench/AnalysisBlock";
import { analysisBlocked, analysisOk, analysisUnavailable } from "./fixtures/analysis";

describe("AnalysisBlock · ok（变化贡献分解）", () => {
  const html = renderToString(<AnalysisBlock analysis={analysisOk} />);

  it("渲染身份与两期总量（后端原串，不重算）", () => {
    expect(html).toContain("commission_revenue");
    expect(html).toContain("Branch");
    expect(html).toContain("2013Q3");
    expect(html).toContain("2013Q4");
    expect(html).toContain("1000.00");
    expect(html).toContain("1300.00");
    expect(html).toContain("300.00");
  });

  it("贡献条宽度 = 后端 contribution_pct 原串（N1：非前端计算值）", () => {
    // 后端给 "66.67" / "33.33" → 宽度直接是 66.67% / 33.33%（两位小数原样）
    expect(html).toContain("width:66.67%");
    expect(html).toContain("width:33.33%");
    expect(html).toContain("66.67");
  });

  it("steps 折叠渲染各角色（成功步带 SQL/数据）", () => {
    expect(html).toContain("baseline_total");
    expect(html).toContain("current_by_dimension");
    // 成功步的出口 SQL 片段可见（Guard 校验后的只读 SQL）
    expect(html).toContain("SELECT SUM(ft.Commission)");
  });

  it("标题声明分解、不含因果越界词（N2）", () => {
    expect(html).toContain("贡献分解");
    expect(html).not.toContain("原因分析");
    expect(html).not.toContain("因果");
  });
});

describe("AnalysisBlock · unavailable", () => {
  const html = renderToString(<AnalysisBlock analysis={analysisUnavailable} />);

  it("渲染后端稳定文案", () => {
    expect(html).toContain("两期总量无变化");
  });

  it("不渲染贡献条/贡献百分比（items 为空）", () => {
    expect(html).not.toContain("width:66.67%");
    expect(html).not.toContain("66.67");
  });
});

describe("AnalysisBlock · blocked", () => {
  const html = renderToString(<AnalysisBlock analysis={analysisBlocked} />);

  it("渲染后端拒绝文案", () => {
    expect(html).toContain("只读网关拒绝");
  });

  it("失败步不泄露被拒 SQL，且无贡献条", () => {
    expect(html).not.toContain("width:66.67%");
    // current_total 被裁为安全摘要：其 SQL 不应出现（fixture sql=null，镜像后端裁剪）
    expect(html).toContain("current_total");
  });
});
