/**
 * API 路径常量唯一出口（ADR-0018 决策 ④；ADR-0022 判据 9 的 TS 侧提取对象）。
 *
 * 三条纪律（写第二处硬编码前先读这里）：
 * 1. 本文件是前端唯一允许出现 API 路径字符串的地方；组件与 fetch 封装一律 import。
 *    判据 9 要求「后端 openapi paths 集合 == 本文件正则提取的路径集合」双向相等，
 *    别处再硬写路径，防漂移断言即失效（设计页 §4.1 第三条）。
 * 2. 提取正则（Python 侧，tests/test_api_contract_v2.py）：
 *    /"(\/(?:api\/v1|health)[a-z0-9_\/{}.-]*)"/g —— 仅匹配**双引号字面量**。
 *    故：路径一律用双引号；注释里提及路径只写反引号形式（如 `/api/v1/ask`），
 *    双引号包裹会被误提取成多出的一条而断言变红。
 * 3. 集合必须与后端契约 v2（ADR-0022；业务面增量见 ADR-0026 / ADR-0028 ④a；
 *    认证面见 ADR-0031 D13；运行面见 ADR-0031 D07/D13 T05c；反馈面见 ADR-0031 D13 T05d；
 *    历史目录/会话/artifact 面见 ADR-0031 D13 T06a；SSE 事件面见 ADR-0031 D07/D13 T06b；
 *    源管理面见 ADR-0031 D03/D13 T07a/T07b；部署面见 ADR-0031 D04/D13 T07c；
 *    草稿面见 ADR-0031 D02/D04/D13 T08a/T08a-s2；发布面见 ADR-0031 D04/D13 T08b）
 *    逐条对应：43 条 = 42 条 /api/v1 前缀路径（1 探针 + 6 业务 + 8 治理集合 + 2 钻取
 *    + 4 认证 + 4 运行 + 2 会话 + 1 反馈 + 2 源管理 + 2 部署 + 5 草稿 + 5 发布）+ 1 条根探针。
 */
