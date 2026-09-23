/**
 * T10f 结构化问数控件与结构化合同断言（`panels/workbench/PlanComposer.tsx` +
 * `lib/plan-contract.ts`；ADR-0031 R04；dev-plan T10 绿测「前端控件与计划卡片
 * 只发结构化合同」的机器证据）。
 *
 * 断言面（诚实性优先）：
 * - composerPlanPayload 键集精确 6 键（PlanPayload 平铺 JSON）——控件内部态
 *   （timeGranularity/timeValue 的行形态）不泄漏进合同；时间未声明为 null；
 *   时间值**原样字符串透传**（前端零解析，解析权在后端 compiler）；全空筛选行
 *   不构成合同字段；
 * - composerErrors：指标必选、时间粒度与值配套、筛选行完整性在提交前拦住——
 *   不是「先发请求再让后端 422」的通道；
 * - toCompileBody / toExecuteBody 是唯一构造点：execute 体 8 键且**不含
 *   question**（question 由后端从 Plan 生成规范文本；T10f 用户口径）；
 * - FILTER_OPS 与 agent/compiler.py `_compare` 词表逐字一致（6 标量算子）；
 * - SSR 初始态：未认证提示（token 空串不发请求）；
 * - 静态断言：AskWorkbench 的执行体经 toExecuteBody 构造，无内联 question
 *   拼装残留（防回归；模式仿 governance-readonly.test.ts）。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { toCompileBody, toExecuteBody } from "../lib/plan-contract";
import AskWorkbench from "../panels/workbench/AskWorkbench";
import PlanComposer, {
  DEFAULT_LIMIT,
  FILTER_OPS,
  TIME_GRANULARITIES,
  composerErrors,
  composerPlanPayload,
  type PlanComposerForm,
} from "../panels/workbench/PlanComposer";

const FORM: PlanComposerForm = {
  metric: "gmv",
  dimensions: ["trades.region"],
  timeGranularity: "quarter",
  timeValue: "2013Q4",
  filters: [{ column: "trades.region", op: "=", value: "华东" }],
};

describe("composerPlanPayload（控件表单 → Plan 平铺 JSON）", () => {
  it("键集精确 6 键且逐键构造；order_by/limit 为固定形态", () => {
    const plan = composerPlanPayload(FORM);
    expect(Object.keys(plan).sort()).toEqual([
      "dimensions",
      "filters",
      "limit",
      "metric",
      "order_by",
      "time",
    ]);
    expect(plan.metric).toBe("gmv");
    expect(plan.dimensions).toEqual(["trades.region"]);
    expect(plan.time).toEqual({ granularity: "quarter", value: "2013Q4" });
    expect(plan.filters).toEqual([{ column: "trades.region", op: "=", value: "华东" }]);
    expect(plan.order_by).toEqual([]);
    expect(plan.limit).toBe(DEFAULT_LIMIT);
  });

  it("时间未声明 → null；值原样字符串（前端零解析，不做数字推断）", () => {
    expect(composerPlanPayload({ ...FORM, timeGranularity: "", timeValue: "" }).time).toBeNull();
    const raw = composerPlanPayload({ ...FORM, timeValue: "2013" });
    expect(typeof raw.time?.value).toBe("string");
    expect(raw.time?.value).toBe("2013");
  });

  it("全空筛选行过滤（未填的行不是合同字段）；其余行原样保留", () => {
    const plan = composerPlanPayload({
      ...FORM,
      filters: [{ column: "", op: "", value: "" }, ...FORM.filters],
    });
    expect(plan.filters).toEqual([{ column: "trades.region", op: "=", value: "华东" }]);
  });
});

describe("composerErrors（提交前校验；不是后端 422 的通道）", () => {
  it("合法表单：零错误（含无时间、含全空筛选行两种合法形态）", () => {
    expect(composerErrors(FORM)).toEqual([]);
    expect(composerErrors({ ...FORM, timeGranularity: "", timeValue: "" })).toEqual([]);
    expect(composerErrors({ ...FORM, filters: [{ column: "", op: "", value: "" }] })).toEqual([]);
  });

  it("指标未选 → 拒；时间粒度与值必须配套 → 拒", () => {
    expect(composerErrors({ ...FORM, metric: "" }).length).toBeGreaterThan(0);
    expect(composerErrors({ ...FORM, timeValue: "" }).length).toBeGreaterThan(0);
    expect(composerErrors({ ...FORM, timeGranularity: "", timeValue: "2013" }).length).toBeGreaterThan(0);
  });

  it("筛选行部分填写（不完整）→ 拒", () => {
    expect(
      composerErrors({ ...FORM, filters: [{ column: "trades.region", op: "", value: "" }] }).length,
    ).toBeGreaterThan(0);
    expect(
      composerErrors({ ...FORM, filters: [{ column: "", op: "=", value: "" }] }).length,
    ).toBeGreaterThan(0);
    expect(
      composerErrors({ ...FORM, filters: [{ column: "trades.region", op: "=", value: "" }] }).length,
    ).toBeGreaterThan(0);
  });
});

describe("词表常量（与后端 compiler 逐字对齐）", () => {
  it("FILTER_OPS == agent/compiler.py `_compare` 的 6 个标量算子", () => {
    expect(FILTER_OPS).toEqual(["=", "!=", "<", "<=", ">", ">="]);
  });

  it("TIME_GRANULARITIES == compiler.TimeSpec 粒度词表", () => {
    expect(TIME_GRANULARITIES).toEqual(["year", "quarter", "month", "date"]);
  });
});

describe("plan-contract（唯一请求体构造点）", () => {
  it("toCompileBody：Plan 6 键 + model（7 键）", () => {
    const plan = composerPlanPayload(FORM);
    const body = toCompileBody(plan, "finance");
    expect(Object.keys(body).sort()).toEqual([
      "dimensions",
      "filters",
      "limit",
      "metric",
      "model",
      "order_by",
      "time",
    ]);
    expect(body.model).toBe("finance");
    expect(body.metric).toBe("gmv");
  });

  it("toExecuteBody：只多 session_id（8 键）且不含 question", () => {
    const plan = composerPlanPayload(FORM);
    const body = toExecuteBody(plan, "retail", "sess-1");
    expect(Object.keys(body).sort()).toEqual([
      "dimensions",
      "filters",
      "limit",
      "metric",
      "model",
      "order_by",
      "session_id",
      "time",
    ]);
    expect("question" in body).toBe(false);
    expect(body.session_id).toBe("sess-1");
    expect(body.model).toBe("retail");
  });
});

describe("PlanComposer（SSR 初始态）", () => {
  it("未认证：认证提示 + 不进控件（token 空串不发请求）", () => {
    const html = renderToString(
      <PlanComposer token="" model="finance" sessionId="s-1" onCompiled={() => {}} onExecuted={() => {}} />,
    );
    expect(html).toContain("请先登录");
  });

  it("已认证（装载中）：控件骨架与两个动作按钮就绪", () => {
    const html = renderToString(
      <PlanComposer token="t" model="finance" sessionId="s-1" onCompiled={() => {}} onExecuted={() => {}} />,
    );
    expect(html).toContain("生成 SQL");
    // AntD Button autoInsertSpace: 两个中文字符间自动插入空格
    expect(html).toContain("执");
    expect(html).toContain("行");
  });
});

describe("AskWorkbench 集成（高级选项区块）", () => {
  it("查询模式下渲染高级选项入口（计划卡片执行同一结构化合同）", () => {
    const html = renderToString(
      <AskWorkbench
        token=""
        model="finance"
        onModelChange={() => {}}
        sessionId="s-1"
        onTurnSeen={() => {}}
      />,
    );
    expect(html).toContain("高级选项");
  });
});

describe("静态断言：执行体只发结构化合同（防回归）", () => {
  const WORKBENCH_SOURCES = import.meta.glob("../panels/workbench/**/*.{ts,tsx}", {
    eager: true,
    query: "?raw",
    import: "default",
  }) as Record<string, string>;

  it("AskWorkbench 执行体经 toExecuteBody 构造；无内联 question 拼装残留", () => {
    const ask = WORKBENCH_SOURCES["../panels/workbench/AskWorkbench.tsx"];
    expect(ask).toBeDefined();
    expect(ask).toContain("toExecuteBody(plan, model, sessionId)");
    expect(ask).not.toContain("question: q");
  });
});
