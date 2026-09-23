/**
 * HTTP 契约 → TS 类型（ADR-0022 契约 v2；设计页 §3.2/§3.4）。
 *
 * 纪律：
 * - `TurnPayload` 的 kind 无关字段是「字段全集稳定输出」（api.py `_turn_payload`
 *   docstring 原文）——一律 `| null` 而非 `?:`：服务端只会发 null，可选类型会让
 *   undefined 与 null 两种形态在组件里各写一遍分支（设计页 §3.4）；
 * - 键集与 `serving/api.py` 的 `_turn_payload()`（23 键，0026 起含 `analysis`）
 *   逐键对齐；键数由 Python 侧契约测试（tests/test_api_contract_v2.py 等）锁定，
 *   本文件只做镜像；
 * - 治理面 8 集合的 item 类型随各自子页落地批补齐（P2 前 6 子页 / P3
 *   reports、snapshots 两子页）；键集与 serving/governance.py 的构造逐字对齐。
 */
import type { TurnKind } from "../lib/order";

/** 语义模型域（serving/api.py `_model_name` 白名单）。 */
export type ModelDomain = "finance" | "retail";

/** 统一信封（0022 决策 ⑤：kind / count / sources / items）。 */
export interface Envelope<K extends string, T> {
  kind: K; // 如 "governance.models"
  count: number; // == items.length，前端不得自行计算后覆盖
  sources: string[]; // Git 文件路径（面板必须显示，0022 决策 ⑤ 的理由）
  items: T[];
}

/** 澄清请求载荷（serving/api.py `_clarify_payload`）。 */
export interface ClarifyPayload {
  question: string;
  reasons: string[];
  candidates: string[];
  kind: "ambiguous" | "relative_time" | "unmatched";
}

/** 归因解释（agent/graph.py `node_explain`：13 固定键 + 1 条件键）。 */
export interface Explanation {
  metric: string;
  metric_expression: string; // 语义层表达式；无则空串（不编造）
  dimensions: string[];
  time: string | null; // str(plan.time.value)，如 "2013"
  filters: string[]; // 已格式化的 "col op value" 串
  sql: string | null;
  tables: string[];
  row_count: number;
  latency_ms: number;
  path: string; // deterministic | candidate
  engine: string; // deterministic | stub | openai
  data_version: string | null; // 快照 sha（无快照不编造）
  data_refreshed_at: string | null;
  policy_effect?: string; // 仅策略生效轮追加（ADR-0011 硬化）
}

/** `/api/v1/ask`、`/api/v1/plan/execute` 与 `/api/v1/analyze` 的响应
 * （23 键，字段全集恒定）。 */
export interface TurnPayload {
  kind: TurnKind;
  session_id: string;
  question: string;
  turns_in_session: number;
  metric: string | null;
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  latency_ms: number;
  engine: string;
  path: string | null;
  usage: Record<string, number>;
  validation_issues: string[];
  explanation: Explanation | null;
  /** 图表 spec（ADR-0025 决策 ②）：键恒存，kind != "answer" 时为 null 而非缺失。 */
  chart: ChartSpec | null;
  clarification: ClarifyPayload | null;
  block_reason: string | null;
  error: string | null;
  handoff_reason: string | null;
  snapshot_sha: string | null;
  snapshot_bound_to_head: boolean | null;
  /** 多步分析投影（ADR-0026 决策 ⑥）：键恒存，非分析请求值为 null 而非缺失。 */
  analysis: AnalysisPayload | null;
  /**
   * 接地叙述块（ADR-0029 ⑤）：**条件键**——仅当请求 `llm=narrative` 且服务端发货/回落时出现；
   * `off`/未请求时键缺失（非 null）。`grounded=false` 文本永不发货（发确定性模板）。
   */
  narrative?: NarrativeBlock;
}

/**
 * 接地叙述块（ADR-0029 ③④，serving `NarrativeResult.to_payload()` 镜像）。
 * tier=none 表回落模板；grounded 仅对 LLM 文本为真；后端由服务端定（客户端不能选）。
 */
export interface NarrativeBlock {
  text: string;
  model: string;
  tier: "cloud" | "self_hosted" | "none";
  grounded: boolean;
  fallback: boolean;
  reason_code: string | null;
}

