/**
 * T06d 控制面运行 API 客户端断言（`api/control.ts`；ADR-0031 D13）。
 *
 * 关键契约：
 * - 路径只从 `api/endpoints.ts` 取；路径参数 URL 编码。
 * - query 参数用后端名（snake_case：`session_id`/`deployment_id`）；值为
 *   undefined/空串时不附带（URL 干净）。
 * - token 只在 Authorization 头（空串不注入）；非 2xx 抛 ApiError 原文透传。
 * - 列表响应是 `{items, next_cursor}` 透传（Page<T>）；`GET /feedback` 无分页
 *   语义 → 解包为 FeedbackRecord[]。
 * - T08c 草稿面：编辑是 PUT + `If-Match: "<revision>"`（CAS，不盲写）；校验
 *   不携请求体；发布/回退以 `expected_active_release_id` 做 CAS。
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import {
  createDeployment,
  createDraft,
  createSource,
  editDraft,
  exportDraftPatch,
  getDeployment,
  getDraft,
  getRelease,
  getRun,
  getRunArtifact,
  importRelease,
  listDeployments,
  listDrafts,
  listFeedback,
  listReleases,
  listRuns,
  listSessionRuns,
  listSessions,
  listSources,
  probeSource,
  publishRelease,
  reviewDraft,
  rollbackRelease,
  submitFeedback,
  validateDraft,
} from "../api/control";
import type {
  ActivationRecord,
  DeploymentRecord,
  DraftBody,
  DraftPatchView,
  DraftRecord,
  DraftValidationRecord,
  FeedbackRecord,
  ProbeResult,
  ReleaseImportRecord,
  ReleaseRecord,
  RunSummary,
  SourceRevisionRecord,
} from "../api/types";

const RUN: RunSummary = {
  run_id: "run-0001",
  session_id: "sess-1",
  deployment_id: "dep-1",
  scope: "finance",
  mode: "ask",
  status: "succeeded",
  result_availability: "available",
  result_kind: "answer",
  replay_of: null,
  last_seq: 12,
  created_at: "2026-09-22T10:00:00+08:00",
  updated_at: "2026-09-22T10:00:05+08:00",
};

interface FetchCall {
  url: string;
  init?: RequestInit;
}

/** 替换 globalThis.fetch 返回固定 JSON；回传捕获的调用与还原函数。 */
function mockJson(payload: unknown, status = 200): { calls: FetchCall[]; restore: () => void } {
  const calls: FetchCall[] = [];
  const orig = globalThis.fetch;
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(u), init });
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      headers: new Headers(),
      json: () => Promise.resolve(payload),
    } as unknown as Response);
  }) as unknown as typeof fetch;
  return { calls, restore: () => (globalThis.fetch = orig) };
}

function headersOf(call: FetchCall): Record<string, string> {
  return (call.init?.headers ?? {}) as Record<string, string>;
}

describe("listRuns / listSessions / listSessionRuns（GET + query 透传）", () => {
  it("listRuns：query 用后端参数名（snake_case），分页游标原样回传", async () => {
    const page = { items: [RUN], next_cursor: "2026-09-22T10:00:00+08:00|run-0001" };
    const { calls, restore } = mockJson(page);
    try {
      const got = await listRuns("TOK", {
        limit: 10,
        status: "succeeded",
        sessionId: "sess-1",
        since: "2026-09-22T09:00:00+08:00",
      });
      expect(got).toEqual(page);
    } finally {
      restore();
    }
    expect(calls[0].init?.method).toBe("GET");
    expect(calls[0].url).toContain("/api/v1/runs?");
    expect(calls[0].url).toContain("limit=10");
    expect(calls[0].url).toContain("status=succeeded");
    expect(calls[0].url).toContain("session_id=sess-1");
    expect(calls[0].url).toContain("since=2026-09-22T09%3A00%3A00%2B08%3A00");
    expect(headersOf(calls[0]).Authorization).toBe("Bearer TOK");
  });

  it("listRuns：未给过滤条件时 URL 无 query（不产生空参数）", async () => {
    const { calls, restore } = mockJson({ items: [], next_cursor: null });
    try {
      await listRuns("");
    } finally {
      restore();
    }
    expect(calls[0].url).toBe("/api/v1/runs");
    expect(headersOf(calls[0]).Authorization).toBeUndefined();
  });

  it("listSessions / listSessionRuns：路径参数编码 + deployment_id 参数名", async () => {
    const sessions = mockJson({ items: [], next_cursor: null });
    try {
      await listSessions("T", { scope: "finance" });
      expect(sessions.calls[0].url).toContain("/api/v1/sessions?scope=finance");
    } finally {
      sessions.restore();
    }

    const turns = mockJson({ items: [], next_cursor: null });
    try {
      await listSessionRuns("sess/1", "T", { deploymentId: "dep-1" });
      expect(turns.calls[0].url).toContain("/api/v1/sessions/sess%2F1?");
      expect(turns.calls[0].url).toContain("deployment_id=dep-1");
    } finally {
      turns.restore();
    }
  });
});

