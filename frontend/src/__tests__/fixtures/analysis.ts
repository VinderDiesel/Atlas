/**
 * 归因三态 fixture（ADR-0026 决策⑥ `AnalysisPayload` 形态）——仅供 B1 渲染测试驱动。
 *
 * ⚠ **数字是占位，不是评测结果**（AGENTS.md N1/§9.3）：本文件只驱动 `AnalysisBlock`
 * 三态渲染断言，**不得**被任何报告/README/对外文案引用为真实数字。身份字段
 * （metric/dimension/period/intent/sha）忠实取自金标 `eval/analysis/finance/attribution-001.json`
 * （"分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"），数值列只是内部自洽的十进制串。
 *
 * 形态对齐 `serving/api.py::_analysis_payload`（`_jsonable` 后 totals/items 数值一律字符串出网）；
 * steps 角色序取自 `agent/analysis.py::ANALYSIS_ROLES`（四步固定模板）。
 */
import type { AnalysisPayload } from "../../api/types";

/** ok：两期总量 + 按 Branch 的贡献项（|Δ| 降序）+ 四步全成功。 */
export const analysisOk: AnalysisPayload = {
  schema_version: 1,
  intent: "change_contribution",
  status: "ok",
  metric: "commission_revenue",
  dimension: "Branch",
  baseline: { granularity: "quarter", value: "2013Q3" },
  current: { granularity: "quarter", value: "2013Q4" },
  filters: [],
  snapshot_sha: "7c966e9",
  semantic_sha256:
    "a3ba5e2b708bcf578dc27b3587ca8050bef6ae0a4e2cc77290ffda586e345be4",
  recipe_version: 1,
  // 内部自洽：基线 A600+B400=1000，本期 A800+B500=1300，Δ300；贡献 66.67/33.33
  totals: { baseline: "1000.00", current: "1300.00", delta: "300.00" },
  items: [
    { value: "A", baseline: "600.00", current: "800.00", delta: "200.00", contribution_pct: "66.67" },
    { value: "B", baseline: "400.00", current: "500.00", delta: "100.00", contribution_pct: "33.33" },
  ],
  steps: [
    {
      role: "baseline_total",
      kind: "answer",
      sql: "SELECT SUM(ft.Commission) AS commission_revenue FROM atlas.dwd.fact_trades AS ft JOIN atlas.dwd.dim_date AS d ON ft.SK_CreateDateID = d.SK_DateID WHERE d.CalendarQtrID = 20133 LIMIT 1",
      columns: ["commission_revenue"],
      rows: [["1000.00"]],
      latency_ms: 12,
    },
    {
      role: "current_total",
      kind: "answer",
      sql: "SELECT SUM(ft.Commission) AS commission_revenue FROM atlas.dwd.fact_trades AS ft JOIN atlas.dwd.dim_date AS d ON ft.SK_CreateDateID = d.SK_DateID WHERE d.CalendarQtrID = 20134 LIMIT 1",
      columns: ["commission_revenue"],
      rows: [["1300.00"]],
      latency_ms: 9,
    },
    {
      role: "current_by_dimension",
      kind: "answer",
      sql: "SELECT b.Branch AS Branch, SUM(ft.Commission) AS commission_revenue FROM atlas.dwd.fact_trades AS ft JOIN atlas.dwd.dim_broker AS b ON ft.SK_BrokerID = b.SK_BrokerID JOIN atlas.dwd.dim_date AS d ON ft.SK_CreateDateID = d.SK_DateID WHERE d.CalendarQtrID = 20134 GROUP BY b.Branch LIMIT 100",
      columns: ["Branch", "commission_revenue"],
      rows: [
        ["A", "800.00"],
        ["B", "500.00"],
      ],
      latency_ms: 15,
    },
    {
      role: "baseline_by_dimension",
      kind: "answer",
      sql: "SELECT b.Branch AS Branch, SUM(ft.Commission) AS commission_revenue FROM atlas.dwd.fact_trades AS ft JOIN atlas.dwd.dim_broker AS b ON ft.SK_BrokerID = b.SK_BrokerID JOIN atlas.dwd.dim_date AS d ON ft.SK_CreateDateID = d.SK_DateID WHERE d.CalendarQtrID = 20133 GROUP BY b.Branch LIMIT 100",
      columns: ["Branch", "commission_revenue"],
      rows: [
        ["A", "600.00"],
        ["B", "400.00"],
      ],
      latency_ms: 14,
    },
  ],
  reason_code: null,
  text: null,
  elapsed_ms: 50,
};

/** unavailable：plan 在、四步已执行，但两期总量差为 0（决策⑤）→ totals null、items 空、带后端 text。 */
export const analysisUnavailable: AnalysisPayload = {
  ...analysisOk,
  status: "unavailable",
  totals: null,
  items: [],
  reason_code: "zero_total_delta",
  text: "两期总量无变化，贡献占比无定义（不推断分解）。",
};

/** blocked：current_total 子步被只读网关拒绝 → 该步裁为安全摘要（sql null），分析终止。 */
export const analysisBlocked: AnalysisPayload = {
  ...analysisOk,
  status: "blocked",
  totals: null,
  items: [],
  reason_code: null,
  text: "某分析步骤被只读网关拒绝，未能完成贡献分解。",
  steps: [
    analysisOk.steps[0],
    {
      role: "current_total",
      kind: "blocked",
      sql: null,
      columns: [],
      rows: [],
      latency_ms: 3,
      reason_code: "guard_blocked",
    },
  ],
};