/**
 * 多步分析 17 键投影（ADR-0026 决策 ⑥，serving/api.py `_analysis_payload` 镜像）。
 * 不适用字段为 null / 空数组（字段全集恒定，同 `_turn_payload` 纪律）。
 */
export interface AnalysisPayload {
  schema_version: number;
  intent: string;
  /** ok | unavailable | blocked | error（`analysis_status` 的取值；clarify 不产本投影）。 */
  status: "ok" | "unavailable" | "blocked" | "error";
  metric: string;
  dimension: string | null;
  baseline: AnalysisPeriod;
  current: AnalysisPeriod;
  filters: PlanFilter[];
  snapshot_sha: string | null;
  semantic_sha256: string | null;
  recipe_version: number;
  /** Decimal→str 保精度：三值齐全才有对象（unavailable/blocked/error 为 null）。 */
  totals: { baseline: string; current: string; delta: string } | null;
  /** 贡献项（|delta| 降序；unavailable/blocked/error 为空数组）。 */
  items: AnalysisItem[];
  /** 步骤按角色序；失败步裁为安全摘要（sql null、columns/rows 空）。 */
  steps: AnalysisStep[];
  /** 机器可读原因码（ok 时 null）。 */
  reason_code: string | null;
  /** 面向用户的稳定文案（unavailable/blocked/error 非空；无编造数字）。 */
  text: string | null;
  /** 分析总耗时（父轮 latency_ms 是已执行子 SQL 耗时和，两者分工不同）。 */
  elapsed_ms: number;
}

/** 两期描述（`{granularity, value}`；value 透传不解析）。 */
export interface AnalysisPeriod {
  granularity: string;
  value: number | string;
}

/** 贡献项（决策⑤精确 Decimal 综合；数值一律字符串出网）。 */
export interface AnalysisItem {
  value: string;
  baseline: string | null;
  current: string | null;
  delta: string | null;
  contribution_pct: string | null;
}

/**
 * 分析步骤：成功步携带 sql/columns/rows/latency_ms；失败步（blocked/error）
 * 裁为安全摘要——sql 为 null、columns/rows 空、另带 reason_code
 * （"guard_blocked" | "execution_error"），被拒 SQL 与底层数据不出网。
 */
export interface AnalysisStep {
  role: string;
  kind: "answer" | "blocked" | "error";
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  latency_ms: number;
  reason_code?: string;
}

/**
 * 图表 spec（agent/tools/chart.py `render_chart` 的 JSON 镜像）——
 * **discriminated union**（设计页 §6.1）：`bar`/`line` 有 `data` 无
 * `columns`/`rows`，`table` 反之。`note` 在 `bar`/`line` 是**条件键**
 * （实测仅在 note_parts 非空时写入，故 `?:` 而非 `| null`）；在 `table`
 * 分支恒存。图表类型与轴选择是后端裁定，前端零决策（0025 决策 ①⑥）。
 */
interface ChartSeriesSpecBase {
  /** x 轴列名；无维度轴时为 `(行序)` 字面量（原样展示，不重算）。 */
  x: string;
  /** y 轴列名（恒 1 元；0025 决策 ⑥ 不扩多序列，yoy/pop 只画当期）。 */
  y: string[];
  /** 已执行 rows 的轴投影（原序；折线按行序连线，不排序不插值）。 */
  data: { x: unknown; y: unknown }[];
  /** 出口 SQL 摘要（可审计：schema 可溯源到具体 SQL）。 */
  sql_sha256: string;
  /** bar/line 分支恒 0（series 不截断）；table 分支为被截断行数。 */
  skipped: number;
  /** 条件键：多数值列仅渲染首个 / 时间轴兜底识别声明（仅在非空时携带）。 */
  note?: string;
}

export interface ChartBarSpec extends ChartSeriesSpecBase {
  type: "bar";
}

export interface ChartLineSpec extends ChartSeriesSpecBase {
  type: "line";
}

export interface ChartTableSpec {
  type: "table";
  columns: string[];
  /** 已按 MAX_TABLE_ROWS 截断的行（skipped 为被截断数）。 */
  rows: unknown[][];
  sql_sha256: string;
  /** 恒存键：降级原因原文（类目超限 / 无数值列）。 */
  note: string;
  /** 被截断的行数（0 = 未截断）。 */
  skipped: number;
}

export type ChartSpec = ChartBarSpec | ChartLineSpec | ChartTableSpec;

