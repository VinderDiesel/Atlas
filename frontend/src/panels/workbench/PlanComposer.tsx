/**
 * PlanComposer：结构化问数控件（T10f；ADR-0031 R04「指标/时间/筛选控件 +
 * 业务 Plan 卡片」）。
 *
 * 契约纪律（dev-plan T10 绿测「前端控件与计划卡片只发结构化合同」）：
 * - 控件构造 Plan 平铺 JSON（`composerPlanPayload`，6 键），经 `lib/plan-contract`
 *   的唯一构造点发 `/compile`（7 键）与 `/plan/execute`（8 键，含 session_id）——
 *   请求体**不附带自由文本**（question 由后端从 Plan 生成规范文本）；
 * - 选项取自治理面（指标 ← `/governance/metrics`；维度 ← `/governance/dimensions`，
 *   排除 is_time 列——时间走专用控件）；
 * - 校验先行（`composerErrors`）：指标必选、时间粒度与值配套、筛选行完整性在
 *   提交前拦住——不是「先发请求再让后端 422」的通道；
 * - 前端零解析：时间值与筛选值**原样字符串透传**，解析权在后端 compiler；
 * - FILTER_OPS 与 agent/compiler.py `_compare` 词表逐字一致（6 标量算子）；
 * - token 空串不发请求（认证前零 401 噪声；对齐 GovernanceLayout 的口径，但
 *   本组件挂载即发 2 条治理集合——懒挂载由调用方（折叠区块）承担）。
 */
import { Alert, Button, Input, Select, Space, Typography } from "antd";
import { useEffect, useState } from "react";

import { getJson, postJson } from "../../api/client";
import { API } from "../../api/endpoints";
import type {
  CompileResponse,
  DimensionsItem,
  Envelope,
  MetricsItem,
  ModelDomain,
  PlanPayload,
  TurnPayload,
} from "../../api/types";
import ErrorNote from "../../components/ErrorNote";
import { toCompileBody, toExecuteBody } from "../../lib/plan-contract";

const { Text } = Typography;

/** 过滤算子词表（agent/compiler.py `_compare` 的 6 标量算子，逐字对齐）。 */
export const FILTER_OPS = ["=", "!=", "<", "<=", ">", ">="] as const;

/** 时间粒度词表（compiler.TimeSpec 的 year / quarter / month / date）。 */
export const TIME_GRANULARITIES = ["year", "quarter", "month", "date"] as const;

/** 行数上限缺省（控件不提供编辑面；执行结果的行数上限仍由后端 Guard 复核）。 */
export const DEFAULT_LIMIT = 100;

/** 单行筛选的 UI 形态（value 恒为文本；原样透传，前端零解析）。 */
export interface ComposerFilterRow {
  column: string;
  op: string;
  value: string;
}

/** 控件表单（UI 形态；`composerPlanPayload` 转 Plan 平铺 JSON）。 */
export interface PlanComposerForm {
  metric: string;
  dimensions: string[];
  /** "" = 未声明时间（对应 Plan.time = null）。 */
  timeGranularity: string;
  timeValue: string;
  filters: ComposerFilterRow[];
}

const EMPTY_FORM: PlanComposerForm = {
  metric: "",
  dimensions: [],
  timeGranularity: "",
  timeValue: "",
  filters: [],
};

/** 全空行 = 用户没填的行；不构成合同字段，也不是校验错误。 */
function isBlankRow(row: ComposerFilterRow): boolean {
  return row.column.trim() === "" && row.op.trim() === "" && row.value.trim() === "";
}

/**
 * 控件表单 → Plan 平铺 JSON（键集精确 6 键，与 PlanPayload 逐字一致）。
 *
 * - time：粒度未声明 → null；value **原样字符串**（不做数字推断）；
 * - filters：过滤全空行；行内权重由 UI 态承载，不泄漏进合同；
 * - order_by 恒 []、limit 恒 DEFAULT_LIMIT（控件不提供编辑面）。
 */
export function composerPlanPayload(form: PlanComposerForm): PlanPayload {
  return {
    metric: form.metric.trim(),
    dimensions: [...form.dimensions],
    time:
      form.timeGranularity.trim() === ""
        ? null
        : { granularity: form.timeGranularity, value: form.timeValue },
    filters: form.filters
      .filter((row) => !isBlankRow(row))
      .map((row) => ({ column: row.column, op: row.op, value: row.value })),
    order_by: [],
    limit: DEFAULT_LIMIT,
  };
}

