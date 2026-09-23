/**
 * 控制面运行 API 客户端（ADR-0031 D13：历史目录 / 会话 / 运行详情 / artifact / 反馈）。
 *
 * 复用 `api/client.ts` 的 getJson/postJson（Bearer 注入、非 2xx 抛 ApiError 原文
 * 透传、429 Retry-After 透传）——本文件只管**路径拼装与 query 透传**：
 * - 路径只从 `api/endpoints.ts` 取（0022 判据 9 的防漂移前提）；
 * - query 参数名用后端名（snake_case：`session_id`/`deployment_id`），调用侧
 *   接口用驼峰（TS 风格），映射集中在本文件；
 * - 值为 undefined/空串时不附带（URL 干净，测试锁定）；
 * - `GET /feedback` 无分页语义 → 解包 `{items}` 为数组（T12 接审核队列前不扩）。
 */
import { getJson, postJson, putJson } from "./client";
import { API } from "./endpoints";
import type {
  ActivationBody,
  ActivationRecord,
  ArtifactRecord,
  DeploymentBody,
  DeploymentRecord,
  DraftBody,
  DraftContent,
  DraftPatchView,
  DraftRecord,
  DraftReviewBody,
  DraftReviewRecord,
  DraftValidationRecord,
  FeedbackBody,
  FeedbackRecord,
  Page,
  ProbeResult,
  ReleaseImportBody,
  ReleaseImportRecord,
  ReleaseRecord,
  RunStatus,
  RunSummary,
  RunView,
  SessionSummary,
  SourceRevisionBody,
  SourceRevisionRecord,
} from "./types";

/** 拼接 query：undefined/空串跳过；值统一 encodeURIComponent。 */
function withQuery(
  path: string,
  params: Record<string, string | number | undefined>,
): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") {
      continue;
    }
    parts.push(`${key}=${encodeURIComponent(String(value))}`);
  }
  return parts.length === 0 ? path : `${path}?${parts.join("&")}`;
}

/** GET /runs 过滤条件（域/时间/状态/会话；游标与 limit 走分页）。 */
export interface RunListQuery {
  limit?: number;
  cursor?: string;
  scope?: string;
  status?: RunStatus;
  since?: string;
  until?: string;
  sessionId?: string;
}

/** 可见运行目录（服务端 ACL 裁剪；跨所有者仅限 read_summary 能力）。 */
export function listRuns(token: string, query: RunListQuery = {}): Promise<Page<RunSummary>> {
  return getJson<Page<RunSummary>>(
    withQuery(API.runs, {
      limit: query.limit,
      cursor: query.cursor,
      scope: query.scope,
      status: query.status,
      since: query.since,
      until: query.until,
      session_id: query.sessionId,
    }),
    token,
  );
}

/** GET /runs/{id}：运行详情视图（10 键；result 按 ACL 与 result_availability 裁剪）。 */
export function getRun(runId: string, token: string): Promise<RunView> {
  return getJson<RunView>(API.runDetail.replace("{run_id}", encodeURIComponent(runId)), token);
}

/** GET /runs/{id}/artifacts/{aid}：显式捕获正文（清理后 410 artifact_expired）。 */
export function getRunArtifact(
  runId: string,
  artifactId: string,
  token: string,
): Promise<ArtifactRecord> {
  const path = API.runArtifact
    .replace("{run_id}", encodeURIComponent(runId))
    .replace("{artifact_id}", encodeURIComponent(artifactId));
  return getJson<ArtifactRecord>(path, token);
}

/** GET /sessions 过滤条件。 */
export interface SessionListQuery {
  limit?: number;
  cursor?: string;
  scope?: string;
}

/** 本人会话目录（需 run.read_own；operator 403）。 */
export function listSessions(
  token: string,
  query: SessionListQuery = {},
): Promise<Page<SessionSummary>> {
  return getJson<Page<SessionSummary>>(
    withQuery(API.sessions, {
      limit: query.limit,
      cursor: query.cursor,
      scope: query.scope,
    }),
    token,
  );
}

/** GET /sessions/{id} 过滤条件。 */
export interface SessionRunQuery {
  limit?: number;
  cursor?: string;
  deploymentId?: string;
}