/** Plan 时间描述（compiler.TimeSpec 的 JSON 镜像）。 */
export interface PlanTime {
  granularity: string; // year / quarter / month / date
  value: number | string;
}

export interface PlanFilter {
  column: string;
  op: string;
  value: unknown;
}

export interface PlanOrder {
  column: string;
  desc: boolean;
}

/**
 * Plan 平铺 JSON（`_plan_payload`）：`/plan` 的 `plan` 键、
 * `/compile` 与 `/plan/execute` 的请求体同构（+model 等附加键）。
 */
export interface PlanPayload {
  metric: string;
  dimensions: string[];
  time: PlanTime | null;
  filters: PlanFilter[];
  order_by: PlanOrder[];
  limit: number;
}

/** `/api/v1/plan` 响应：kind=plan|clarify（二选一非 null）。 */
export interface PlanResponse {
  kind: "plan" | "clarify";
  plan: PlanPayload | null;
  clarification: ClarifyPayload | null;
}

/** `/api/v1/compile` 响应。 */
export interface CompileResponse {
  sql: string;
}

/** `/api/v1/health`（根与前缀双挂同 body；8 键恒定，degraded 时值为 null）。 */
export interface HealthPayload {
  status: string; // "ok" | "degraded"
  head_sha: string | null;
  snapshot_sha: string | null;
  snapshot_source: string | null; // env | head | latest
  snapshot_bound_to_head: boolean | null;
  snapshot_created_at: string | null;
  snapshot_tables: number | null;
  boot_id: string;
}

/** 业务面请求体（AskBody / CompileBody / PlanExecuteBody 的 TS 镜像）。 */
export interface AskBody {
  question: string;
  model: ModelDomain;
  session_id?: string;
  /**
   * LLM 意图旗标（ADR-0029 ⑤）：加性可选，缺省 `off` 逐字向后兼容。
   * 仅表达意图，后端由服务端定（客户端不能选 backend）；角色无该能力 → 403。
   */
  llm?: "off" | "candidate-fallback" | "narrative";
}

export interface CompileBody {
  metric: string;
  dimensions: string[];
  time: PlanTime | null;
  filters: PlanFilter[];
  order_by: PlanOrder[];
  limit: number;
  model: ModelDomain;
}

export type PlanExecuteBody = CompileBody & { session_id?: string; question?: string };

// ---------------------------------------------------------------------------
// 治理面（P2 落地：面板 4 前 6 子页 + RoleSwitcher；键集与 serving/governance.py
// 的构造函数逐字对齐——fixture 取自 2026-09-16 对本机服务的实测响应）
// ---------------------------------------------------------------------------

/** 端点 1 `_models_index`：语义模型清单。 */
export interface ModelsItem {
  domain: string;
  model_name: string;
  source_file: string;
  datasets: number;
  metrics: number;
  relationships: number;
  fields: number;
  /** ossie time_dimension 原样（无声明为 null；columns 粒度 → 物理列）。 */
  time_dimension: {
    table: string;
    mode: string;
    columns: Record<string, string>;
  } | null;
  policy: { default_row_policy: string | null };
  governance: {
    owner: string | null;
    version: number | null;
    status: string | null;
    review_cycle_days: number | null;
  };
  lineage: { source_tables: string[] };
  freshness: { schedule: string | null; sla_minutes: number | null };
}

/** FIBO 对齐条目（ossie mappings 值原样透传；`note` 非强制键）。 */
export interface FiboMappingEntry {
  concept: string;
  match_type: string;
  confidence: string;
  verified_at: string;
  note?: string;
}

/** 端点 2 `_metrics_index`：指标清单（fibo_alignment.mappings 恒 0|1 键）。 */
export interface MetricsItem {
  name: string;
  expression: string;
  description: string | null;
  synonyms: string[];
  owner: string | null;
  version: number | null;
  status: string | null;
  supersedes: string | null;
  lineage: { source_columns: string[] };
  quality: { gold_test_cases: string[]; expected_value_snapshot_sha: string | null };
  fibo_alignment: { mappings: Record<string, FiboMappingEntry> };
}

/** 端点 3 `_dimensions_index`：维度清单（value_domain 为注册状态索引）。 */
export interface DimensionsItem {
  dataset: string;
  field: string;
  physical: string;
  is_time: boolean;
  synonyms: string[];
  /** "registered" | "skipped" | "none"（none = 无值域文件）。 */
  value_domain: string;
}

