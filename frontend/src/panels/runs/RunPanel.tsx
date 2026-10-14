/**
 * 运行历史面板（T06d；ADR-0031 D13 控制面消费面）。
 *
 * 三段式：目录（GET /runs，游标分页）→ 详情（GET /runs/{id} + SSE 事件流）
 * → 反馈（POST /feedback 提交 + GET /feedback 列表找回，审核页留 T12）。
 *
 * 纪律：
 * - **事件流是运行事实的唯一来源**：图与时间线由 `streamRunEvents` 帧经
 *   `reduceRunEvent` 归约（断线续读/去重/终态停止在 api/run-stream.ts 与
 *   state/run-events.ts 承担；本组件只管按选中 run 重连与卸载中止）；
 * - **不伪造答案**：结果区由 `availabilityNotice` 裁决——非 available 只给
 *   提示；available 而服务端未返回正文时给兜底文案（裁剪/未保留不渲染「没有结果」）；
 * - **捕获正文显式钻取**：artifact 引用只从事件流 STATE_SNAPSHOT 提取，
 *   点击才请求 `GET /runs/{id}/artifacts/{aid}`（403/404/410 原文透传）；
 * - **token 空串不发请求**（App 顶栏认证前不产生 401 噪声；认证提示如实）。
 */
import {
  Alert,
  Button,
  Input,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useEffect, useState } from "react";

import {
  deleteRun,
  getRun,
  getRunArtifact,
  listFeedback,
  listRuns,
  submitFeedback,
} from "../../api/control";
import { streamRunEvents } from "../../api/run-stream";
import type {
  ArtifactRecord,
  FeedbackRecord,
  FeedbackVerdict,
  RunEvent,
  RunStatus,
  RunSummary,
  RunView,
} from "../../api/types";
import EmptyState from "../../components/EmptyState";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import RunStatusHeader from "../../components/RunStatusHeader";
import Section from "../../components/Section";
import { truncationState } from "../../lib/honesty";
import { emptyRunState, reduceRunEvent, type RunViewState } from "../../state/run-events";
import DataTable from "../workbench/DataTable";

import RunGraph from "./RunGraph";
import RunTimeline from "./RunTimeline";
import { NodeInspector } from "./NodeInspector";
import {
  RUN_GRAPH_SPEC,
  availabilityNotice,
  collectArtifactIds,
  statusTag,
} from "./run-view-model";

const { Text } = Typography;

const NODE_IDS = RUN_GRAPH_SPEC.nodes.map((node) => node.id);
const PAGE_SIZE = 20;

const PAGE_HEADER = (
  <PageHeader
    title="运行历史"
    description="查看每次查询的执行详情和结果。"
  />
);

/** 捕获正文钻取态（每 artifact 独立；错误原文透传）。 */
export type ArtifactView =
  | { status: "loading" }
  | { status: "loaded"; record: ArtifactRecord }
  | { status: "error"; error: unknown };

const VERDICT_OPTIONS: Array<{ value: FeedbackVerdict; label: string }> = [
  { value: "up", label: "有用" },
  { value: "down", label: "无用" },
  { value: "corrected", label: "已纠正" },
];

// -- result 窄化（Record<string, unknown> 只读取类型正确的字段；宁缺不猜） ----

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? (value as string[])
    : [];
}

function rowsOf(value: unknown): unknown[][] {
  return Array.isArray(value) && value.every((row) => Array.isArray(row))
    ? (value as unknown[][])
    : [];
}

// -- 结果区（唯一裁决点：availabilityNotice） --------------------------------