/**
 * 提交前校验（空数组 = 通过）；错误逐条可读，提交动作据此拦截。
 *
 * 时间与筛选的「配套」规则：粒度与值同填同空；筛选行要么全空（忽略）要么
 * 三段齐全——半填行不猜默认值。
 */
export function composerErrors(form: PlanComposerForm): string[] {
  const errors: string[] = [];
  if (form.metric.trim() === "") {
    errors.push("指标必选（选项取自治理面 /governance/metrics）");
  }
  const gran = form.timeGranularity.trim();
  const tval = form.timeValue.trim();
  if (gran !== "" && tval === "") {
    errors.push("已选时间粒度时必须填写时间值（如 2013 / 2013Q4，前端不解析）");
  }
  if (gran === "" && tval !== "") {
    errors.push("填写了时间值但未选时间粒度（时间粒度与值必须配套）");
  }
  form.filters.forEach((row, index) => {
    if (isBlankRow(row)) {
      return;
    }
    if (row.column.trim() === "" || row.op.trim() === "" || row.value.trim() === "") {
      errors.push(`第 ${index + 1} 行筛选不完整：字段 / 操作符 / 值需同时填写`);
    }
  });
  return errors;
}

type CollectionState<T> =
  | { status: "loading" }
  | { status: "ok"; items: T[] }
  | { status: "error"; error: unknown };