/** 端点 4 `_synonyms_payload`：locale 词典（patterns 为 YAML 原文结构，不编译）。 */
export interface SynonymsItem {
  locale: string;
  /** 值实测为 string[]（en_us 17 条 / zh_cn 空占位）。 */
  metric_synonyms: Record<string, string[]>;
  dimension_synonyms: Record<string, string[]>;
  patterns: Record<string, unknown>;
  empty_placeholder: boolean;
  authority_note: string | null;
}

/** 端点 5 `_values_index`：值域注册表清单（status 实测仅 registered|skipped）。 */
export interface ValuesItem {
  model: string | null;
  field: string | null;
  status: string | null;
  values_count: number;
  skip_reason: string | null;
  snapshot_sha: string | null;
  generated_at: string | null;
  bound_dataset: string | null;
  source_table: string | null;
  source_column: string | null;
}

/** 钻取 1 `_value_detail`：值域 JSON 原文（16 键；skipped 时 values: []）。 */
export interface ValueDetail {
  model: string;
  field: string;
  status: string;
  snapshot_sha: string;
  generated_at: string;
  bound_dataset: string;
  source_table: string;
  source_column: string;
  distinct_count: number;
  row_count: number;
  null_count: number;
  max_cardinality: number;
  skip_reason: string | null;
  values: { value: string; count: number }[];
  /** 别名 → 规范值（如 `{"nsdq": "NASDAQ"}`）。 */
  aliases: Record<string, string>;
  /** 派生说明（权威事实：派生自哪个快照、DISTINCT 实测数）。 */
  note: string;
}

/** 端点 6 `_policies_index` 的 roles[] 项（claims 契约 + 注册状态）。 */
export interface RoleItem {
  name: string;
  /** condition 模板原文（含 `{{ user.X }}` 占位符，不含渲染值）。 */
  condition: string | null;
  description: string | null;
  required_claims: string[];
  /** 需 sql_in 渲染的列表值键（如 categories）。 */
  list_claims: string[];
  /** false = 策略声明但未在 ROLE_DIRECTORY 注册，不可签发。 */
  registered: boolean;
}

/** 端点 6 `_policies_index`：行级策略（declared_by_models = 声明该策略的域）。 */
export interface PolicyItem {
  name: string | null;
  description: string | null;
  default_deny: boolean | null;
  declared_by_models: string[];
  roles: RoleItem[];
}

// ---------------------------------------------------------------------------
// 治理面（P3 落地：面板 4 的 reports / snapshots 两子页 + ReportDrawer）
// ---------------------------------------------------------------------------

/** 端点 7 `_reports_index`：报告索引。 */
export interface ReportsItem {
  /** 文件名 stem（主报告 = 纯 sha；钻取键同名）。 */
  name: string;
  /** 模式标签（sha 段已替换为 `<sha>`，如 `rls-verify-<sha>`）。 */
  pattern: string;
  /** 主报告才为 true（后端只解析主报告 body；非主报告仅文件元信息）。 */
  structured: boolean;
  size_bytes: number;
  /** 文件 mtime（ISO8601，+08:00）。 */
  mtime: string;
  /** 以下 4 键仅主报告解析（structured=true 时存在）——条件键，非 null 全集。 */
  sha?: string | null;
  created_at?: string | null;
  /** dry 透传（§3.3 第 3 行：dry 报告的 ex:"n/a" 不得被读成 EX=0）。 */
  dry?: boolean | null;
  domains?: string[] | null;
}

/**
 * 钻取 2 `/reports/{name}` 主报告 body（eval/runner.py 产出的 7 键；
 * sha 是**快照** sha，文件名才是代码 HEAD）。
 */
export interface ReportDetail {
  sha: string;
  created_at: string;
  dry: boolean;
  domains: string[];
  /** 按域分节（finance / retail 不混报，AGENTS.md N10），by_lang 内再分语言。 */
  summary: Record<string, unknown>;
  /** 逐样本结果（id / question / plan_ok / ex / hash …）。 */
  samples: Record<string, unknown>[];
  /** 口径说明原文（不截断）。 */
  notes: string;
  /** 契约外键（runner.py 演进时如实展示，不挑好看的键）。 */
  [key: string]: unknown;
}