function ResultBlock({ detail }: { detail: RunView }) {
  const notice = availabilityNotice(detail.result_availability, detail.result !== null);
  if (notice !== null) {
    // 非 available（或 available 无正文）：只给提示，不渲染任何结果内容
    return <Alert type="info" showIcon message={notice} />;
  }
  const result = detail.result as Record<string, unknown>;
  const kind = str(result["kind"]);
  const question = str(result["question"]);
  const sql = str(result["sql"]);
  const rowCount = num(result["row_count"]);
  const columns = strArray(result["columns"]);
  const rows = rowsOf(result["rows"]);
  const truncation = truncationState({ rowCount: rowCount ?? 0, sql });
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space size="small" wrap>
        {kind !== null && <Tag color="green">{kind}</Tag>}
        {question !== null && <Text>{`问句：${question}`}</Text>}
        {rowCount !== null && <Text type="secondary">{`返回 ${rowCount} 行`}</Text>}
      </Space>
      {sql !== null && <pre className="atlas-pre">{sql}</pre>}
      {columns.length > 0 && (
        <DataTable columns={columns} rows={rows} renderedRows={truncation.renderedRows} />
      )}
      {truncation.renderNote !== null && (
        <Alert type="warning" showIcon message={truncation.renderNote} />
      )}
      {truncation.limitNote !== null && (
        <Alert type="info" showIcon message={truncation.limitNote} />
      )}
    </Space>
  );
}

// -- 详情段（数据驱动；导出供 SSR 冒烟直喂 props） ---------------------------

interface RunDetailSectionProps {
  detail: RunView;
  runState: RunViewState;
  token: string;
  artifactViews: Record<string, ArtifactView>;
  onDrillArtifact: (artifactId: string) => void;
}

export function RunDetailSection({
  detail,
  runState,
  token,
  artifactViews,
  onDrillArtifact,
}: RunDetailSectionProps) {
  const artifactIds = collectArtifactIds(runState);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // 选中节点的子工具调用事件
  const nodeToolEvents = selectedNodeId
    ? runState.timeline.filter(
        (e) =>
          (e.event_type === "TOOL_STARTED" || e.event_type === "TOOL_FINISHED") &&
          e.node_id === selectedNodeId,
      )
    : [];

  // 选中节点视图
  const selectedNode = selectedNodeId ? runState.nodes[selectedNodeId] ?? null : null;

  const handleNodeClick = (nodeId: string) => {
    setSelectedNodeId((prev) => (prev === nodeId ? null : nodeId));
  };

  // 捕获正文块（显式授权；超期不可再读）
  const artifactsBlock =
    artifactIds.length === 0 ? (
      <Text type="secondary">本次运行未捕获正文（请求未包含 capture 授权）。</Text>
    ) : (
      artifactIds.map((artifactId) => {
        const view = artifactViews[artifactId];
        return (
          <div key={artifactId}>
            <Space size="small">
              <Text code>{artifactId}</Text>
              <Button
                size="small"
                disabled={token === ""}
                onClick={() => onDrillArtifact(artifactId)}
              >
                查看捕获正文
              </Button>
            </Space>
            {view?.status === "loading" && <Text type="secondary">读取中…</Text>}
            {view?.status === "error" && (
              <ErrorNote error={view.error} title="捕获正文读取失败" />
            )}
            {view?.status === "loaded" && (
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {`保留至 ${view.record.retain_until}；字段：${view.record.fields.join(", ")}`}
                </Text>
                <pre className="atlas-pre" style={{ maxHeight: 240 }}>
                  {JSON.stringify(view.record.content, null, 2)}
                </pre>
              </Space>
            )}
          </div>
        );
      })
    );

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <RunStatusHeader detail={detail} />

      <Tabs
        items={[
          {
            key: "result",
            label: "结果",
            children: <ResultBlock detail={detail} />,
          },
          {
            key: "graph",
            label: "执行路径",
            children: (
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                <RunGraph
                  state={runState}
                  selectedNodeId={selectedNodeId}
                  onNodeClick={handleNodeClick}
                />
                {selectedNode && (
                  <NodeInspector
                    node={selectedNode}
                    toolEvents={nodeToolEvents}
                    routingDecisions={runState.routingDecisions}
                    templateEdges={RUN_GRAPH_SPEC.templateEdges}
                  />
                )}
              </Space>
            ),
          },
          {
            key: "timeline",
            label: "时间线",
            children: <RunTimeline events={runState.timeline} />,
          },
          {
            key: "artifacts",
            label: "捕获正文",
            children: artifactsBlock,
          },
        ]}
      />
    </Space>
  );
}

