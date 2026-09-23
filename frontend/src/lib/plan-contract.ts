/**
 * 结构化 Plan 合同构造（T10f；ADR-0031 R04「指标/时间/筛选控件 + 业务 Plan
 * 卡片」；dev-plan T10「前端控件与计划卡片只发结构化合同」）。
 *
 * 唯一构造点：控件与计划卡片的 `/compile`、`/plan/execute` 请求体一律经本
 * 模块构造——只含 Plan 结构化字段（6 键）+ model（执行另加 session_id），
 * **不附带任何自由文本**。`/plan/execute` 的 question 缺省由后端从 Plan 生成
 * 规范文本（serving/api.py `_plan_text`），解析不读 question。
 *
 * 纪律：本模块不 import React / fetch（纯函数，vitest 直测）。
 */
import type {
  CompileBody,
  ModelDomain,
  PlanExecuteBody,
  PlanPayload,
} from "../api/types";

/** Plan 6 键 + model（CompileBody；`/api/v1/compile` 的请求体）。 */
export function toCompileBody(plan: PlanPayload, model: ModelDomain): CompileBody {
  return {
    metric: plan.metric,
    dimensions: plan.dimensions,
    time: plan.time,
    filters: plan.filters,
    order_by: plan.order_by,
    limit: plan.limit,
    model,
  };
}

/**
 * 执行体：CompileBody + session_id（8 键；`/api/v1/plan/execute` 的请求体）。
 * 恒不含 question——执行请求只发结构化合同。
 */
export function toExecuteBody(
  plan: PlanPayload,
  model: ModelDomain,
  sessionId: string,
): PlanExecuteBody {
  return {
    ...toCompileBody(plan, model),
    session_id: sessionId,
  };
}