/** 会话回合（新→旧；未知与不可见统一 404，不泄露存在性）。 */
export function listSessionRuns(
  sessionId: string,
  token: string,
  query: SessionRunQuery = {},
): Promise<Page<RunSummary>> {
  const path = API.sessionDetail.replace("{session_id}", encodeURIComponent(sessionId));
  return getJson<Page<RunSummary>>(
    withQuery(path, {
      limit: query.limit,
      cursor: query.cursor,
      deployment_id: query.deploymentId,
    }),
    token,
  );
}

/** POST /feedback：提交最小反馈（201；审核状态与训练资格由服务端固定）。 */
export function submitFeedback(body: FeedbackBody, token: string): Promise<FeedbackRecord> {
  return postJson<FeedbackRecord>(API.feedback, body, token);
}

/** GET /feedback：本人反馈列表（新→旧；无分页——T12 前规模由服务端自限）。 */
export async function listFeedback(token: string): Promise<FeedbackRecord[]> {
  const page = await getJson<{ items: FeedbackRecord[] }>(API.feedback, token);
  return page.items;
}

// ---------------------------------------------------------------------------
// 数据源接入 / 探测 / 部署（T07；ADR-0031 D03/D04/D13）
// ---------------------------------------------------------------------------

/** GET /manage/sources：源目录（每源最新修订；last_probe 为探测证据摘要）。 */
export async function listSources(token: string): Promise<SourceRevisionRecord[]> {
  const page = await getJson<{ items: SourceRevisionRecord[] }>(API.manageSources, token);
  return page.items;
}

/** POST /manage/sources：追加源修订（201；秘密只收 env: 引用）。 */
export function createSource(
  body: SourceRevisionBody,
  token: string,
): Promise<SourceRevisionRecord> {
  return postJson<SourceRevisionRecord>(API.manageSources, body, token);
}

/**
 * POST /manage/sources/{id}/probes：受限探测（固定目录查询）。
 *
 * **不携带请求体**（后綴 422 且零副作用）——不提供任意「测试 SQL」通道。
 */
export function probeSource(sourceId: string, token: string): Promise<ProbeResult> {
  const path = API.manageSourceProbes.replace("{source_id}", encodeURIComponent(sourceId));
  return postJson<ProbeResult>(path, undefined, token);
}

/** GET /manage/deployments：部署目录（按已授权领域服务端裁剪）。 */
export async function listDeployments(token: string): Promise<DeploymentRecord[]> {
  const page = await getJson<{ items: DeploymentRecord[] }>(API.manageDeployments, token);
  return page.items;
}

/** POST /manage/deployments：创建 draft 绑定（201；首次发布前 active_release_id=null）。 */
export function createDeployment(body: DeploymentBody, token: string): Promise<DeploymentRecord> {
  return postJson<DeploymentRecord>(API.manageDeployments, body, token);
}

/** GET /manage/deployments/{id}：部署详情（发布/回退 CAS 的前置读取）。 */
export function getDeployment(deploymentId: string, token: string): Promise<DeploymentRecord> {
  const path = API.manageDeploymentDetail.replace(
    "{deployment_id}",
    encodeURIComponent(deploymentId),
  );
  return getJson<DeploymentRecord>(path, token);
}

// ---------------------------------------------------------------------------
// 语义草稿 / 校验 / 审核 / 补丁（T08a/T08a-s2；ADR-0031 D02/D04/D13）
// ---------------------------------------------------------------------------

/** GET /manage/drafts：草稿目录（按已授权领域服务端裁剪；不因人裁剪）。 */
export async function listDrafts(token: string): Promise<DraftRecord[]> {
  const page = await getJson<{ items: DraftRecord[] }>(API.manageDrafts, token);
  return page.items;
}

/** POST /manage/drafts：创建非权威草稿（201；不改变任何运行 Metric 与发布指针）。 */
export function createDraft(body: DraftBody, token: string): Promise<DraftRecord> {
  return postJson<DraftRecord>(API.manageDrafts, body, token);
}

