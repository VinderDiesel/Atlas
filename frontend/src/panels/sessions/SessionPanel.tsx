/**
 * 面板 3：会话与历史（SessionPanel）。
 *
 * 展示服务端会话目录（跨重启持久化）和本标签页会话日志（内存态，刷新即失）。
 * token 空串不发请求（认证前不产生 401 噪声）。
 */
import { Button, Divider, Space, Table, Tag, Typography } from "antd";
import { useEffect, useState } from "react";

import { listSessionRuns, listSessions } from "../../api/control";
import type { RunStatus, RunSummary, SessionSummary } from "../../api/types";
import EmptyState from "../../components/EmptyState";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import Section from "../../components/Section";
import { groupSessionsByRole, type SessionLogEntry } from "../../state/session";
import { statusTag } from "../runs/run-view-model";

const { Text } = Typography;

interface Props {
  log: readonly SessionLogEntry[];
  currentId: string;
  /** Bearer token（App 内存态）；空串时不发请求。 */
  token: string;
  /** 打开运行详情（App 提供路由动作：navigate 到 /runs?run=）。 */
  onOpenRun: (runId: string) => void;
}

function formatTime(epochMs: number): string {
  return new Date(epochMs).toLocaleString();
}

function StatusTagCell({ status }: { status: RunStatus }) {
  const meta = statusTag(status);
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

const PAGE_HEADER = (
  <PageHeader
    title="会话历史"
    description="查看历史会话和每次查询的运行记录。"
  />
);

export default function SessionPanel({
  log,
  currentId,
  token,
  onOpenRun,
}: Props) {
  const groups = groupSessionsByRole(log);
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [sessionsError, setSessionsError] = useState<unknown>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<unknown>(null);

  // 服务端会话目录（token 变化即重载；空 token 不发请求）
  useEffect(() => {
    if (token === "") {
      setSessions(null);
      setSessionsError(null);
      return;
    }
    let cancelled = false;
    setSessionsError(null);
    listSessions(token, { limit: 20 })
      .then((page) => {
        if (!cancelled) {
          setSessions(page.items);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setSessionsError(e);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // 展开会话的运行清单（新→旧）
  useEffect(() => {
    if (token === "" || expanded === null) {
      return;
    }
    let cancelled = false;
    setRuns(null);
    setRunsError(null);
    listSessionRuns(expanded, token, { limit: 20 })
      .then((page) => {
        if (!cancelled) {
          setRuns(page.items);
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
  }, [token, expanded]);

  // 未认证：显示友好空状态
  if (token === "") {
    return (
      <div className="atlas-page">
        {PAGE_HEADER}
        <EmptyState
          icon="lock"
          title="尚未登录"
          description="请先在右上角选择角色开始使用"
        />
      </div>
    );
  }

  return (
    <div className="atlas-page">
      {PAGE_HEADER}

      <Section title="会话列表">
        {sessionsError !== null ? (
          <ErrorNote error={sessionsError} title="加载失败" />
        ) : sessions === null ? (
          <Text type="secondary">加载中…</Text>
        ) : sessions.length === 0 ? (
          <EmptyState
            icon="inbox"
            title="暂无会话"
            description="在工作台提问后，会话记录会出现在这里"
          />
        ) : (
          <>
            <Table
              size="small"
              rowKey="session_id"
              pagination={false}
              scroll={{ x: "max-content" }}
              dataSource={sessions}
              columns={[
                {
                  title: "会话 ID",
                  dataIndex: "session_id",
                  render: (value: string) => <Text code>{value.slice(0, 12)}…</Text>,
                },
                { title: "运行数", dataIndex: "run_count", width: 80 },
                {
                  title: "最近状态",
                  dataIndex: "last_status",
                  width: 104,
                  render: (value: RunStatus) => <StatusTagCell status={value} />,
                },
                { title: "最近运行", dataIndex: "last_run_at", width: 200 },
                {
                  title: "操作",
                  key: "op",
                  width: 100,
                  render: (_: unknown, row: SessionSummary) => (
                    <Button
                      type="link"
                      size="small"
                      onClick={() =>
                        setExpanded((prev) => (prev === row.session_id ? null : row.session_id))
                      }
                    >
                      {expanded === row.session_id ? "收起" : "查看运行"}
                    </Button>
                  ),
                },
              ]}
            />
            {expanded !== null && (
              <div style={{ marginTop: 12 }}>
                <Divider orientation="left" plain style={{ margin: "8px 0" }}>
                  运行记录
                </Divider>
                {runsError !== null && (
                  <ErrorNote error={runsError} title="运行列表加载失败" />
                )}
                {runs === null && runsError === null && <Text type="secondary">加载中…</Text>}
                {runs !== null && runs.length === 0 && (
                  <Text type="secondary">该会话暂无运行记录。</Text>
                )}
                {runs !== null && runs.length > 0 && (
                  <Table
                    size="small"
                    rowKey="run_id"
                    pagination={false}
                    scroll={{ x: "max-content" }}
                    dataSource={runs}
                    columns={[
                      {
                        title: "运行 ID",
                        dataIndex: "run_id",
                        render: (value: string) => <Text code>{value.slice(0, 12)}…</Text>,
                      },
                      {
                        title: "状态",
                        dataIndex: "status",
                        width: 96,
                        render: (value: RunStatus) => <StatusTagCell status={value} />,
                      },
                      { title: "更新时间", dataIndex: "updated_at", width: 200 },
                      {
                        title: "操作",
                        key: "op",
                        width: 80,
                        render: (_: unknown, row: RunSummary) => (
                          <Button
                            type="link"
                            size="small"
                            onClick={() => onOpenRun(row.run_id)}
                          >
                            详情
                          </Button>
                        ),
                      },
                    ]}
                  />
                )}
              </div>
            )}
          </>
        )}
      </Section>

      {groups.length > 0 && (
        <Section title="本次会话" meta="刷新后清除">
          {groups.map((group, idx) => (
            <div key={group.role ?? "__null__"}>
              {idx > 0 && <Divider plain />}
              <Space direction="vertical" size="small" style={{ width: "100%" }}>
                {group.sessions.map((entry) => (
                  <div
                    key={entry.id}
                    className={
                      entry.id === currentId
                        ? "atlas-session-entry atlas-session-entry--current"
                        : "atlas-session-entry"
                    }
                  >
                    <Space size={4} style={{ marginBottom: 4 }}>
                      <Text code style={{ fontSize: 12 }}>
                        {entry.id.slice(0, 12)}…
                      </Text>
                      {entry.id === currentId && <Tag color="blue">当前</Tag>}
                    </Space>
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        开始：{formatTime(entry.startedAt)} · 轮数：{entry.turnsSeen}
                      </Text>
                    </div>
                  </div>
                ))}
              </Space>
            </div>
          ))}
        </Section>
      )}
    </div>
  );
}