/** 钻取 2 非主报告：`{name, pattern, structured: false, raw}`（降级展示形态）。 */
export interface ReportRawFallback {
  name: string;
  pattern: string;
  structured: false;
  /** 原始 JSON 全文（不摊平成表格列，不伪造统一表头）。 */
  raw: unknown;
}

export type ReportDetailPayload = ReportDetail | ReportRawFallback;

/** 端点 8 `_snapshots_index`：快照清单。 */
export interface SnapshotsItem {
  sha: string;
  created_at: string | null;
  source: string | null;
  data_range: unknown;
  raw_size_bytes: number | null;
  table_count: number;
  row_counts: {
    namespaces: Record<string, number>;
    total_tables: number;
    total_rows: number;
  };
  bound_to_head: boolean;
  /** 后端按 created_at 降序排序后标记的首条（字典序不可靠——0019 陷阱 6）。 */
  is_latest_by_created_at: boolean;
}

// ---------------------------------------------------------------------------
// ④a /analyze/stream SSE 事件载荷（ADR-0028 决策 ④·执行模型 A）
//
// 事件名字面量与 `serving/api.py` 的 AG-UI 词表常量逐字对齐（借鉴非兼容，N2：
// 不声称 AG-UI 兼容）。STATE_SNAPSHOT 直发 `/analyze` 同一份完整 TurnPayload
// （单一事实源）——见 api/analysis-stream.ts 的 reducer 终态断言。
// ---------------------------------------------------------------------------

/** SSE 事件名（镜像 serving/api.py:467-473 七个词表字面量）。 */
export type AnalysisStreamEventName =
  | "RUN_STARTED"
  | "STEP_STARTED"
  | "STEP_FINISHED"
  | "TOOL_CALL_RESULT"
  | "STATE_SNAPSHOT"
  | "RUN_FINISHED"
  | "RUN_ERROR";

/** 单帧 SSE：事件名 + 已解析的 JSON 载荷（未知键保留，不强收窄）。 */
export interface AnalysisStreamEvent {
  name: AnalysisStreamEventName;
  data: unknown;
}

/** RUN_STARTED：`{intent}`（plan None 时 intent 为 null）。 */
export interface RunStartedData {
  intent: string | null;
}

/** STEP_STARTED：`{role}`（角色名，取自 ANALYSIS_ROLES）。 */
export interface StepStartedData {
  role: string;
}

/** STEP_FINISHED：成功步收尾（status = step.kind，恒 "answer"）。 */
export interface StepFinishedData {
  role: string;
  status: string;
  latency_ms: number;
}

/**
 * TOOL_CALL_RESULT：成功步的已执行事实（镜像 serving/api.py:498-508）。
 * 被拒步不出此事件（N3：被拒 SQL 不出网）。
 */
export interface ToolCallResultData {
  role: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
}

/** RUN_FINISHED：`{status}` = analysis_status（ok|unavailable|blocked|error）。 */
export interface RunFinishedData {
  status: string;
}

/** RUN_ERROR：终态失败步（reason_code 镜像 _analysis_payload 失败步口径）。 */
export interface RunErrorData {
  reason_code: string;
  text: string;
}

// ---------------------------------------------------------------------------
// T06 运行面：事件、状态机与节点视图（ADR-0031 D07/D13）
//
// 逐字镜像 `serving/control/contracts.py`（EventType/RunStatus/ResultKind）与
// `serving/control/events.py`（NodeStatus）——图与时间线由同一事件流归约
// （state/run-events.ts 的 reducer），前端零重算（N1）。
// ---------------------------------------------------------------------------

/** 运行状态机：queued → running → 唯一终态（interrupted = 恢复封存，不自动重跑）。 */
export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "blocked"
  | "failed"
  | "interrupted";

/** 结果可用性投影（D07）：按此显示结果或「正文未保留/已过期/受限」。 */
export type ResultAvailability =
  | "pending"
  | "available"
  | "not_retained"
  | "expired"
  | "restricted";

/** 结果种类（内容不可用时的安全摘要保留项；摘要事件不能伪造答案）。 */
export type ResultKind = "answer" | "clarify" | "handoff" | "blocked" | "error";