/** GET /manage/drafts/{id}：草稿详情（含 content；未知与不可见按管理面区分）。 */
export function getDraft(draftId: string, token: string): Promise<DraftRecord> {
  const path = API.manageDraftDetail.replace("{draft_id}", encodeURIComponent(draftId));
  return getJson<DraftRecord>(path, token);
}

/**
 * PUT /manage/drafts/{id}：CAS 编辑（内容整体替换）。
 *
 * `ifMatch` = 当前 revision：后端缺头/格式错 422、不匹配 409——不盲写；
 * 编辑使 revision+1 并作废旧验证/审核（服务端原语）。
 */
export function editDraft(
  draftId: string,
  content: DraftContent,
  ifMatch: number,
  token: string,
): Promise<DraftRecord> {
  const path = API.manageDraftDetail.replace("{draft_id}", encodeURIComponent(draftId));
  return putJson<DraftRecord>(path, { content }, token, ifMatch);
}

/**
 * POST /manage/drafts/{id}/validations：确定性校验（结构/治理/策略）。
 * **不携带请求体**——校验对象就是草稿内容本身，不接受调用方指定规则集或结论。
 */
export function validateDraft(draftId: string, token: string): Promise<DraftValidationRecord> {
  const path = API.manageDraftValidations.replace("{draft_id}", encodeURIComponent(draftId));
  return postJson<DraftValidationRecord>(path, undefined, token);
}

/** POST /manage/drafts/{id}/reviews：人工审核（只认 validated；决定绑定当前摘要）。 */
export function reviewDraft(
  draftId: string,
  body: DraftReviewBody,
  token: string,
): Promise<DraftReviewRecord> {
  const path = API.manageDraftReviews.replace("{draft_id}", encodeURIComponent(draftId));
  return postJson<DraftReviewRecord>(path, body, token);
}

/** GET /manage/drafts/{id}/patch：最小统一 diff + 影响面（纯只读；未编辑即空 patch）。 */
export function exportDraftPatch(draftId: string, token: string): Promise<DraftPatchView> {
  const path = API.manageDraftPatch.replace("{draft_id}", encodeURIComponent(draftId));
  return getJson<DraftPatchView>(path, token);
}

// ---------------------------------------------------------------------------
// 发布制品 / 激活（T08b；ADR-0031 D04/D13）
// ---------------------------------------------------------------------------

/** POST /manage/releases/imports：导入已审核草稿的 Git 制品（201；不激活）。 */
export function importRelease(
  body: ReleaseImportBody,
  token: string,
): Promise<ReleaseImportRecord> {
  return postJson<ReleaseImportRecord>(API.manageReleaseImports, body, token);
}

/** POST /manage/deployments/{id}/releases：CAS 发布（首发 expected=null）。 */
export function publishRelease(
  deploymentId: string,
  body: ActivationBody,
  token: string,
): Promise<ActivationRecord> {
  const path = API.manageDeploymentReleases.replace(
    "{deployment_id}",
    encodeURIComponent(deploymentId),
  );
  return postJson<ActivationRecord>(path, body, token);
}

/** POST /manage/deployments/{id}/rollbacks：CAS 回退（只改指针；门禁与发布同等）。 */
export function rollbackRelease(
  deploymentId: string,
  body: ActivationBody,
  token: string,
): Promise<ActivationRecord> {
  const path = API.manageDeploymentRollbacks.replace(
    "{deployment_id}",
    encodeURIComponent(deploymentId),
  );
  return postJson<ActivationRecord>(path, body, token);
}

/** GET /manage/releases：发布历史（按已授权领域裁剪；脱敏登记行）。 */
export async function listReleases(token: string): Promise<ReleaseRecord[]> {
  const page = await getJson<{ items: ReleaseRecord[] }>(API.manageReleases, token);
  return page.items;
}

/** GET /manage/releases/{id}：发布详情（脱敏 Manifest，无模型正文与源凭据）。 */
export function getRelease(releaseId: string, token: string): Promise<ReleaseRecord> {
  const path = API.manageReleaseDetail.replace("{release_id}", encodeURIComponent(releaseId));
  return getJson<ReleaseRecord>(path, token);
}
