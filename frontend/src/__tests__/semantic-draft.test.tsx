/**
 * T08c 语义草稿与发布面板断言（`panels/setup/SemanticDraft.tsx`；ADR-0031 D02/D04/D13）。
 *
 * 断言面（诚实性优先）：
 * - 草稿创建写 SQLite 非权威副本：表单校验在提交前拦住非法输入（target 必须
 *   命中 `semantic/ossie/` 白名单、document 必须是合法 JSON 对象）——不是
 *   「先发请求再让后端 422」的通道；
 * - DraftRowsSection / PatchViewSection 等**只渲染已知键**——数据夹带的隐藏
 *   字段（password/host）不得出现在输出；
 * - 校验发现逐条展示（校验器 code + 原文）；patch 视图含 base Git sha/内容
 *   摘要/影响面（added/changed/removed × metric/dimension 六类）；
 * - 激活视图区分 publish/rollback；首发 previous=null 如实显示为「首次发布」；
 * - token 空串不发请求（认证前零 401 噪声）。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type {
  ActivationRecord,
  DraftPatchView,
  DraftRecord,
  DraftValidationRecord,
  PatchImpact,
  ReleaseImportRecord,
} from "../api/types";
import SemanticDraft, {
  ActivationResultSection,
  DraftRowsSection,
  ImpactSection,
  ImportResultSection,
  PatchViewSection,
  ValidationResultSection,
  draftStatusLabel,
  parseJsonObject,
  toDraftBody,
  validateDraftForm,
  type DraftFormValues,
} from "../panels/setup/SemanticDraft";

const DRAFT: DraftRecord = {
  draft_id: "draft-1",
  kind: "semantic",
  owner: { issuer: "atlas-local", subject: "operator" },
  scope: "finance",
  base_git_sha: "a".repeat(40),
  revision: 3,
  status: "reviewed",
  content: {
    target: "semantic/ossie/atlas_finance.ossie.yaml",
    document: { semantic_model: [] },
  },
  content_digest: "c".repeat(64),
  created_at: "2026-09-22T13:00:00+08:00",
  updated_at: "2026-09-22T13:10:00+08:00",
};

const FORM: DraftFormValues = {
  scope: "finance",
  target: "semantic/ossie/atlas_finance.ossie.yaml",
  documentText: '{"semantic_model": []}',
};

const IMPACT: PatchImpact = {
  added_metrics: ["gmv"],
  removed_metrics: [],
  changed_metrics: ["aum"],
  added_dimensions: ["trades.region"],
  removed_dimensions: [],
  changed_dimensions: [],
};

describe("parseJsonObject（JSON 文本 → 对象；错误原文不吞）", () => {
  it("合法对象 → ok；数组/标量/非法 JSON → 具体错误", () => {
    const ok = parseJsonObject('{"a": 1}');
    expect(ok).toEqual({ ok: true, value: { a: 1 } });

    expect(parseJsonObject("[1, 2]").ok).toBe(false);
    expect(parseJsonObject("42").ok).toBe(false);
    const bad = parseJsonObject("{nope}");
    expect(bad.ok).toBe(false);
    if (!bad.ok) {
      expect(bad.error).not.toBe("");
    }
  });
});

describe("validateDraftForm（向导只收白名单目标与合法 JSON）", () => {
  it("合法表单：零错误", () => {
    expect(validateDraftForm(FORM)).toEqual([]);
  });

  it("target 不在 semantic/ossie/ 前缀或非 .ossie.yaml → 拒", () => {
    expect(
      validateDraftForm({ ...FORM, target: "agent/prompts/intent_v1.yaml" }).length,
    ).toBeGreaterThan(0);
    expect(
      validateDraftForm({ ...FORM, target: "semantic/ossie/../../etc/passwd" }).length,
    ).toBeGreaterThan(0);
    expect(
      validateDraftForm({ ...FORM, target: "semantic/ossie/atlas_finance.yml" }).length,
    ).toBeGreaterThan(0);
  });

  it("document 非法 JSON / 非对象 → 拒；scope 空 → 拒", () => {
    expect(validateDraftForm({ ...FORM, documentText: "semantic_model: []" }).length).toBeGreaterThan(0);
    expect(validateDraftForm({ ...FORM, documentText: "[1]" }).length).toBeGreaterThan(0);
    expect(validateDraftForm({ ...FORM, scope: "" }).length).toBeGreaterThan(0);
  });

  it("toDraftBody：kind 恒 semantic；字段为后端 snake_case", () => {
    expect(toDraftBody(FORM)).toEqual({
      kind: "semantic",
      scope: "finance",
      content: {
        target: "semantic/ossie/atlas_finance.ossie.yaml",
        document: { semantic_model: [] },
      },
    });
  });
});

describe("draftStatusLabel（7 状态全覆盖）", () => {
  it("每个状态都有中文标签（含 draft/reviewed 两个代表值）", () => {
    const statuses = [
      "draft",
      "validated",
      "reviewed",
      "source_imported",
      "release_ready",
      "published",
      "retired",
    ] as const;
    for (const status of statuses) {
      expect(draftStatusLabel(status)).not.toBe("");
    }
    expect(draftStatusLabel("draft")).toContain("草稿");
    expect(draftStatusLabel("reviewed")).toContain("审核");
  });
});

describe("DraftRowsSection（只渲染已知键；不泄漏隐藏字段）", () => {
  it("渲染草稿行与状态中文；base_git_sha 可回显", () => {
    const html = renderToString(
      <DraftRowsSection items={[DRAFT]} selectedId={null} onSelect={() => {}} />,
    );
    expect(html).toContain("draft-1");
    expect(html).toContain("审核");
    expect(html).toContain("a".repeat(40));
  });

  it("数据夹带 password/host 时输出仍不含（UI 不渲染未知键）", () => {
    const tainted = { ...DRAFT, password: "sup3r-secret-pw", host: "10.9.8.7" } as unknown as DraftRecord;
    const html = renderToString(
      <DraftRowsSection items={[tainted]} selectedId={null} onSelect={() => {}} />,
    );
    expect(html).not.toContain("sup3r-secret-pw");
    expect(html).not.toContain("10.9.8.7");
  });
});

describe("ValidationResultSection / ImpactSection（证据与影响面）", () => {
  it("failed：findings 逐条（校验器 code + 原文）+ 绑定 revision/摘要", () => {
    const validation: DraftValidationRecord = {
      validation_id: "val-1",
      draft_id: "draft-1",
      revision: 3,
      content_digest: "c".repeat(64),
      status: "failed",
      findings: [
        { code: "structure", message: "同名 active 指标：gmv" },
        { code: "governance", message: "血缘缺失：fact_trades" },
      ],
      actor: { issuer: "atlas-local", subject: "operator" },
      created_at: "2026-09-22T13:05:00+08:00",
    };
    const html = renderToString(<ValidationResultSection validation={validation} />);
    expect(html).toContain("不通过");
    expect(html).toContain("同名 active 指标：gmv");
    expect(html).toContain("血缘缺失：fact_trades");
    expect(html).toContain("structure");
    expect(html).toContain("governance");
  });

  it("ImpactSection：六类逐项；空数组显示「无」；全空显示「无变更」", () => {
    const html = renderToString(<ImpactSection impact={IMPACT} />);
    expect(html).toContain("新增指标");
    expect(html).toContain("gmv");
    expect(html).toContain("aum");
    expect(html).toContain("trades.region");

    const empty = renderToString(
      <ImpactSection
        impact={{
          added_metrics: [],
          removed_metrics: [],
          changed_metrics: [],
          added_dimensions: [],
          removed_dimensions: [],
          changed_dimensions: [],
        }}
      />,
    );
    expect(empty).toContain("无变更");
  });
});

describe("PatchViewSection（base sha / 摘要 / patch 原文 / 影响面）", () => {
  it("渲染 7 键视图；patch 文本原样", () => {
    const view: DraftPatchView = {
      draft_id: "draft-1",
      revision: 3,
      content_digest: "c".repeat(64),
      base_git_sha: "a".repeat(40),
      target: "semantic/ossie/atlas_finance.ossie.yaml",
      patch: "--- a/semantic/ossie/atlas_finance.ossie.yaml\n+++ b/semantic/ossie/atlas_finance.ossie.yaml\n",
      impact: IMPACT,
    };
    const html = renderToString(<PatchViewSection view={view} />);
    expect(html).toContain("a".repeat(40));
    expect(html).toContain("c".repeat(64));
    expect(html).toContain("semantic/ossie/atlas_finance.ossie.yaml");
    expect(html).toContain("+++ b/semantic/ossie/atlas_finance.ossie.yaml");
    expect(html).toContain("新增指标");
  });

  it("空 patch → 如实标注（草稿未编辑）", () => {
    const view: DraftPatchView = {
      draft_id: "draft-1",
      revision: 3,
      content_digest: "c".repeat(64),
      base_git_sha: "a".repeat(40),
      target: "semantic/ossie/atlas_finance.ossie.yaml",
      patch: "",
      impact: {
        added_metrics: [],
        removed_metrics: [],
        changed_metrics: [],
        added_dimensions: [],
        removed_dimensions: [],
        changed_dimensions: [],
      },
    };
    const html = renderToString(<PatchViewSection view={view} />);
    expect(html).toContain("空 patch");
  });
});

describe("ImportResultSection / ActivationResultSection（制品与激活回执）", () => {
  it("导入回执：release_ready 明文；release_id/摘要/target 逐键", () => {
    const record: ReleaseImportRecord = {
      release_id: "d".repeat(64),
      content_digest: "c".repeat(64),
      draft_id: "draft-1",
      draft_revision: 3,
      status: "release_ready",
      target: "semantic/ossie/atlas_finance.ossie.yaml",
      created_at: "2026-09-22T13:20:00+08:00",
    };
    const html = renderToString(<ImportResultSection record={record} />);
    expect(html).toContain("d".repeat(64));
    expect(html).toContain("release_ready");
    expect(html).toContain("未激活");
  });

  it("激活回执：publish 首发 previous=null 如实显示；rollback 显示 previous→active", () => {
    const publish: ActivationRecord = {
      deployment_id: "finance-live",
      action: "publish",
      previous_release_id: null,
      active_release_id: "d".repeat(64),
      revision: 2,
      updated_at: "2026-09-22T13:25:00+08:00",
    };
    const pubHtml = renderToString(<ActivationResultSection record={publish} />);
    expect(pubHtml).toContain("发布");
    expect(pubHtml).toContain("首次发布");
    expect(pubHtml).toContain("d".repeat(64));

    const rollback: ActivationRecord = {
      ...publish,
      action: "rollback",
      previous_release_id: "e".repeat(64),
      revision: 3,
    };
    const rollHtml = renderToString(<ActivationResultSection record={rollback} />);
    expect(rollHtml).toContain("回退");
    expect(rollHtml).toContain("e".repeat(64));
  });
});

describe("SemanticDraft（SSR 初始态）", () => {
  it("未认证：友好空状态（token 空串不发请求）", () => {
    const html = renderToString(<SemanticDraft token="" />);
    expect(html).toContain("语义模型");
    expect(html).toContain("尚未登录");
  });
});
