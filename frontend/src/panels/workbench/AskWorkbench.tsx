/**
 * 面板 1：问数工作台（AskWorkbench）——P1 核心组件。
 *
 * 渲染次序（决策 ⑦；0018 判据 9）由 lib/order.ts 的 ANSWER_SECTIONS 驱动：
 * ① 指标口径 → ② 行数/耗时 → ③ 出口 SQL → ④ 数据 → ⑤ 截断声明 → ⑥ 图表
 * （⑥ 消费独立 `chart` 键，ADR-0025 决策 ②）——组件不硬写次序，改序先改
 * lib 的断言。四态（五 kind）分支由 branchOf(kind) 判定：
 * 未知 kind 展示原始 payload（不猜分支）。
 *
 * 三个动作（设计页 §3.1 的消费面）：
 * - 问一句：POST /ask（主路径；session_id 挂载时生成并复用 → 多轮续接）；
 * - 只看计划：POST /plan → PlanPreview（含「执行此计划」→ POST /plan/execute）；
 * - 只编译（不执行）：取 Plan 产物 → POST /compile → SqlPreview。
 *
 * T10f 结构化合同（ADR-0031 R04；dev-plan T10「前端控件与计划卡片只发结构化
 * 合同」）：查询模式提供「结构化问数」折叠区块（PlanComposer，可选懒挂载——
 * 展开才发治理面 2 条选项请求）；本组件与计划卡片的 /compile、/plan/execute
 * 请求体一律经 `lib/plan-contract` 构造——6 键 + model（执行另加
 * session_id），恒不含 question（缺省由后端从 Plan 生成规范文本）。
 *
 * P2 受控化（本批次）：token / model / session_id 上收到 App（域是全局单源；会话
 * 由 App 在切角色时轮换——§3.5 约束 1）。本组件不再自持 model 与 sessionId；
 * 每个成功回合经 onTurnSeen 回报 session_id 与 turns_in_session（App 记录到会话
 * 日志供 SessionPanel 展示）。token 空串时动作按钮禁用并指引顶栏角色切换器。
 * 零遥测口径（0018 落地注记 P1 批次）：错误原文如实渲染，吞错即违约。
 */
import { Alert, Button, Collapse, Input, Segmented, Select, Space, Typography } from "antd";
import { Fragment, useState, type ReactNode } from "react";

import { postJson } from "../../api/client";
import { API } from "../../api/endpoints";
import {
  initialStreamState,
  reduceAnalysisEvents,
  streamAnalysis,
  type AnalysisStreamState,
} from "../../api/analysis-stream";
import type {
  CompileResponse,
  ModelDomain,
  PlanPayload,
  PlanResponse,
  TurnPayload,
} from "../../api/types";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import Section from "../../components/Section";
import { truncationState } from "../../lib/honesty";
import { ANSWER_SECTIONS, branchOf, type AnswerSection } from "../../lib/order";
import { toCompileBody, toExecuteBody } from "../../lib/plan-contract";
import { shortSha } from "../../lib/sha";

import ChartBlock from "../chart/ChartBlock";

import AnalysisBlock from "./AnalysisBlock";
import DataTable from "./DataTable";
import ExplanationBlock from "./ExplanationBlock";
import NarrativeBlock from "./NarrativeBlock";
import PlanComposer from "./PlanComposer";
import PlanPreview from "./PlanPreview";
import SqlPreview from "./SqlPreview";
import TruncationNote from "./TruncationNote";

type Busy = "ask" | "plan" | "compile" | "execute" | "analyze";

type WorkMode = "query" | "analyze";

const PAGE_HEADER = (
  <PageHeader
    title="问数工作台"
    description="用自然语言提问，自动生成只读 SQL 并返回数据与图表。"
  />
);

interface Props {
  /** Bearer token（App 内存态）；空串时动作按钮禁用并指引顶栏角色切换器。 */
  token: string;
  /** 当前域（App 单源；与顶栏角色清单、治理页域选择器同一事实源）。 */
  model: ModelDomain;
  onModelChange: (model: ModelDomain) => void;
  /** 当前会话 id（App 持有；切角色即轮换——§3.5 约束 1）。 */
  sessionId: string;
  /** 成功回合回报（App 记录会话日志；轮数取 max 由 state/session.ts 承担）。 */
  onTurnSeen: (sessionId: string, turnsInSession: number) => void;
}