/** 单一真实事件类型（D07③；镜像 contracts.py:EventType 13 个字面量）。 */
export type RunEventType =
  | "RUN_ACCEPTED"
  | "RUN_STARTED"
  | "NODE_STARTED"
  | "NODE_FINISHED"
  | "NODE_FAILED"
  | "NODE_SKIPPED"
  | "EDGE_TAKEN"
  | "TOOL_STARTED"
  | "TOOL_FINISHED"
  | "FALLBACK"
  | "STATE_SNAPSHOT"
  | "RUN_FINISHED"
  | "RUN_INTERRUPTED";

/**
 * 运行事件（固定 12 键；镜像 contracts.py:RunEventRecord）。
 *
 * seq 同 run 内事务递增（唯一序证据）；occurred_at 由服务端时钟生成
 * （前端不从 UI 时间推断执行）。payload 是脱敏摘要通道：问句/SQL/结果行/
 * Prompt 一律不进入（D07）。
 */
export interface RunEvent {
  schema_version: 1;
  run_id: string;
  seq: number;
  event_id: string;
  occurred_at: string;
  node_id: string | null;
  node_run_id: string | null;
  parent_node_run_id: string | null;
  attempt: number | null;
  event_type: RunEventType;
  release_id: string | null;
  payload: Record<string, unknown>;
}

/** 归约后的节点状态（镜像 events.py:NodeStatus；skipped 只来自显式事件）。 */
export type RunNodeStatus = "not_reached" | "running" | "finished" | "failed" | "skipped";

/** 运行模式（镜像 contracts.py:RunMode）。 */
export type RunMode = "ask" | "analyze" | "execute_plan";

/** 反馈结论（镜像 FeedbackVerdict）；状态在 T12 审核前恒 pending_review。 */
export type FeedbackVerdict = "up" | "down" | "corrected";
export type FeedbackStatus = "pending_review" | "approved" | "rejected";

/** 控制面稳定所有者（镜像 contracts.py:Owner；issuer+subject，非会话指纹）。 */
export interface ControlOwner {
  issuer: string;
  subject: string;
}

/** 分页信封（GET /runs、/sessions、/sessions/{id} 统一形状；next_cursor 不透明）。 */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

/** 运行列表行（固定 12 键；只含摘要，不含正文/问句/结果）。 */
export interface RunSummary {
  run_id: string;
  session_id: string;
  deployment_id: string;
  scope: string;
  mode: RunMode;
  status: RunStatus;
  result_availability: ResultAvailability;
  result_kind: ResultKind | null;
  replay_of: string | null;
  last_seq: number;
  created_at: string;
  updated_at: string;
}

/** 内容不可用时的安全摘要（D07）：保留结果种类枚举，不伪造答案。 */
export interface RunTraceSummary {
  result_kind: ResultKind | null;
}

/** GET /runs/{id} 固定 10 键视图；result 的裁剪由服务端 ACL 决定。 */
export interface RunView {
  run_id: string;
  session_id: string;
  release_id: string | null;
  status: RunStatus;
  result: Record<string, unknown> | null;
  result_availability: ResultAvailability;
  data_identity: Record<string, unknown> | null;
  replay_of: string | null;
  last_seq: number;
  trace_summary: RunTraceSummary;
}

/** 会话目录行（固定 6 键；控制库运行事实聚合，非 checkpoint dump）。 */
export interface SessionSummary {
  session_id: string;
  deployment_id: string;
  scope: string;
  run_count: number;
  last_run_at: string;
  last_status: RunStatus;
}

/** 捕获正文白名单字段（镜像 CaptureField）。 */
export type CaptureField = "question" | "node_io" | "result";

/** 显式捕获的私有制品；清理后 content/content_digest 为 null 且保留 cleaned_at。 */
export interface ArtifactRecord {
  artifact_id: string;
  run_id: string;
  kind: "capture";
  purpose: string;
  fields: CaptureField[];
  content: Record<string, unknown> | null;
  content_digest: string | null;
  retain_until: string;
  cleaned_at: string | null;
  created_at: string;
}

/** 反馈记录（T05d）；审核状态与训练资格由服务端固定，客户端无权指定。 */
export interface FeedbackRecord {
  feedback_id: string;
  run_id: string;
  owner: ControlOwner;
  verdict: FeedbackVerdict;
  comment: string | null;
  correction: Record<string, unknown> | null;
  status: FeedbackStatus;
  training_eligible: boolean;
  created_at: string;
}

