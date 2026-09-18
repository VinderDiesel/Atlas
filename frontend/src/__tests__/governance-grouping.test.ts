/**
 * 治理区二级分组的结构断言（ADR-0028 决策 ①；B1 收口判据的机器证据）。
 *
 * 断言 8 子页被划入 build / control 两组：
 * - build（语义构建面）：models / metrics / dimensions / synonyms / values
 * - control（治理控制面）：policies / reports / snapshots
 * - 8 个 key 不变（旧 `/governance/:section` 路由仍可解析）
 * - `unknownSectionNote`「8 子页」措辞仍属实
 *
 * 不测渲染（Tabs 视觉分区属 B1.2 实现），只测数据结构的归并与完整性。
 */
import { describe, expect, it } from "vitest";

import { GOV_SECTIONS } from "../panels/governance/GovernanceLayout";

const ALL_KEYS = GOV_SECTIONS.map((s) => s.key);

describe("治理区 build/control 二级分组（ADR-0028 决策 ①）", () => {
  it("恰好 8 子页，每个都有 group 字段", () => {
    expect(GOV_SECTIONS).toHaveLength(8);
    for (const section of GOV_SECTIONS) {
      expect(["build", "control"]).toContain(section.group);
    }
  });

  it("build 组含 models/metrics/dimensions/synonyms/values（5 个）", () => {
    const buildKeys = GOV_SECTIONS.filter((s) => s.group === "build").map((s) => s.key);
    expect(buildKeys).toEqual(["models", "metrics", "dimensions", "synonyms", "values"]);
  });

  it("control 组含 policies/reports/snapshots（3 个）", () => {
    const controlKeys = GOV_SECTIONS.filter((s) => s.group === "control").map((s) => s.key);
    expect(controlKeys).toEqual(["policies", "reports", "snapshots"]);
  });

  it("8 key 完整且唯一（旧路由兼容）", () => {
    expect(ALL_KEYS).toEqual([
      "models",
      "metrics",
      "dimensions",
      "synonyms",
      "values",
      "policies",
      "reports",
      "snapshots",
    ]);
  });
});