export default function AskWorkbench({
  token,
  model,
  onModelChange,
  sessionId,
  onTurnSeen,
}: Props) {
  const [question, setQuestion] = useState("");
  const [payload, setPayload] = useState<TurnPayload | null>(null);
  const [planResp, setPlanResp] = useState<PlanResponse | null>(null);
  const [compileSql, setCompileSql] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy | null>(null);
  const [error, setError] = useState<unknown>(null);
  /** 工作模式（决策 B）：query=现有单轮问数；analyze=消费 /analyze 多步归因。默认查询，不改既有行为。 */
  const [mode, setMode] = useState<WorkMode>("query");
  /** ④a 流式消费的中间态（仅 analyze 模式；终态以 payload 为准，单源逐字一致）。 */
  const [streamState, setStreamState] = useState<AnalysisStreamState>(initialStreamState());

  async function run(action: Busy, fn: () => Promise<void>): Promise<void> {
    setBusy(action);
    setError(null);
    try {
      await fn();
    } catch (e: unknown) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  const ask = (text: string): void => {
    void run("ask", async () => {
      const resp = await postJson<TurnPayload>(
        API.ask,
        { question: text, model, session_id: sessionId },
        token,
      );
      setPayload(resp);
      onTurnSeen(resp.session_id, resp.turns_in_session);
    });
  };

  /**
   * 多步归因（④a）：优先走流式 `/analyze/stream` 渐进消费；终态 STATE_SNAPSHOT
   * 即与 request/response 逐字同源的完整 TurnPayload（单一事实源）。流式故障或
   * 无分析意图（无 STATE_SNAPSHOT）时回落 `/analyze`（request/response，ADR-0026
   * 决策⑥），渲染义务不变：错误原文经 run 的 catch → ErrorNote 如实展示。
   */
  const analyze = (text: string): void => {
    void run("analyze", async () => {
      const body = { question: text, model, session_id: sessionId };
      const events: Parameters<typeof reduceAnalysisEvents>[0] = [];
      setStreamState(initialStreamState());
      let final: TurnPayload | null = null;
      try {
        for await (const evt of streamAnalysis(body, token)) {
          events.push(evt);
          setStreamState(reduceAnalysisEvents(events));
        }
        final = reduceAnalysisEvents(events).final;
      } catch {
        final = null; // 流式失败：下方统一回落 request/response
      }
      if (final === null) {
        const resp = await postJson<TurnPayload>(API.analyze, body, token);
        setPayload(resp);
        onTurnSeen(resp.session_id, resp.turns_in_session);
        return;
      }
      setPayload(final);
      onTurnSeen(final.session_id, final.turns_in_session);
    });
  };

  const showPlan = (): void => {
    void run("plan", async () => {
      const resp = await postJson<PlanResponse>(API.plan, { question, model }, token);
      setPlanResp(resp);
    });
  };

  const compileOnly = (): void => {
    void run("compile", async () => {
      // 复用已有 Plan；没有则先取 /plan（同一个 plan 对象 → /compile 请求体同构）
      let plan = planResp?.kind === "plan" ? planResp.plan : null;
      if (plan === null) {
        const resp = await postJson<PlanResponse>(API.plan, { question, model }, token);
        setPlanResp(resp);
        if (resp.kind !== "plan" || resp.plan === null) {
          return; // clarify：无编译输入，PlanPreview 已展示澄清
        }
        plan = resp.plan;
      }
      const { sql } = await postJson<CompileResponse>(
        API.compile,
        toCompileBody(plan, model),
        token,
      );
      setCompileSql(sql);
    });
  };

  /**
   * 计划卡片执行（T10f）：只发结构化合同（Plan 6 键 + model + session_id，
   * 恒不含 question——缺省由后端从 Plan 生成规范文本；`lib/plan-contract`
   * 为唯一构造点）。
   */
  const executePlan = (plan: PlanPayload): void => {
    void run("execute", async () => {
      const resp = await postJson<TurnPayload>(
        API.planExecute,
        toExecuteBody(plan, model, sessionId),
        token,
      );
      setPayload(resp);
      onTurnSeen(resp.session_id, resp.turns_in_session);
    });
  };

  const pickCandidate = (candidate: string): void => {
    setQuestion(candidate);
    ask(candidate);
  };

  function renderAnswer(p: TurnPayload): ReactNode {
    const t = truncationState({ rowCount: p.row_count, sql: p.sql });
    const sections: Record<AnswerSection, ReactNode> = {
      metric: <ExplanationBlock metric={p.metric} explanation={p.explanation} />,
      counts: (
        <Space direction="vertical" size={0}>
          <Typography.Text>
            指标={p.metric ?? "（无）"} | 行数={p.row_count} | 执行 {p.latency_ms}ms
          </Typography.Text>
          <Typography.Text type="secondary">
            会话 {p.session_id.slice(0, 8)} · 第 {p.turns_in_session} 轮
            {p.snapshot_sha !== null
              ? ` · 本轮绑定快照 ${shortSha(p.snapshot_sha)}（bound_to_head=${String(
                  p.snapshot_bound_to_head,
                )}）`
              : " · 本轮未回显快照（snapshot_sha 为 null）"}
          </Typography.Text>
        </Space>
      ),
      sql: <SqlPreview sql={p.sql} title="出口 SQL" />,
      data: <DataTable columns={p.columns} rows={p.rows} renderedRows={t.renderedRows} />,
      truncation: <TruncationNote state={t} />,
      // ⑥ 图表（ADR-0025 决策 ②）：chart === null 时 ChartBlock 渲染 null，
      // 不占位不推断原因（设计页 §3.4 ⑥）
      chart: <ChartBlock spec={p.chart} />,
    };
    return (
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {ANSWER_SECTIONS.map((section) => (
          <Fragment key={section}>{sections[section]}</Fragment>
        ))}
        {/* ⑦ 接地叙述（ADR-0029 ⑤）：条件键——narrative 缺失（off/未请求）时渲染 null，
            off 路径不占位、不改变既有 DOM（逐字向后兼容）。 */}
        <NarrativeBlock narrative={p.narrative} />
      </Space>
    );
  }

  function renderTurn(p: TurnPayload): ReactNode {
    const spec = branchOf(p.kind);
    if (spec === null) {
      return (
        <Alert
          type="warning"
          showIcon
          message={`未知回合类型：${p.kind}（契约外值，原样展示）`}
          description={
            <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(p, null, 2)}</pre>
          }
        />
      );
    }
    switch (p.kind) {
      case "answer":
        return renderAnswer(p);
      case "clarify":
        return (
          <Alert
            type="warning"
            showIcon
            message="需要澄清（Planner 不猜）"
            description={
              <Space direction="vertical">
                {(p.clarification?.reasons ?? ["（无澄清详情）"]).map((reason, i) => (
                  <span key={`${i}-${reason}`}>{reason}</span>
                ))}
                {p.clarification !== null && p.clarification.candidates.length > 0 && (
                  <Space wrap>
                    {p.clarification.candidates.map((candidate) => (
                      <Button key={candidate} size="small" onClick={() => pickCandidate(candidate)}>
                        {candidate}
                      </Button>
                    ))}
                  </Space>
                )}
              </Space>
            }
          />
        );
      case "blocked":
        return (
          <Alert
            type="warning"
            showIcon
            message="查询被只读网关拒绝（blocked）"
            description={
              <Space direction="vertical">
                <span>{p.block_reason ?? "（无拒绝原因）"}</span>
                {p.validation_issues.length > 0 && (
                  <ul style={{ margin: 0, paddingLeft: 20 }}>
                    {p.validation_issues.map((issue) => (
                      <li key={issue}>{issue}</li>
                    ))}
                  </ul>
                )}
              </Space>
            }
          />
        );
      case "error":
        return (
          <Alert
            type="error"
            showIcon
            message="执行期故障（error）"
            description={<span>{p.error ?? "（无错误详情）"}</span>}
          />
        );
      case "handoff":
        return (
          <Alert
            type="info"
            showIcon
            message="转人工接管（handoff）"
            description={
              <Space direction="vertical">
                <span>{p.handoff_reason ?? "（无交接原因）"}</span>
                <span>path={p.path ?? "null"}</span>
              </Space>
            }
          />
        );
    }
  }

  const canRun = question.trim() !== "" && token.trim() !== "" && busy === null;

  return (
    <div className="atlas-page">
      {PAGE_HEADER}
  
      <Section title="提问">
        {/* 工具栏：模式 + 域选择 */}
        <Space wrap align="center" size={12} style={{ marginBottom: 12 }}>
          <Segmented<WorkMode>
            value={mode}
            onChange={(value) => setMode(value)}
            options={[
              { value: "query", label: "查询" },
              { value: "analyze", label: "分析" },
            ]}
          />
          <Select<ModelDomain>
            value={model}
            onChange={onModelChange}
            style={{ width: 160 }}
            options={[
              { value: "finance", label: "金融" },
              { value: "retail", label: "零售" },
            ]}
          />
        </Space>
  
        <Input.TextArea
          name="question"
          autoComplete="off"
          spellCheck={false}
          aria-label="问数输入框"
          placeholder={mode === "query" ? "用自然语言提问，例如：2013 年第二季度总交易额" : "输入归因问题，例如：2013Q4 佣金收入相对 2013Q3 的变化"}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          autoSize={{ minRows: 2, maxRows: 6 }}
          onPressEnter={(e) => {
            if (e.shiftKey) return; // Shift+Enter 换行
            e.preventDefault();
            if (canRun) {
              mode === "query" ? ask(question) : analyze(question);
            }
          }}
        />
  
        {/* 操作按钮行 */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
          {token.trim() === "" && (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              请先在右上角选择角色开始使用
            </Typography.Text>
          )}
          <Space size={8}>
            {mode === "query" ? (
              <>
                <Button
                  type="primary"
                  loading={busy === "ask"}
                  disabled={!canRun}
                  onClick={() => ask(question)}
                >
                  查询
                </Button>
                <Button loading={busy === "plan"} disabled={!canRun} onClick={showPlan}>
                  预览计划
                </Button>
                <Button loading={busy === "compile"} disabled={!canRun} onClick={compileOnly}>
                  生成 SQL
                </Button>
              </>
            ) : (
              <Button
                type="primary"
                loading={busy === "analyze"}
                disabled={!canRun}
                onClick={() => analyze(question)}
              >
                归因分析
              </Button>
            )}
          </Space>
        </div>
  
        {mode === "analyze" && (
          <Typography.Text type="secondary" style={{ display: "block", marginTop: 12, fontSize: 13 }}>
            提示：归因需指定绝对时间区间（如"2013Q4 相对 2013Q3"），系统不会猜测默认区间。
          </Typography.Text>
        )}
      </Section>
  
      {mode === "query" && (
        <Collapse
          items={[
            {
              key: "composer",
              label: "高级选项：手动指定指标与筛选条件",
              children: (
                <PlanComposer
                  token={token}
                  model={model}
                  sessionId={sessionId}
                  onCompiled={(sql) => setCompileSql(sql)}
                  onExecuted={(resp) => {
                    setPayload(resp);
                    onTurnSeen(resp.session_id, resp.turns_in_session);
                  }}
                />
              ),
            },
          ]}
        />
      )}

      {/* ④a 渐进消费：流式期间按事件到达序展示步骤进度（字段全取自后端事件，
          前端零重算；终态到达后由 payload→AnalysisBlock 接管，本块随 busy 清空） */}
      {mode === "analyze" && busy === "analyze" && streamState.steps.length > 0 && (
        <Space direction="vertical" size={0} style={{ width: "100%" }}>
          <Typography.Text type="secondary">
            分析进行中{streamState.intent !== null ? `（意图 ${streamState.intent}）` : ""}：
          </Typography.Text>
          {streamState.steps.map((step) => (
            <Typography.Text key={step.role}>
              {step.role}：
              {step.phase === "finished"
                ? `完成（${step.latency_ms}ms）`
                : step.phase === "result"
                  ? `返回 ${step.row_count} 行`
                  : "执行中…"}
            </Typography.Text>
          ))}
        </Space>
      )}

      {error !== null && <ErrorNote error={error} />}

      {planResp !== null && (
        <PlanPreview
          kind={planResp.kind}
          plan={planResp.plan}
          clarification={planResp.clarification}
          executing={busy === "execute"}
          onExecute={executePlan}
          onPickCandidate={pickCandidate}
        />
      )}

      {compileSql !== null && <SqlPreview sql={compileSql} title="生成的 SQL" />}

      {/* WIG：答案区动态内容通知（aria-live=polite，屏幕阅读器可读） */}
      <div aria-live="polite">
        {payload !== null &&
          (payload.analysis !== null ? (
            <AnalysisBlock analysis={payload.analysis} />
          ) : (
            renderTurn(payload)
          ))}
      </div>
    </div>
  );
}