describe("getRun / getRunArtifact（GET 钻取）", () => {
  it("getRun：路径参数替换 + URL 编码", async () => {
    const { calls, restore } = mockJson({ run_id: "run 1" });
    try {
      await getRun("run 1", "T");
    } finally {
      restore();
    }
    expect(calls[0].url).toBe("/api/v1/runs/run%201");
    expect(calls[0].init?.method).toBe("GET");
  });

  it("getRunArtifact：两段路径参数替换", async () => {
    const { calls, restore } = mockJson({ artifact_id: "a-1" });
    try {
      await getRunArtifact("run-1", "a-1", "T");
    } finally {
      restore();
    }
    expect(calls[0].url).toBe("/api/v1/runs/run-1/artifacts/a-1");
  });
});

describe("submitFeedback / listFeedback（POST/GET + 错误透传）", () => {
  it("submitFeedback：POST JSON + Content-Type；201 响应逐字回传", async () => {
    const record: FeedbackRecord = {
      feedback_id: "fb-1",
      run_id: "run-0001",
      owner: { issuer: "iss", subject: "sub" },
      verdict: "down",
      comment: "口径不对",
      correction: null,
      status: "pending_review",
      training_eligible: false,
      created_at: "2026-09-22T10:01:00+08:00",
    };
    const { calls, restore } = mockJson(record, 201);
    try {
      const got = await submitFeedback(
        { run_id: "run-0001", verdict: "down", comment: "口径不对" },
        "T",
      );
      expect(got).toEqual(record);
    } finally {
      restore();
    }
    expect(calls[0].init?.method).toBe("POST");
    expect(headersOf(calls[0])["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      run_id: "run-0001",
      verdict: "down",
      comment: "口径不对",
    });
  });

  it("listFeedback：解包 {items} 为数组（无分页语义）", async () => {
    const { calls, restore } = mockJson({ items: [{ feedback_id: "fb-1" }] });
    try {
      const got = await listFeedback("T");
      expect(got).toEqual([{ feedback_id: "fb-1" }]);
    } finally {
      restore();
    }
    expect(calls[0].url).toBe("/api/v1/feedback");
  });

  it("非 2xx 抛 ApiError（detail 原文透传，含 410 artifact 过期）", async () => {
    const detail = "捕获正文已过保留期：a-1";
    const { restore } = mockJson({ detail }, 410);
    try {
      await expect(getRunArtifact("r", "a-1", "T")).rejects.toThrowError(
        new ApiError(410, detail, null),
      );
    } finally {
      restore();
    }
  });
});