// -- 运行目录卡片 -----------------------------------------------------------

interface RunCatalogCardProps {
  run: RunSummary;
  selected: boolean;
  onClick: () => void;
  onDelete: () => void;
  deleteBusy: boolean;
}

function RunCatalogCard({ run, selected, onClick, onDelete, deleteBusy }: RunCatalogCardProps) {
  const meta = statusTag(run.status);
  return (
    <div
      className={`atlas-run-card${selected ? " atlas-run-card--selected" : ""}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
    >
      <div className="atlas-run-card__head">
        <span className={`atlas-run-card__dot atlas-run-card__dot--${meta.color}`} />
        <Text strong className="atlas-mono">{run.run_id}</Text>
      </div>
      <div className="atlas-run-card__meta">
        <Text type="secondary">{run.mode} · {run.result_availability}</Text>
      </div>
      <div className="atlas-run-card__foot">
        <Tag color={meta.color}>{meta.label}</Tag>
        <Space size={8}>
          <Text type="secondary" className="atlas-mono">seq {run.last_seq}</Text>
          <Popconfirm
            title="确认删除"
            description={`删除运行 ${run.run_id}？删除后不可恢复。`}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={(e) => {
              e?.stopPropagation();
              onDelete();
            }}
          >
            <Button
              type="text"
              size="small"
              danger
              loading={deleteBusy}
              onClick={(e) => e.stopPropagation()}
            >
              删除
            </Button>
          </Popconfirm>
        </Space>
      </div>
    </div>
  );
}

// -- 面板主体 ---------------------------------------------------------------

interface Props {
  /** Bearer token（App 内存态）；空串时不发请求并提示认证。 */
  token: string;
  /** 初始选中运行（来自 /runs?run= 深链；App 解析 query 后传入）。 */
  initialRunId?: string;
}

export default function RunPanel({ token, initialRunId }: Props) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [runsError, setRunsError] = useState<unknown>(null);
  const [selected, setSelected] = useState<string | null>(initialRunId ?? null);
  const [detail, setDetail] = useState<RunView | null>(null);
  const [detailError, setDetailError] = useState<unknown>(null);
  const [streamError, setStreamError] = useState<unknown>(null);
  const [runState, setRunState] = useState<RunViewState>(() => emptyRunState(NODE_IDS));
  const [artifactViews, setArtifactViews] = useState<Record<string, ArtifactView>>({});
  const [feedbacks, setFeedbacks] = useState<FeedbackRecord[] | null>(null);
  const [feedbackError, setFeedbackError] = useState<unknown>(null);
  const [feedbackNote, setFeedbackNote] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<FeedbackVerdict>("up");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"more" | "feedback" | "delete" | null>(null);
  const authed = token !== "";

  // 深链变化（会话面板跳转 /runs?run=）→ 跟随选中
  useEffect(() => {
    if (initialRunId !== undefined) {
      setSelected(initialRunId);
    }
  }, [initialRunId]);

  // 目录（token 变化即重载；空 token 不发请求）
  useEffect(() => {
    if (token === "") {
      setRuns(null);
      setRunsError(null);
      setNextCursor(null);
      return;
    }
    let cancelled = false;
    setRunsError(null);
    listRuns(token, { limit: PAGE_SIZE })
      .then((page) => {
        if (!cancelled) {
          setRuns(page.items);
          setNextCursor(page.next_cursor);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setRunsError(e);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // 本人反馈列表（提交后刷新）
  useEffect(() => {
    if (token === "") {
      setFeedbacks(null);
      setFeedbackError(null);
      return;
    }
    let cancelled = false;
    listFeedback(token)
      .then((items) => {
        if (!cancelled) {
          setFeedbacks(items);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setFeedbackError(e);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // 详情 + 事件流（选中变化即重连；卸载/切换中止）
  useEffect(() => {
    if (token === "" || selected === null) {
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailError(null);
    setStreamError(null);
    setRunState(emptyRunState(NODE_IDS));
    setArtifactViews({});
    setFeedbackNote(null);
    getRun(selected, token)
      .then((view) => {
        if (!cancelled) {
          setDetail(view);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setDetailError(e);
        }
      });
    const controller = new AbortController();
    void (async () => {
      try {
        for await (const event of streamRunEvents(selected, token, {
          signal: controller.signal,
        })) {
          if (cancelled) {
            break;
          }
          setRunState((prev) => reduceRunEvent(prev, event));
        }
      } catch (e: unknown) {
        if (!cancelled && !controller.signal.aborted) {
          setStreamError(e);
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [token, selected]);

  function loadMore(): void {
    if (token === "" || nextCursor === null) {
      return;
    }
    setBusy("more");
    listRuns(token, { limit: PAGE_SIZE, cursor: nextCursor })
      .then((page) => {
        setRuns((prev) => [...(prev ?? []), ...page.items]);
        setNextCursor(page.next_cursor);
      })
      .catch((e: unknown) => setRunsError(e))
      .finally(() => setBusy(null));
  }

  function drillArtifact(artifactId: string): void {
    if (selected === null || token === "") {
      return;
    }
    setArtifactViews((prev) => ({ ...prev, [artifactId]: { status: "loading" } }));
    getRunArtifact(selected, artifactId, token)
      .then((record) =>
        setArtifactViews((prev) => ({ ...prev, [artifactId]: { status: "loaded", record } })),
      )
      .catch((error: unknown) =>
        setArtifactViews((prev) => ({ ...prev, [artifactId]: { status: "error", error } })),
      );
  }

  function sendFeedback(): void {
    if (selected === null || token === "") {
      return;
    }
    setBusy("feedback");
    setFeedbackError(null);
    setFeedbackNote(null);
    submitFeedback(
      { run_id: selected, verdict, comment: comment.trim() === "" ? undefined : comment },
      token,
    )
      .then(() => {
        setComment("");
        setFeedbackNote("已提交反馈（状态 pending_review）。");
        return listFeedback(token);
      })
      .then((items) => setFeedbacks(items))
      .catch((e: unknown) => setFeedbackError(e))
      .finally(() => setBusy(null));
  }

  function handleDeleteRun(runId: string): void {
    if (token === "") {
      return;
    }
    setBusy("delete");
    deleteRun(runId, token)
      .then(() => {
        // 从列表中移除
        setRuns((prev) => (prev ? prev.filter((r) => r.run_id !== runId) : prev));
        // 如果删除的是当前选中的运行，清空选中
        if (selected === runId) {
          setSelected(null);
          setDetail(null);
          setRunState(emptyRunState(NODE_IDS));
        }
      })
      .catch((e: unknown) => {
        setRunsError(e);
      })
      .finally(() => setBusy(null));
  }

  return (
    <div className="atlas-page">
      {PAGE_HEADER}

      {!authed && (
        <EmptyState
          icon="lock"
          title="尚未登录"
          description="请先在右上角选择角色开始使用"
        />
      )}

      <div className="atlas-observatory">
        <aside className="atlas-observatory__sidebar" aria-label="运行目录">
          <Section title="运行目录">
            {runsError !== null && <ErrorNote error={runsError} title="运行目录加载失败" />}
            {authed && runs === null && runsError === null && (
              <Text type="secondary">加载中…</Text>
            )}
            {runs !== null && runs.length === 0 && <Text type="secondary">暂无可见运行。</Text>}
            {runs !== null && runs.length > 0 && (
              <Space direction="vertical" size="small" style={{ width: "100%" }}>
                {runs.map((run) => (
                  <RunCatalogCard
                    key={run.run_id}
                    run={run}
                    selected={selected === run.run_id}
                    onClick={() => setSelected(run.run_id)}
                    onDelete={() => handleDeleteRun(run.run_id)}
                    deleteBusy={busy === "delete"}
                  />
                ))}
              </Space>
            )}
            {nextCursor !== null && (
              <Button
                size="small"
                style={{ marginTop: 8 }}
                loading={busy === "more"}
                onClick={loadMore}
              >
                加载更多
              </Button>
            )}
          </Section>
        </aside>

        <main className="atlas-observatory__main" aria-label="运行详情">
          <Section title="运行详情">
            {selected === null ? (
              <Text type="secondary">请从左侧列表选择一条运行查看详情。</Text>
            ) : (
              <Space direction="vertical" size="small" style={{ width: "100%" }}>
                {detailError !== null && (
                  <ErrorNote error={detailError} title="运行详情加载失败" />
                )}
                {streamError !== null && (
                  <ErrorNote error={streamError} title="事件流中断（不重跑；可重新选择该运行续读）" />
                )}
                {detail !== null && (
                  <RunDetailSection
                    detail={detail}
                    runState={runState}
                    token={token}
                    artifactViews={artifactViews}
                    onDrillArtifact={drillArtifact}
                  />
                )}
                {detail === null && detailError === null && (
                  <Text type="secondary">详情加载中…（事件流同步连接）</Text>
                )}
              </Space>
            )}
          </Section>

          <Section title="反馈" meta="本人">
            {selected === null ? (
              <Text type="secondary">选择运行后可提交反馈。</Text>
            ) : (
              <Space direction="vertical" size="small" style={{ width: "100%" }}>
                <Space wrap>
                  <Select
                    value={verdict}
                    onChange={(value: FeedbackVerdict) => setVerdict(value)}
                    options={VERDICT_OPTIONS}
                    style={{ width: 120 }}
                    disabled={!authed}
                  />
                  <Button
                    type="primary"
                    loading={busy === "feedback"}
                    disabled={!authed}
                    onClick={sendFeedback}
                  >
                    提交反馈
                  </Button>
                </Space>
                <Input.TextArea
                  rows={2}
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="补充说明（可选；请勿包含敏感信息）"
                  disabled={!authed}
                />
                {feedbackNote !== null && <Alert type="success" showIcon message={feedbackNote} />}
                {feedbackError !== null && (
                  <ErrorNote error={feedbackError} title="反馈提交失败" />
                )}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  审核状态由服务端固定为 pending_review；审核页留 T12。以下为本人反馈列表
                  （GET /feedback）：
                </Text>
                {feedbacks === null ? (
                  <Text type="secondary">反馈列表加载中…（认证后拉取）</Text>
                ) : feedbacks.length === 0 ? (
                  <Text type="secondary">尚无反馈记录。</Text>
                ) : (
                  <Table
                    size="small"
                    rowKey="feedback_id"
                    pagination={false}
                    scroll={{ x: "max-content" }}
                    dataSource={feedbacks}
                    columns={[
                      {
                        title: "run_id",
                        dataIndex: "run_id",
                        render: (value: string) => <Text code>{value}</Text>,
                      },
                      { title: "结论", dataIndex: "verdict", width: 96 },
                      { title: "审核状态", dataIndex: "status", width: 128 },
                      {
                        title: "训练资格",
                        dataIndex: "training_eligible",
                        width: 96,
                        render: (value: boolean) => (value ? "是" : "否"),
                      },
                      { title: "提交时间", dataIndex: "created_at", width: 216 },
                    ]}
                  />
                )}
              </Space>
            )}
          </Section>
        </main>
      </div>
    </div>
  );
}
