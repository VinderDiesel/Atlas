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
 * 日志）。token 空串时动作按钮禁用并指引顶栏角色切换器。
 * 零遥测口径（0018 落地注记 P1 批次）：错误原文如实渲染，吞错即违约。
 */
import { DeleteOutlined, AuditOutlined } from "@ant-design/icons";
import { Alert, Button, Collapse, Input, Popconfirm, Segmented, Select, Space, Tag, Typography } from "antd";
import { Fragment, useEffect, useState, type ReactNode } from "react";

import { ApiError, postJson } from "../../api/client";
import { getRun, listDeployments, submitRun } from "../../api/control";
import { API } from "../../api/endpoints";
import { streamRunEvents } from "../../api/run-stream";
import {
  initialStreamState,
  reduceAnalysisEvents,
  streamAnalysis,
  type AnalysisStreamState,
} from "../../api/analysis-stream";
import type {
  CompileResponse,
  DeploymentRecord,
  ModelDomain,
  PlanPayload,
  PlanResponse,
  RunReceipt,
  TurnPayload,
} from "../../api/types";
import DeterminismBadge from "../../components/DeterminismBadge";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import ProvenanceSpine from "../../components/ProvenanceSpine";
import Section from "../../components/Section";
import SnapshotSeal from "../../components/SnapshotSeal";
import { truncationState } from "../../lib/honesty";
import { ANSWER_SECTIONS, branchOf, type AnswerSection } from "../../lib/order";
import { toCompileBody, toExecuteBody } from "../../lib/plan-contract";
import { shortSha } from "../../lib/sha";
import { emptyRunState, reduceRunEvent, type RunViewState } from "../../state/run-events";
import { entryFromPayload, formatHistoryTime, toTurnPayload, type QueryHistoryEntry } from "../../state/query-history";

import ChartBlock from "../chart/ChartBlock";
import { NodeInspector } from "../runs/NodeInspector";
import RunGraph from "../runs/RunGraph";
import { RUN_GRAPH_SPEC } from "../runs/run-view-model";

import AnalysisBlock from "./AnalysisBlock";
import DataTable from "./DataTable";
import ExplanationBlock from "./ExplanationBlock";
import NarrativeBlock from "./NarrativeBlock";
import PlanComposer from "./PlanComposer";
import PlanPreview from "./PlanPreview";
import SqlPreview from "./SqlPreview";
import TruncationNote from "./TruncationNote";

type Busy = "ask" | "plan" | "compile" | "execute" | "analyze" | "runs";

type WorkMode = "query" | "analyze";

/** 执行通道：ask=同步轻量（默认）；runs=异步审计（POST /runs → 轮询结果）。 */
type ExecutionChannel = "ask" | "runs";

/** 终态状态（run 已结束，无需继续轮询）。 */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "succeeded", "blocked", "failed", "interrupted",
]);

/** 轮询间隔 ms（runs 提交后等待结果）。 */
const POLL_INTERVAL_MS = 500;

/** 轮询上限（防止无限等待）。 */
const MAX_POLL_ATTEMPTS = 120;

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
  /** 查询历史（App 持有，localStorage 持久化；历史区展示用）。 */
  queryHistory: readonly QueryHistoryEntry[];
  /** 记录一条查询历史（每次成功回合调用；App 负责持久化）。 */
  onRecordHistory: (entry: QueryHistoryEntry) => void;
  /** 删除单条历史（历史区 × 按钮调用）。 */
  onDeleteHistory: (id: string) => void;
  /** 清空全部历史（历史区「清空」按钮调用）。 */
  onClearHistory: () => void;
}