/** 单条治理集合装载（token 空串不发请求；path/token 变化即重拉）。 */
function useOptions<T>(path: string, token: string): CollectionState<T> {
  const [state, setState] = useState<CollectionState<T>>({ status: "loading" });
  useEffect(() => {
    if (token.trim() === "") {
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    getJson<Envelope<string, T>>(path, token)
      .then((envelope) => {
        if (!cancelled) {
          setState({ status: "ok", items: envelope.items });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "error", error });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [path, token]);
  return state;
}

interface Props {
  /** Bearer token（App 内存态）；空串 = 未认证（区块替换为引导，不发请求）。 */
  token: string;
  /** 当前域（App 单源；与顶栏角色清单、治理页域选择器同一事实源）。 */
  model: ModelDomain;
  /** 当前会话 id（App 持有；执行回合经 onExecuted 回报轮次）。 */
  sessionId: string;
  /** 编译成功（SQL 原文 → 工作台的 SqlPreview）。 */
  onCompiled: (sql: string) => void;
  /** 执行成功（完整 TurnPayload → 工作台渲染回合 + 会话轮次回报）。 */
  onExecuted: (payload: TurnPayload) => void;
}

export default function PlanComposer({
  token,
  model,
  sessionId,
  onCompiled,
  onExecuted,
}: Props) {
  const authed = token.trim() !== "";
  const metricPath = `${API.governanceMetrics}?model=${encodeURIComponent(model)}`;
  const dimensionPath = `${API.governanceDimensions}?model=${encodeURIComponent(model)}`;
  const metrics = useOptions<MetricsItem>(metricPath, token);
  const dimensions = useOptions<DimensionsItem>(dimensionPath, token);
  const [form, setForm] = useState<PlanComposerForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState<"compile" | "execute" | null>(null);
  const [error, setError] = useState<unknown>(null);

  const metricOptions =
    metrics.status === "ok" ? metrics.items.map((item) => ({ value: item.name, label: item.name })) : [];
  const dimensionOptions =
    dimensions.status === "ok"
      ? dimensions.items
          .filter((item) => !item.is_time)
          .map((item) => ({ value: item.field, label: `${item.field}（${item.dataset}）` }))
      : [];

  const updateFilter = (index: number, patch: Partial<ComposerFilterRow>): void => {
    setForm((prev) => ({
      ...prev,
      filters: prev.filters.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    }));
  };

  const submit = (kind: "compile" | "execute"): void => {
    const found = composerErrors(form);
    setErrors(found);
    if (found.length > 0) {
      return; // 校验未过不发送（不是先发请求再让后端 422）
    }
    void (async () => {
      setBusy(kind);
      setError(null);
      try {
        const plan = composerPlanPayload(form);
        if (kind === "compile") {
          const resp = await postJson<CompileResponse>(
            API.compile,
            toCompileBody(plan, model),
            token,
          );
          onCompiled(resp.sql);
        } else {
          const resp = await postJson<TurnPayload>(
            API.planExecute,
            toExecuteBody(plan, model, sessionId),
            token,
          );
          onExecuted(resp);
        }
      } catch (e: unknown) {
        setError(e);
      } finally {
        setBusy(null);
      }
    })();
  };

  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      {!authed ? (
        <Alert
          type="warning"
          showIcon
          message="请先登录：选项数据需要认证后才能加载"
          description="未激活身份时本区块不发任何请求。"
        />
      ) : (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Text type="secondary">
            选项取自治理面（指标 ← /governance/metrics；维度 ← /governance/dimensions，排除时间列）；
            请求体只含结构化合同（Plan 6 键 + model），不附带自由文本。
          </Text>
          {metrics.status === "error" && <ErrorNote error={metrics.error} title="指标选项加载失败" />}
          {dimensions.status === "error" && (
            <ErrorNote error={dimensions.error} title="维度选项加载失败" />
          )}
          <Space wrap align="center" size={8}>
            <Select
              value={form.metric === "" ? undefined : form.metric}
              onChange={(value: string) => setForm((prev) => ({ ...prev, metric: value }))}
              options={metricOptions}
              placeholder="指标（必选）"
              style={{ width: 220 }}
              showSearch
            />
            <Select<string[]>
              mode="multiple"
              value={form.dimensions}
              onChange={(value) => setForm((prev) => ({ ...prev, dimensions: value }))}
              options={dimensionOptions}
              placeholder="维度（可多选，排除时间列）"
              style={{ minWidth: 280 }}
              showSearch
            />
          </Space>
          <Space wrap align="center" size={8}>
            <Select
              value={form.timeGranularity === "" ? undefined : form.timeGranularity}
              onChange={(value: string | undefined) =>
                setForm((prev) => ({ ...prev, timeGranularity: value ?? "" }))
              }
              options={TIME_GRANULARITIES.map((value) => ({ value, label: value }))}
              placeholder="时间粒度（可空）"
              style={{ width: 150 }}
              allowClear
            />
            <Input
              value={form.timeValue}
              onChange={(e) => setForm((prev) => ({ ...prev, timeValue: e.target.value }))}
              placeholder="时间值（如 2013 / 2013Q4；原样透传，前端不解析）"
              style={{ width: 280 }}
            />
          </Space>
          {form.filters.map((row, index) => (
            <Space key={index} wrap align="center" size={8}>
              <Select
                value={row.column === "" ? undefined : row.column}
                onChange={(value: string) =>
                  updateFilter(index, { column: value })
                }
                options={dimensionOptions}
                placeholder="筛选字段"
                style={{ width: 200 }}
                showSearch
              />
              <Select
                value={row.op === "" ? undefined : row.op}
                onChange={(value: string) => updateFilter(index, { op: value })}
                options={FILTER_OPS.map((value) => ({ value, label: value }))}
                placeholder="操作符"
                style={{ width: 96 }}
              />
              <Input
                value={row.value}
                onChange={(e) => updateFilter(index, { value: e.target.value })}
                placeholder="值（原样透传）"
                style={{ width: 200 }}
              />
              <Button
                size="small"
                onClick={() =>
                  setForm((prev) => ({
                    ...prev,
                    filters: prev.filters.filter((_, i) => i !== index),
                  }))
                }
              >
                删除
              </Button>
            </Space>
          ))}
          <Button
            size="small"
            onClick={() =>
              setForm((prev) => ({
                ...prev,
                filters: [...prev.filters, { column: "", op: "", value: "" }],
              }))
            }
          >
            添加筛选
          </Button>
          {errors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="表单未通过校验（不发送请求）"
              description={
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {errors.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              }
            />
          )}
          {error !== null && <ErrorNote error={error} />}
          <Space wrap size={8}>
            <Button loading={busy === "compile"} disabled={busy !== null} onClick={() => submit("compile")}>
              生成 SQL
            </Button>
            <Button
              type="primary"
              loading={busy === "execute"}
              disabled={busy !== null}
              onClick={() => submit("execute")}
            >执行</Button>
          </Space>
        </Space>
      )}
    </Space>
  );
}