describe("源接入 / 探测 / 部署（T07d 客户端面；ADR-0031 D03/D04/D13）", () => {
  const SOURCE: SourceRevisionRecord = {
    source_id: "doris-primary",
    version: 1,
    revision: "rev-1",
    connector_kind: "doris",
    secret_ref: "env:ATLAS_TEST_DORIS",
    allowed_catalogs: ["atlas"],
    allowed_tables: ["atlas.dwd.fact_trades"],
    timezone: "+08:00",
    tls_policy: "required",
    query_budget: 10000,
    created_by: { issuer: "atlas-local", subject: "operator" },
    created_at: "2026-09-22T12:00:00+08:00",
    last_probe: null,
  };

  it("listSources：GET /manage/sources 解包 items", async () => {
    const { calls, restore } = mockJson({ items: [SOURCE] });
    let got: SourceRevisionRecord[];
    try {
      got = await listSources("T");
    } finally {
      restore();
    }
    expect(got).toEqual([SOURCE]);
    expect(calls[0].url).toBe("/api/v1/manage/sources");
    expect(calls[0].init?.method).toBe("GET");
    expect(headersOf(calls[0]).Authorization).toBe("Bearer T");
  });

  it("createSource：POST JSON 逐键透传（secret_ref 为 env 引用）；201 回执原样返回", async () => {
    const body = {
      source_id: "doris-primary",
      revision: "rev-1",
      connector_kind: "doris" as const,
      secret_ref: "env:ATLAS_TEST_DORIS",
      allowed_catalogs: ["atlas"],
      allowed_tables: ["atlas.dwd.fact_trades"],
      timezone: "+08:00",
      tls_policy: "required" as const,
      query_budget: 10000,
    };
    const { calls, restore } = mockJson(SOURCE, 201);
    let got: SourceRevisionRecord;
    try {
      got = await createSource(body, "T");
    } finally {
      restore();
    }
    expect(got).toEqual(SOURCE);
    expect(calls[0].url).toBe("/api/v1/manage/sources");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(body);
  });

  it("probeSource：POST 钻取且不携带请求体（无任意测试 SQL 通道）", async () => {
    const result: ProbeResult = {
      probe_id: "probe-1",
      source_id: "doris-primary",
      version: 1,
      status: "blocked",
      blocked_reason: "credential_missing",
      observed_at: "2026-09-22T12:05:00+08:00",
      engine_version: null,
      schema_digest: null,
      capabilities: {
        dialect: "doris",
        read_only: false,
        metadata_probe: false,
        cancel_query: false,
        snapshot_read: false,
        consistent_analysis: false,
      },
      reproducible: false,
    };
    const { calls, restore } = mockJson(result);
    let got: ProbeResult;
    try {
      got = await probeSource("doris/primary", "T");
    } finally {
      restore();
    }
    expect(got).toEqual(result);
    expect(calls[0].url).toBe("/api/v1/manage/sources/doris%2Fprimary/probes");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.body).toBeUndefined();
  });

  it("listDeployments / createDeployment / getDeployment：路径与 body 形态", async () => {
    const record: DeploymentRecord = {
      deployment_id: "finance-live",
      scope: "finance",
      source_id: "doris-primary",
      active_release_id: null,
      revision: 1,
      created_by: { issuer: "atlas-local", subject: "operator" },
      created_at: "2026-09-22T12:10:00+08:00",
      updated_at: "2026-09-22T12:10:00+08:00",
    };
    const listed = mockJson({ items: [record] });
    try {
      expect(await listDeployments("T")).toEqual([record]);
    } finally {
      listed.restore();
    }
    expect(listed.calls[0].url).toBe("/api/v1/manage/deployments");

    const created = mockJson(record, 201);
    try {
      await createDeployment(
        { deployment_id: "finance-live", scope: "finance", source_id: "doris-primary" },
        "T",
      );
    } finally {
      created.restore();
    }
    expect(created.calls[0].url).toBe("/api/v1/manage/deployments");
    expect(created.calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(created.calls[0].init?.body))).toEqual({
      deployment_id: "finance-live",
      scope: "finance",
      source_id: "doris-primary",
    });

    const detail = mockJson(record);
    try {
      expect(await getDeployment("finance live", "T")).toEqual(record);
    } finally {
      detail.restore();
    }
    expect(detail.calls[0].url).toBe("/api/v1/manage/deployments/finance%20live");
    expect(detail.calls[0].init?.method).toBe("GET");
  });
});