export const API = {
  // 前缀探针（POST/GET 面不消费；与根探针同 body，ADR-0022 决策 ①）
  health: "/api/v1/health",

  // 业务面 6 条（POST；ADR-0022 决策 ③ + ADR-0026 多步分析 + ADR-0028 ④a SSE 流式）
  plan: "/api/v1/plan",
  compile: "/api/v1/compile",
  ask: "/api/v1/ask",
  planExecute: "/api/v1/plan/execute",
  analyze: "/api/v1/analyze",
  analyzeStream: "/api/v1/analyze/stream",

  // 治理面 8 集合（GET，只读 Git 文件与产物目录；ADR-0022 决策 ⑤）
  governanceModels: "/api/v1/governance/models",
  governanceMetrics: "/api/v1/governance/metrics",
  governanceDimensions: "/api/v1/governance/dimensions",
  governanceSynonyms: "/api/v1/governance/synonyms",
  governanceValues: "/api/v1/governance/values",
  governancePolicies: "/api/v1/governance/policies",
  governanceReports: "/api/v1/governance/reports",
  governanceSnapshots: "/api/v1/governance/snapshots",

  // 治理面 2 钻取（调用侧把 {item}/{name} 替换为实际值；ADR-0022 决策 ⑤）
  governanceValueDetail: "/api/v1/governance/values/{item}",
  governanceReportDetail: "/api/v1/governance/reports/{name}",

  // 认证面 4 条（ADR-0031 D13：OIDC BFF 私有登录；未配置时 503 配置阻塞）
  // session 探测为前端首屏入口：200 已登录 / 401 未登录 / 503 演示模式
  authLogin: "/api/v1/auth/login",
  authCallback: "/api/v1/auth/callback",
  authLogout: "/api/v1/auth/logout",
  authSession: "/api/v1/auth/session",

  // 运行面 4 条（ADR-0031 D07/D13；T05c：POST 幂等提交 + GET 运行视图；需 Bearer 认证）
  // artifact 钻取（T06a）：200 正文 / 403 摘要 / 404 他人未知 / 410 已清理过期
  // events（T06b）：SSE 真事件流（fetch 带 Authorization；不把 token 放 URL）
  runs: "/api/v1/runs",
  runDetail: "/api/v1/runs/{run_id}",
  runArtifact: "/api/v1/runs/{run_id}/artifacts/{artifact_id}",
  runEvents: "/api/v1/runs/{run_id}/events",

  // 会话目录 2 条（ADR-0031 D13/T06a：GET 历史目录 + GET 会话轮次；需 run.read_own）
  sessions: "/api/v1/sessions",
  sessionDetail: "/api/v1/sessions/{session_id}",

  // 反馈面 1 条（ADR-0031 D13；T05d：POST 提交反馈 + GET 本人列表；需 Bearer 认证）
  feedback: "/api/v1/feedback",

  // 源管理面 2 条（ADR-0031 D03/D13；T07a：POST 追加源修订 + GET 源目录；
  // T07b：POST 受限探测（固定目录查询、不接受请求体）；需 source.manage）
  // 秘密只收 env: 引用（明文密码/DSN/未知字段在合同层 422）；last_probe 为探测证据摘要
  manageSources: "/api/v1/manage/sources",
  manageSourceProbes: "/api/v1/manage/sources/{source_id}/probes",

  // 部署面 2 条（ADR-0031 D04/D13；T07c：POST 创建 draft 绑定 + GET 列表/详情；
  // 需 deployment.manage；发布动作能力亦可读（发布/回退以当前指针做 CAS））
  // 创建即 draft：首次批准发布前 active_release_id=null；绑定源必须已配置
  manageDeployments: "/api/v1/manage/deployments",
  manageDeploymentDetail: "/api/v1/manage/deployments/{deployment_id}",

  // 草稿面 5 条（ADR-0031 D02/D04/D13；T08a：POST/GET 创建与目录 + GET/PUT 详情；
  // T08a-s2：POST 校验与审核 + GET 补丁导出；需 draft.edit，读取另容 draft.review/export）
  // 草稿非权威：创建/编辑不改变发布指针；编辑经 If-Match 修订 ETag CAS
  // 校验/审核证据绑定 revision + 内容摘要；导出为最小统一 diff（未编辑即空 patch）
  manageDrafts: "/api/v1/manage/drafts",
  manageDraftDetail: "/api/v1/manage/drafts/{draft_id}",
  manageDraftValidations: "/api/v1/manage/drafts/{draft_id}/validations",
  manageDraftReviews: "/api/v1/manage/drafts/{draft_id}/reviews",
  manageDraftPatch: "/api/v1/manage/drafts/{draft_id}/patch",

  // 发布面 5 条（ADR-0031 D04/D13；T08b：POST 制品导入 + POST 部署发布/回退；
  // GET 发布列/详情；导入只接受通过门禁的 Git 制品；发布/回退以当前指针做 CAS）
  // 发布动作需 release.publish/rollback；读取面折叠 release.import 与 deployment.manage
  manageReleaseImports: "/api/v1/manage/releases/imports",
  manageDeploymentReleases: "/api/v1/manage/deployments/{deployment_id}/releases",
  manageDeploymentRollbacks: "/api/v1/manage/deployments/{deployment_id}/rollbacks",
  manageReleases: "/api/v1/manage/releases",
  manageReleaseDetail: "/api/v1/manage/releases/{release_id}",

  // 诊断面 1 条（ADR-0031 D12/D13；T13：GET 管理面诊断——源连接/发布完整性/存储可写性）
  // 需 operator 能力（ops.read）；不返回 DSN/密钥/原始 Prompt
  manageDiagnostics: "/api/v1/manage/diagnostics",
} as const;

/**
 * 根探针路径。**前端不调用**；仅为 0022 判据 9 的集合相等而存在（探针契约，
 * 决策 ①）——根挂点服务 docker-compose healthcheck 与外部探针，不属前端请求面。
 */
export const PROBE_HEALTH = "/health";

/**
 * dev 签发中间件路径（vite dev-only；**不属 0022 契约的路径集合**）。
 *
 * 仅 `make ui-dev`（vite dev server）下由 `vite.config.ts` 的 devSignPlugin 提供；
 * `make serve-dev`（uvicorn 直服 dist）与容器无此端点 → 调用侧（api/devsign.ts）
 * 必须处理「不可用」降级。按判据 9 的正则定义（须以 `/api/v1` 或 `/health` 开头）
 * 本路径不会被提取，不影响集合相等断言。
 */
export const DEV_SIGN = "/__dev/sign";