/** POST /feedback 提交体（comment 可选的补充说明；审核字段不可指定）。 */
export interface FeedbackBody {
  run_id: string;
  verdict: FeedbackVerdict;
  comment?: string;
  correction?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// 数据源接入 / 探测 / 部署（T07；ADR-0031 D03/D04/D13；镜像 contracts.py）
// ---------------------------------------------------------------------------

/** 连接器种类（镜像 SourceConnectorKind；当前仅 Doris）。 */
export type SourceConnectorKind = "doris";

/** TLS 策略（镜像 TlsPolicy）：required 强校验；disabled 只用于内网/测试。 */
export type TlsPolicy = "required" | "disabled";

/** 探测状态（镜像 ProbeStatus）。 */
export type ProbeStatus = "ok" | "blocked";

/** 探测阻塞理由（9 类；镜像 ProbeBlockedReason；中文标签在向导组件内映射）。 */
export type ProbeBlockedReason =
  | "credential_missing"
  | "target_forbidden"
  | "target_not_allowlisted"
  | "tls_error"
  | "table_out_of_whitelist"
  | "read_only_unconfirmed"
  | "metadata_missing"
  | "credential_rejected"
  | "connect_failed";

/** 探测证据摘要（D03：有时间戳的证据，不是永久保证；不回显凭据）。 */
export interface SourceProbeSummary {
  probe_id: string;
  status: ProbeStatus;
  blocked_reason: ProbeBlockedReason | null;
  observed_at: string;
}

/** GET/POST /manage/sources 行（固定 13 键；引用名可回显，秘密与 DSN 永不回显）。 */
export interface SourceRevisionRecord {
  source_id: string;
  version: number;
  revision: string;
  connector_kind: SourceConnectorKind;
  secret_ref: string;
  allowed_catalogs: string[];
  allowed_tables: string[];
  timezone: string;
  tls_policy: TlsPolicy;
  query_budget: number;
  created_by: ControlOwner;
  created_at: string;
  last_probe: SourceProbeSummary | null;
}

/** POST /manage/sources 提交体（秘密只收 env:<NAME> 引用；越界表在合同层 422）。 */
export interface SourceRevisionBody {
  source_id: string;
  revision: string;
  connector_kind: SourceConnectorKind;
  secret_ref: string;
  allowed_catalogs: string[];
  allowed_tables: string[];
  timezone: string;
  tls_policy: TlsPolicy;
  query_budget: number;
}

/** 探测确认的能力（D03 证据化：只声明本次探测证实的能力）。 */
export interface ProbeCapabilities {
  dialect: string;
  read_only: boolean;
  metadata_probe: boolean;
  cancel_query: boolean;
  snapshot_read: boolean;
  consistent_analysis: boolean;
}

/** POST /manage/sources/{id}/probes 结果（固定 10 键；reproducible 恒 False）。 */
export interface ProbeResult {
  probe_id: string;
  source_id: string;
  version: number;
  status: ProbeStatus;
  blocked_reason: ProbeBlockedReason | null;
  observed_at: string;
  engine_version: string | null;
  schema_digest: string | null;
  capabilities: ProbeCapabilities;
  reproducible: false;
}

/** 部署指针行（固定 8 键；创建即 draft——首次发布前 active_release_id=null）。 */
export interface DeploymentRecord {
  deployment_id: string;
  scope: string;
  source_id: string;
  active_release_id: string | null;
  revision: number;
  created_by: ControlOwner;
  created_at: string;
  updated_at: string;
}

/** POST /manage/deployments 提交体（不得夹带 active_release_id——首次发布属 T08）。 */
export interface DeploymentBody {
  deployment_id: string;
  scope: string;
  source_id: string;
}

// ---------------------------------------------------------------------------
// 语义草稿 / 校验 / 审核 / 发布（T08；ADR-0031 D02/D04/D13；镜像 contracts.py）
// ---------------------------------------------------------------------------

/** 草稿种类（镜像 DraftKind；当前只有 semantic 具备确定性校验器）。 */
export type DraftKind = "semantic" | "flow" | "node_config";

/** 草稿生命周期（镜像 DraftStatus；修改内容返回 draft 并使旧验证/审核失效）。 */
export type DraftStatus =
  | "draft"
  | "validated"
  | "reviewed"
  | "source_imported"
  | "release_ready"
  | "published"
  | "retired";

/** 草稿内容（形状门：键恰为 {target, document}；target 命中制品白名单）。 */
export interface DraftContent {
  target: string;
  document: Record<string, unknown>;
}

/** 草稿行（固定 11 键；非权威副本，base_git_sha 由服务端取本地 HEAD）。 */
export interface DraftRecord {
  draft_id: string;
  kind: DraftKind;
  owner: ControlOwner;
  scope: string;
  base_git_sha: string;
  revision: number;
  status: DraftStatus;
  content: DraftContent;
  content_digest: string;
  created_at: string;
  updated_at: string;
}

/** POST /manage/drafts 提交体（创建不改变任何运行 Metric 与发布指针）。 */
export interface DraftBody {
  kind: DraftKind;
  scope: string;
  content: DraftContent;
}

/** PUT /manage/drafts/{id} 提交体（内容整体替换；并发由 If-Match 修订 ETag 控制）。 */
export interface DraftEditBody {
  content: DraftContent;
}

/** 校验发现归因（镜像 FindingCode；三套确定性校验器，不引入模糊结论）。 */
export type FindingCode = "structure" | "governance" | "policy";

/** 单条确定性校验发现：code 标明产生它的校验器，message 为校验器原文。 */
export interface ValidationFinding {
  code: FindingCode;
  message: string;
}

/** 校验证据行（固定 8 键；绑定 draft 的 revision 与内容摘要，可重复审计）。 */
export interface DraftValidationRecord {
  validation_id: string;
  draft_id: string;
  revision: number;
  content_digest: string;
  status: "passed" | "failed";
  findings: ValidationFinding[];
  actor: ControlOwner;
  created_at: string;
}

/** 审核决定（镜像 ReviewDecision）。 */
export type ReviewDecision = "approved" | "rejected";

/** POST /manage/drafts/{id}/reviews 提交体（决定绑定当前摘要）。 */
export interface DraftReviewBody {
  decision: ReviewDecision;
  comment?: string;
}

/** 人工审核证据行（固定 8 键；approved 的状态推进由服务层绑定摘要）。 */
export interface DraftReviewRecord {
  review_id: string;
  draft_id: string;
  revision: number;
  content_digest: string;
  decision: ReviewDecision;
  comment: string | null;
  actor: ControlOwner;
  created_at: string;
}

/** 补丁影响面摘要（固定 6 键；指标按全局 name、维度按 `dataset.field`）。 */
export interface PatchImpact {
  added_metrics: string[];
  removed_metrics: string[];
  changed_metrics: string[];
  added_dimensions: string[];
  removed_dimensions: string[];
  changed_dimensions: string[];
}

/** GET /manage/drafts/{id}/patch 视图（固定 7 键；最小统一 diff + 影响面）。 */
export interface DraftPatchView {
  draft_id: string;
  revision: number;
  content_digest: string;
  base_git_sha: string;
  target: string;
  patch: string;
  impact: PatchImpact;
}

/** POST /manage/releases/imports 提交体（显式 commit；不接受工作树/索引/引用）。 */
export interface ReleaseImportBody {
  draft_id: string;
  source_git_sha: string;
  source_id: string;
}

/** 导入响应（固定 7 键；制品身份 + 草稿推进后的状态 release_ready）。 */
export interface ReleaseImportRecord {
  release_id: string;
  content_digest: string;
  draft_id: string;
  draft_revision: number;
  status: "release_ready";
  target: string;
  created_at: string;
}

/** POST /manage/deployments/{id}/releases|rollbacks 提交体（D04 CAS）。 */
export interface ActivationBody {
  release_id: string;
  expected_active_release_id: string | null;
}

/** 激活视图（固定 6 键；action=publish|rollback；revision 是指针版本）。 */
export interface ActivationRecord {
  deployment_id: string;
  action: "publish" | "rollback";
  previous_release_id: string | null;
  active_release_id: string;
  revision: number;
  updated_at: string;
}

/** 发布登记行（固定 8 键，脱敏：只有 Manifest 与身份，无模型正文/凭据）。 */
export interface ReleaseRecord {
  release_id: string;
  content_digest: string;
  scope: string;
  source_id: string;
  source_revision: string;
  manifest: Record<string, unknown>;
  created_by: ControlOwner;
  created_at: string;
}
