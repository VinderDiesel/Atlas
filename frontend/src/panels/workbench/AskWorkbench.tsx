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
 * P2 受控化（本批次）：token / model / session_id 上收到 App（域是全局单源；会话
 * 由 App 在切角色时轮换——§3.5 约束 1）。本组件不再自持 model 与 sessionId；
 * 每个成功回合经 onTurnSeen 回报 session_id 与 turns_in_session（App 记录到会话
 * 日志供 SessionPanel 展示）。token 空串时动作按钮禁用并指引顶栏角色切换器。
 * 零遥测口径（0018 落地注记 P1 批次）：错误原文如实渲染，吞错即违约。
 */
import { Alert, Button, Input, Select, Space, Typography } from "antd";
import { Fragment, useState, type ReactNode } from "react";

import { postJson } from "../../api/client";
import { API } from "../../api/endpoints";
import type {
  CompileResponse,
  ModelDomain,
  PlanPayload,
  PlanResponse,
  TurnPayload,
} from "../../api/types";
import ErrorNote from "../../components/ErrorNote";
import { truncationState } from "../../lib/honesty";
import { ANSWER_SECTIONS, branchOf, type AnswerSection } from "../../lib/order";
import { shortSha } from "../../lib/sha";

import ChartBlock from "../chart/ChartBlock";

import DataTable from "./DataTable";
import ExplanationBlock from "./ExplanationBlock";
import PlanPreview from "./PlanPreview";
import SqlPreview from "./SqlPreview";
import TruncationNote from "./TruncationNote";

type Busy = "ask" | "plan" | "compile" | "execute";

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
      const { sql } = await postJson<CompileResponse>(API.compile, { ...plan, model }, token);
      setCompileSql(sql);
    });
  };

  const executePlan = (plan: PlanPayload): void => {
    void run("execute", async () => {
      const q = question.trim();
      // question 缺省由后端取 Plan 规范化文本；空串不能发（min_length=1）
      const body =
        q === "" ? { ...plan, model, session_id: sessionId } : { ...plan, model, session_id: sessionId, question: q };
      const resp = await postJson<TurnPayload>(API.planExecute, body, token);
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
      sql: <SqlPreview sql={p.sql} title="出口 SQL（Guard 校验后，只读）" />,
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
    <Space
      direction="vertical"
      size="middle"
      style={{ width: "100%", maxWidth: 1080, margin: "0 auto", display: "flex" }}
    >
      <Input.TextArea
        placeholder="用中文或英文问一句，例如：2013 年第二季度总交易额"
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        autoSize={{ minRows: 2, maxRows: 6 }}
      />
      <Space wrap>
        <Select<ModelDomain>
          value={model}
          onChange={onModelChange}
          style={{ width: 160 }}
          options={[
            { value: "finance", label: "finance（金融）" },
            { value: "retail", label: "retail（零售）" },
          ]}
        />
        <Button type="primary" loading={busy === "ask"} disabled={!canRun} onClick={() => ask(question)}>
          问一句
        </Button>
        <Button loading={busy === "plan"} disabled={!canRun} onClick={showPlan}>
          只看计划
        </Button>
        <Button loading={busy === "compile"} disabled={!canRun} onClick={compileOnly}>
          只编译（不执行）
        </Button>
        {token.trim() === "" && (
          <Typography.Text type="secondary">
            未激活身份：请用顶栏「角色」切换器激活（首次 make token 签发后粘贴；token
            只存内存，刷新后需重粘贴）
          </Typography.Text>
        )}
      </Space>

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

      {compileSql !== null && <SqlPreview sql={compileSql} title="编译 SQL（只编译不执行）" />}

      {payload !== null && renderTurn(payload)}
    </Space>
  );
}
