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
 *    双引号包裹会被误提取成第 17 条而断言变红。
 * 3. 集合必须与后端契约 v2（ADR-0022）逐条对应：16 条 =
 *    15 条 /api/v1 前缀路径（1 探针 + 4 业务 + 8 治理集合 + 2 钻取）+ 1 条根探针。
 */
export const API = {
  // 前缀探针（POST/GET 面不消费；与根探针同 body，ADR-0022 决策 ①）
  health: "/api/v1/health",

  // 业务面 4 条（POST；ADR-0022 决策 ③）
  plan: "/api/v1/plan",
  compile: "/api/v1/compile",
  ask: "/api/v1/ask",
  planExecute: "/api/v1/plan/execute",

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
} as const;

/**
 * 根探针路径。**前端不调用**；仅为 0022 判据 9 的集合相等而存在（探针契约，
 * 决策 ①）——根挂点服务 docker-compose healthcheck 与外部探针，不属前端请求面。
 */
export const PROBE_HEALTH = "/health";

/**
 * dev 签发中间件路径（vite dev-only；**不属 0022 契约的 16 条**）。
 *
 * 仅 `make ui-dev`（vite dev server）下由 `vite.config.ts` 的 devSignPlugin 提供；
 * `make serve-dev`（uvicorn 直服 dist）与容器无此端点 → 调用侧（api/devsign.ts）
 * 必须处理「不可用」降级。按判据 9 的正则定义（须以 `/api/v1` 或 `/health` 开头）
 * 本路径不会被提取，不影响 16 条集合相等断言。
 */
export const DEV_SIGN = "/__dev/sign";