describe("语义草稿与发布（T08c 客户端面；ADR-0031 D02/D04/D13）", () => {
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

  it("listDrafts / createDraft：解包 items + POST 逐键透传（content 整体提交）", async () => {
    const listed = mockJson({ items: [DRAFT] });
    try {
      expect(await listDrafts("T")).toEqual([DRAFT]);
    } finally {
      listed.restore();
    }
    expect(listed.calls[0].url).toBe("/api/v1/manage/drafts");
    expect(listed.calls[0].init?.method).toBe("GET");

    const body: DraftBody = {
      kind: "semantic",
      scope: "finance",
      content: {
        target: "semantic/ossie/atlas_finance.ossie.yaml",
        document: { semantic_model: [] },
      },
    };
    const created = mockJson(DRAFT, 201);
    try {
      expect(await createDraft(body, "T")).toEqual(DRAFT);
    } finally {
      created.restore();
    }
    expect(created.calls[0].url).toBe("/api/v1/manage/drafts");
    expect(created.calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(created.calls[0].init?.body))).toEqual(body);
  });

  it("getDraft / editDraft：路径编码 + PUT 带 If-Match 修订 ETag（CAS，不盲写）", async () => {
    const detail = mockJson(DRAFT);
    try {
      expect(await getDraft("draft 1", "T")).toEqual(DRAFT);
    } finally {
      detail.restore();
    }
    expect(detail.calls[0].url).toBe("/api/v1/manage/drafts/draft%201");
    expect(detail.calls[0].init?.method).toBe("GET");

    const edited = mockJson({ ...DRAFT, revision: 4, status: "draft" });
    try {
      await editDraft("draft-1", DRAFT.content, 3, "T");
    } finally {
      edited.restore();
    }
    expect(edited.calls[0].url).toBe("/api/v1/manage/drafts/draft-1");
    expect(edited.calls[0].init?.method).toBe("PUT");
    expect(headersOf(edited.calls[0])["If-Match"]).toBe('"3"');
    expect(headersOf(edited.calls[0])["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(edited.calls[0].init?.body))).toEqual({ content: DRAFT.content });
  });

  it("validateDraft / reviewDraft：校验不携请求体；审核携决定与意见", async () => {
    const validation: DraftValidationRecord = {
      validation_id: "val-1",
      draft_id: "draft-1",
      revision: 3,
      content_digest: "c".repeat(64),
      status: "failed",
      findings: [{ code: "structure", message: "同名 active 指标：gmv" }],
      actor: { issuer: "atlas-local", subject: "operator" },
      created_at: "2026-09-22T13:05:00+08:00",
    };
    const validated = mockJson(validation, 201);
    try {
      expect(await validateDraft("draft-1", "T")).toEqual(validation);
    } finally {
      validated.restore();
    }
    expect(validated.calls[0].url).toBe("/api/v1/manage/drafts/draft-1/validations");
    expect(validated.calls[0].init?.method).toBe("POST");
    expect(validated.calls[0].init?.body).toBeUndefined();

    const review = mockJson(
      { ...validation, review_id: "rv-1", decision: "approved", comment: "口径已核对" },
      201,
    );
    try {
      await reviewDraft("draft-1", { decision: "approved", comment: "口径已核对" }, "T");
    } finally {
      review.restore();
    }
    expect(review.calls[0].url).toBe("/api/v1/manage/drafts/draft-1/reviews");
    expect(review.calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(review.calls[0].init?.body))).toEqual({
      decision: "approved",
      comment: "口径已核对",
    });
  });

  it("exportDraftPatch：GET 7 键视图（base sha/摘要/影响面逐键回传）", async () => {
    const view: DraftPatchView = {
      draft_id: "draft-1",
      revision: 3,
      content_digest: "c".repeat(64),
      base_git_sha: "a".repeat(40),
      target: "semantic/ossie/atlas_finance.ossie.yaml",
      patch: "--- a/x\n+++ b/x\n",
      impact: {
        added_metrics: ["gmv"],
        removed_metrics: [],
        changed_metrics: ["aum"],
        added_dimensions: ["trades.region"],
        removed_dimensions: [],
        changed_dimensions: [],
      },
    };
    const { calls, restore } = mockJson(view);
    try {
      expect(await exportDraftPatch("draft-1", "T")).toEqual(view);
    } finally {
      restore();
    }
    expect(calls[0].url).toBe("/api/v1/manage/drafts/draft-1/patch");
    expect(calls[0].init?.method).toBe("GET");
  });

  it("importRelease / listReleases：导入 201 回执 + 发布目录解包", async () => {
    const imported: ReleaseImportRecord = {
      release_id: "d".repeat(64),
      content_digest: "c".repeat(64),
      draft_id: "draft-1",
      draft_revision: 3,
      status: "release_ready",
      target: "semantic/ossie/atlas_finance.ossie.yaml",
      created_at: "2026-09-22T13:20:00+08:00",
    };
    const posted = mockJson(imported, 201);
    try {
      expect(
        await importRelease(
          { draft_id: "draft-1", source_git_sha: "a".repeat(40), source_id: "doris-primary" },
          "T",
        ),
      ).toEqual(imported);
    } finally {
      posted.restore();
    }
    expect(posted.calls[0].url).toBe("/api/v1/manage/releases/imports");
    expect(posted.calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(posted.calls[0].init?.body))).toEqual({
      draft_id: "draft-1",
      source_git_sha: "a".repeat(40),
      source_id: "doris-primary",
    });

    const release: ReleaseRecord = {
      release_id: "d".repeat(64),
      content_digest: "c".repeat(64),
      scope: "finance",
      source_id: "doris-primary",
      source_revision: "rev-1",
      manifest: { release_id: "d".repeat(64) },
      created_by: { issuer: "atlas-local", subject: "operator" },
      created_at: "2026-09-22T13:20:00+08:00",
    };
    const listed = mockJson({ items: [release] });
    try {
      expect(await listReleases("T")).toEqual([release]);
    } finally {
      listed.restore();
    }
    expect(listed.calls[0].url).toBe("/api/v1/manage/releases");
    expect(listed.calls[0].init?.method).toBe("GET");
  });

  it("publishRelease / rollbackRelease：CAS body（首发 expected=null）与路径", async () => {
    const activation: ActivationRecord = {
      deployment_id: "finance-live",
      action: "publish",
      previous_release_id: null,
      active_release_id: "d".repeat(64),
      revision: 2,
      updated_at: "2026-09-22T13:25:00+08:00",
    };
    const published = mockJson(activation);
    try {
      expect(
        await publishRelease(
          "finance-live",
          { release_id: "d".repeat(64), expected_active_release_id: null },
          "T",
        ),
      ).toEqual(activation);
    } finally {
      published.restore();
    }
    expect(published.calls[0].url).toBe("/api/v1/manage/deployments/finance-live/releases");
    expect(published.calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(published.calls[0].init?.body))).toEqual({
      release_id: "d".repeat(64),
      expected_active_release_id: null,
    });

    const rolled = mockJson({
      ...activation,
      action: "rollback",
      previous_release_id: "e".repeat(64),
      active_release_id: "d".repeat(64),
      revision: 3,
    });
    try {
      await rollbackRelease(
        "finance-live",
        { release_id: "d".repeat(64), expected_active_release_id: "e".repeat(64) },
        "T",
      );
    } finally {
      rolled.restore();
    }
    expect(rolled.calls[0].url).toBe("/api/v1/manage/deployments/finance-live/rollbacks");
    expect(rolled.calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(rolled.calls[0].init?.body))).toEqual({
      release_id: "d".repeat(64),
      expected_active_release_id: "e".repeat(64),
    });
  });

  it("getRelease：路径参数编码；管理面 409 统一错误体全文透传（不美化）", async () => {
    const detail = mockJson({ release_id: "d".repeat(64) });
    try {
      await getRelease("d".repeat(64), "T");
    } finally {
      detail.restore();
    }
    expect(detail.calls[0].url).toBe(`/api/v1/manage/releases/${"d".repeat(64)}`);
    expect(detail.calls[0].init?.method).toBe("GET");

    // 管理面 409 是 D13 统一错误体（非 FastAPI `{"detail"}`）——客户端不美化，
    // 全文 JSON 透传给 UI 自行展示（与源接入面同口径）。
    const conflictBody = {
      error: {
        code: "revision_conflict",
        message: "草稿修订冲突：期望 3，实际 4",
        request_id: "req-1",
      },
    };
    const { restore } = mockJson(conflictBody, 409);
    try {
      await expect(editDraft("draft-1", DRAFT.content, 3, "T")).rejects.toThrowError(
        new ApiError(409, JSON.stringify(conflictBody), null),
      );
    } finally {
      restore();
    }
  });
});