export default function AskWorkbench({
  token,
  model,
  onModelChange,
  sessionId,
  onTurnSeen,
  queryHistory,
  onRecordHistory,
  onDeleteHistory,
  onClearHistory,
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
  /** 选中查看的历史条目 id（点击历史条目后展示存储结果，不重新执行）。 */
  const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null);
  /** 执行通道：ask=同步轻量（默认）；runs=异步审计（POST /runs → 轮询结果）。 */
  const [execChannel, setExecChannel] = useState<ExecutionChannel>("ask");
  /** 审计运行模式的部署选择（runs 通道用）。 */
  const [deploymentId, setDeploymentId] = useState<string | null>(null);
  /** 可用部署列表（runs 通道时按需加载）。 */
  const [deployments, setDeployments] = useState<DeploymentRecord[]>([]);
  /** 认证错误（401）。 */
  const [authError, setAuthError] = useState<string | null>(null);
  /** 审计运行内嵌视图（Phase 1 可视化：执行路径图 + 节点详情）。 */
  const [runViewState, setRunViewState] = useState<RunViewState | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // 审计运行通道：加载可用部署（token 就绪时）
  useEffect(() => {
    if (execChannel !== "runs" || token === "") {
      setDeployments([]);
      return;
    }
    let cancelled = false;
    setAuthError(null);
    listDeployments(token)
      .then((items) => {
        if (!cancelled) {
          setDeployments(items);
          // 自动选中第一个可用部署
          if (items.length > 0 && deploymentId === null) {
            setDeploymentId(items[0].deployment_id);
          }
        }
      })
      .catch((e) => {
        if (!cancelled) {
          if (e instanceof ApiError && e.status === 401) {
            setAuthError("认证已过期或无效，请重新登录");
          }
          setDeployments([]);
        }
      });
    return () => { cancelled = true; };
  }, [execChannel, token]);

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
    if (execChannel === "runs") {
      askViaRuns(text);
      return;
    }
    void run("ask", async () => {
      const resp = await postJson<TurnPayload>(
        API.ask,
        { question: text, model, session_id: sessionId },
        token,
      );
      setPayload(resp);
      onTurnSeen(resp.session_id, resp.turns_in_session);
      onRecordHistory(entryFromPayload(text, model, resp));
    });
  };

  /**
   * 审计运行通道：POST /runs → 轮询 GET /runs/{id} 至终态 → 提取结果。
   *
   * 与 /ask 路径不同：异步执行、结果在控制库、有完整事件流与审计留痕。
   * 轮询间隔 500ms、上限 120 次（60s），超时即报错（不无限等待）。
   */
  const askViaRuns = (text: string): void => {
    if (deploymentId === null) {
      setError(new Error("请先选择部署目标"));
      return;
    }
    // 重置内嵌运行视图
    setRunViewState(null);
    setSelectedNodeId(null);
    void run("runs", async () => {
      const receipt: RunReceipt = await submitRun(
        {
          deployment_id: deploymentId,
          mode: "ask",
          client_request_id: crypto.randomUUID(),
          question: text,
          session_id: sessionId,
        },
        token,
      );
      // 启动事件流归约（Phase 1 可视化：实时构建执行路径图）
      const initial = emptyRunState([
        "plan", "retrieve", "generate", "validate", "execute", "explain", "clarify", "handoff",
      ]);
      setRunViewState(initial);
      const streamAbort = new AbortController();
      const streamPromise = (async () => {
        try {
          for await (const event of streamRunEvents(receipt.run_id, token, { signal: streamAbort.signal })) {
            setRunViewState((prev) => (prev ? reduceRunEvent(prev, event) : prev));
          }
        } catch {
          // 流中断不影响主链路（轮询仍在继续）
        }
      })();
      // 轮询至终态
      for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        const view = await getRun(receipt.run_id, token);
        if (TERMINAL_STATUSES.has(view.status)) {
          streamAbort.abort();
          await streamPromise;
          if (view.status === "succeeded" && view.result !== null) {
            const turnPayload = view.result as unknown as TurnPayload;
            setPayload(turnPayload);
            onTurnSeen(turnPayload.session_id, turnPayload.turns_in_session);
            onRecordHistory(entryFromPayload(text, model, turnPayload, "runs", receipt.run_id));
          } else {
            setError(new Error(`运行 ${view.status}（run_id=${view.run_id}）`));
          }
          return;
        }
      }
      streamAbort.abort();
      throw new Error(`运行超时（${MAX_POLL_ATTEMPTS * POLL_INTERVAL_MS / 1000}s）：run_id=${receipt.run_id}`);
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
        onRecordHistory(entryFromPayload(text, model, resp));
        return;
      }
      setPayload(final);
      onTurnSeen(final.session_id, final.turns_in_session);
      onRecordHistory(entryFromPayload(text, model, final));
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
      onRecordHistory(entryFromPayload(`[执行计划] ${plan.metric}`, model, resp));
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
      sql: (
        <div style={{ position: "relative" }}>
          <SqlPreview sql={p.sql} title="出口 SQL" />
          <div style={{ position: "absolute", top: 8, right: 8 }}>
            <DeterminismBadge path="compiler" />
          </div>
        </div>
      ),
      data: <DataTable columns={p.columns} rows={p.rows} renderedRows={t.renderedRows} />,
      truncation: <TruncationNote state={t} />,
      // ⑥ 图表（ADR-0025 决策 ②）：chart === null 时 ChartBlock 渲染 null，
      // 不占位不推断原因（设计页 §3.4 ⑥）
      chart: <ChartBlock spec={p.chart} />,
    };
    return (
      <div style={{ position: "relative" }}>
        <ProvenanceSpine>
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {ANSWER_SECTIONS.map((section) => (
              <div key={section} className="atlas-spine__node">
                {sections[section]}
              </div>
            ))}
            {/* ⑦ 接地叙述（ADR-0029 ⑤）：条件键——narrative 缺失（off/未请求）时渲染 null，
                off 路径不占位、不改变既有 DOM（逐字向后兼容）。 */}
            <div className="atlas-spine__node">
              <NarrativeBlock narrative={p.narrative} />
            </div>
          </Space>
        </ProvenanceSpine>
        <div style={{ position: "absolute", top: 0, right: 0 }}>
          <SnapshotSeal
            snapshotSha={p.snapshot_sha}
            boundToHead={p.snapshot_bound_to_head ?? false}
          />
        </div>
      </div>
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

  const canRun = question.trim() !== "" && token.trim() !== "" && busy === null
    && (execChannel !== "runs" || deploymentId !== null);

  return (
    <div className="atlas-page">
      {PAGE_HEADER}

      {authError !== null && (
        <Alert
          type="error"
          message="认证失败"
          description={
            <div>
              <p>{authError}</p>
              <Button
                type="link"
                size="small"
                onClick={() => {
                  setAuthError(null);
                  // 重新触发部署加载
                  if (execChannel === "runs" && token) {
                    setDeployments([]);
                    listDeployments(token)
                      .then((items) => {
                        setDeployments(items);
                        if (items.length > 0 && deploymentId === null) {
                          setDeploymentId(items[0].deployment_id);
                        }
                      })
                      .catch(() => setDeployments([]));
                  }
                }}
              >
                重新加载
              </Button>
            </div>
          }
          showIcon
          closable
          onClose={() => setAuthError(null)}
          style={{ marginBottom: 16 }}
        />
      )}
  
      <Section title="提问">
        {/* 工具栏：模式 + 域 + 执行通道 */}
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
            style={{ width: 120 }}
            options={[
              { value: "finance", label: "金融" },
              { value: "retail", label: "零售" },
            ]}
          />
          <Segmented<ExecutionChannel>
            value={execChannel}
            onChange={(value) => setExecChannel(value)}
            options={[
              { value: "ask", label: "直接查询" },
              { value: "runs", label: "审计运行" },
            ]}
          />
          {execChannel === "runs" && (
            <Select<string>
              value={deploymentId ?? undefined}
              onChange={(value) => setDeploymentId(value)}
              placeholder="选择部署"
              style={{ width: 160 }}
              loading={deployments.length === 0 && token !== ""}
              options={deployments.map((d) => ({
                value: d.deployment_id,
                label: `${d.deployment_id}（${d.scope}）`,
              }))}
              notFoundContent="无可用部署"
            />
          )}
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
                  loading={busy === "ask" || busy === "runs"}
                  disabled={!canRun || (execChannel === "runs" && deploymentId === null)}
                  onClick={() => ask(question)}
                >
                  {execChannel === "runs" ? "审计运行" : "查询"}
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
            提示：归因需指定绝对时间区间（如“2013Q4 相对 2013Q3”），系统不会猜测默认区间。
          </Typography.Text>
        )}
        {execChannel === "runs" && mode === "query" && (
          <Typography.Text type="secondary" style={{ display: "block", marginTop: 12, fontSize: 13 }}>
            审计运行：查询经控制库持久化，有完整事件流与审计留痕。需先在「数据」页创建部署。
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
                    onRecordHistory(entryFromPayload("[结构化问数]", model, resp));
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

      {/* Phase 1 可视化：审计运行内嵌执行路径图 + 节点详情 */}
      {runViewState !== null && execChannel === "runs" && (
        <Section
          title="执行路径"
          meta={
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              点击节点查看输入/输出详情
            </Typography.Text>
          }
        >
          <RunGraph
            state={runViewState}
            selectedNodeId={selectedNodeId}
            onNodeClick={(nodeId) => setSelectedNodeId((prev) => (prev === nodeId ? null : nodeId))}
          />
          {selectedNodeId && runViewState.nodes[selectedNodeId] && (
            <div style={{ marginTop: 16 }}>
              <Typography.Text strong style={{ fontSize: 13 }}>
                节点详情：{selectedNodeId}
              </Typography.Text>
              <NodeInspector
                node={runViewState.nodes[selectedNodeId]}
                toolEvents={runViewState.timeline.filter(
                  (e) =>
                    (e.event_type === "TOOL_STARTED" || e.event_type === "TOOL_FINISHED") &&
                    e.node_id === selectedNodeId,
                )}
                routingDecisions={runViewState.routingDecisions}
                templateEdges={RUN_GRAPH_SPEC.templateEdges}
              />
            </div>
          )}
        </Section>
      )}

      {/* 查询历史（localStorage 持久化；ADR-0028 裁定 B 的客户端补位） */}
      {queryHistory.length > 0 && (
        <Section
          title="查询历史"
          meta={
            <Space size={8}>
              <span>{queryHistory.length} 条 · 刷新不丢失</span>
              <Popconfirm
                title="清空全部查询历史？"
                description="清空后不可恢复"
                onConfirm={() => {
                  onClearHistory();
                  setSelectedHistoryId(null);
                }}
                okText="清空"
                cancelText="取消"
              >
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                >
                  清空
                </Button>
              </Popconfirm>
            </Space>
          }
        >
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            {[...queryHistory].reverse().map((entry) => {
              const isSelected = selectedHistoryId === entry.id;
              return (
                <div
                  key={entry.id}
                  style={{
                    padding: "8px 12px",
                    borderRadius: 6,
                    border: isSelected
                      ? "1px solid var(--ant-color-primary, #1677ff)"
                      : "1px solid rgba(255,255,255,0.08)",
                    background: isSelected
                      ? "rgba(22,119,255,0.06)"
                      : "rgba(255,255,255,0.02)",
                    cursor: "pointer",
                    position: "relative",
                  }}
                  onClick={() => {
                    setSelectedHistoryId(entry.id);
                    setPayload(toTurnPayload(entry));
                  }}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedHistoryId(entry.id);
                      setPayload(toTurnPayload(entry));
                    }
                  }}
                >
                  {/* 删除按钮（阻止冒泡，避免触发条目点击） */}
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    style={{ position: "absolute", top: 4, right: 4 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (isSelected) {
                        setSelectedHistoryId(null);
                        setPayload(null);
                      }
                      onDeleteHistory(entry.id);
                    }}
                  />
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", paddingRight: 28 }}>
                    <Space size={6}>
                      <Typography.Text strong style={{ fontSize: 13 }}>
                        {entry.question}
                      </Typography.Text>
                      {entry.channel === "runs" && (
                        <Tag icon={<AuditOutlined />} color="blue" style={{ marginRight: 0, fontSize: 11 }}>
                          审计{entry.run_id !== null ? ` ${entry.run_id.slice(0, 8)}` : ""}
                        </Tag>
                      )}
                    </Space>
                    <Typography.Text type="secondary" style={{ fontSize: 11, whiteSpace: "nowrap", marginLeft: 8 }}>
                      {formatHistoryTime(entry.timestamp)}
                    </Typography.Text>
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {entry.kind === "answer"
                        ? `${entry.metric ?? "（无指标）"} · ${entry.row_count} 行 · ${entry.latency_ms}ms`
                        : entry.kind === "clarify"
                          ? "需要澄清"
                          : entry.kind === "blocked"
                            ? "被拒绝"
                            : entry.kind === "error"
                              ? "执行错误"
                              : "转人工"}
                      {entry.snapshot_sha !== null ? ` · 快照 ${shortSha(entry.snapshot_sha)}` : ""}
                    </Typography.Text>
                  </div>
                </div>
              );
            })}
          </Space>
        </Section>
      )}
    </div>
  );
}
