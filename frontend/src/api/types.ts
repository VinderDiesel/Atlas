/**
 * HTTP 契约 → TS 类型（ADR-0022 契约 v2；设计页 §3.2/§3.4）。
 *
 * 纪律：
 * - `TurnPayload` 的 kind 无关字段是「字段全集稳定输出」（api.py `_turn_payload`
 *   docstring 原文）——一律 `| null` 而非 `?:`：服务端只会发 null，可选类型会让
 *   undefined 与 null 两种形态在组件里各写一遍分支（设计页 §3.4）；
 * - 键集与 `serving/api.py` 的 `_turn_payload()`（22 键）逐键对齐；键数由 Python
 *   侧契约测试（tests/test_api_contract_v2.py 等）锁定，本文件只做镜像；
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

/** `/api/v1/ask` 与 `/api/v1/plan/execute` 的响应（22 键，字段全集恒定）。 */
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
