/**
 * 面板 3：会话时间线（SessionPanel；设计页 §2 面板 3）——P2。
 *
 * 数据源：`GET /api/v1/health` 的 `boot_id`（服务端本次运行标识；公开面免
 * token，由 App 拉取后传入）+ 工作台响应回传的 `session_id` / `turns_in_session`
 * （App 内存累积的 SessionLogEntry[]；本面板不自行发请求）。
 *
 * **限制横幅是渲染义务**（设计页 §7 KL 草案 / dev-plan §2.7 允许的限制）：
 * 0022 未开 `/governance/sessions` 类端点 → 跨重启、跨标签页的历史会话无数据源，
 * 本页只能列出「本标签页本次运行累积」的会话。横幅文案与 README KL 条目同源，
 * **不得删除或弱化**；服务端重启后 boot_id 变化即为该限制的可见证据。
 *
 * **边界约束（B2a 分组布局演进）**：
 * - 会话记录按角色分组展示（groupSessionsByRole；设计页 §3.5 约束 1 的可视化）
 * - 数据源仅为 App 内存态的 SessionLogEntry[]（no-persist 约束；§3.5 约束 3）
 * - 不引入 localStorage / sessionStorage / IndexedDB（静态断言见 no-persist.test.ts）
 * - 跨重启/跨刷新的会话列表**无数据源**（0022 未开只读端点；README 已知限制）
 * - 分组函数为纯函数（可被 vitest 直接断言；不 import React）
 */
import { Alert, Descriptions, Divider, Space, Tag, Typography } from "antd";

import type { HealthPayload } from "../../api/types";
import { groupSessionsByRole, type SessionLogEntry } from "../../state/session";
import ErrorNote from "../../components/ErrorNote";

const { Text } = Typography;

interface Props {
  health: HealthPayload | null;
  healthError: string | null;
  log: readonly SessionLogEntry[];
  currentId: string;
}

function formatTime(epochMs: number): string {
  return new Date(epochMs).toLocaleString();
}

export default function SessionPanel({ health, healthError, log, currentId }: Props) {
  const groups = groupSessionsByRole(log);

  return (
    <Space
      direction="vertical"
      size="middle"
      style={{ width: "100%", maxWidth: 1080, margin: "0 auto", display: "flex" }}
    >
      <Alert
        type="warning"
        showIcon
        message="会话时间线只能展示当前运行的会话（跨重启历史无数据源）"
        description={
          <Space direction="vertical" size={0}>
            <span>
              HTTP 契约 v2 未开 /governance/sessions 类端点；0020 的 SQLite checkpoint
              是 Agent 内部状态存储，不是可查询的会话目录。
            </span>
            <span>
              本页只列出本浏览器标签页内本次运行累积的会话；服务端重启后 boot_id
              变化，历史会话虽在 SQLite 里但前端列不出来。
            </span>
            <span>
              列出历史会话需新增只读端点并裁定其权限口径（会话含问句原文，属敏感面）
              ——未裁定（README 已知限制）。
            </span>
          </Space>
        }
      />

      {healthError !== null && <ErrorNote error={healthError} title="health 拉取失败" />}

      <Descriptions size="small" column={1} title="服务端本次运行">
        <Descriptions.Item label="boot_id">
          {health === null ? (
            <Text type="secondary">未取得（/api/v1/health 未返回）</Text>
          ) : (
            <Text code>{health.boot_id}</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="health.status">
          {health === null ? "—" : health.status}
        </Descriptions.Item>
      </Descriptions>

      {groups.length === 0 ? (
        <Text type="secondary">尚无会话记录（工作台响应回传 session_id 后自动累积）</Text>
      ) : (
        groups.map((group, idx) => (
          <div key={group.role ?? "__null__"}>
            {idx > 0 && <Divider orientation="left" plain>角色切换</Divider>}
            <Divider orientation="left">
              {group.role === null ? "未认证" : group.role}（{group.sessions.length} 条会话）
            </Divider>
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              {group.sessions.map((entry) => (
                <div
                  key={entry.id}
                  style={{
                    padding: "8px 12px",
                    border: "1px solid #d9d9d9",
                    borderRadius: 6,
                    backgroundColor: entry.id === currentId ? "#e6f4ff" : "#fafafa",
                  }}
                >
                  <Space size={4} style={{ marginBottom: 4 }}>
                    <Text code style={{ fontSize: 12 }}>
                      {entry.id}
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
        ))
      )}

      <Text type="secondary">
        轮数来自工作台响应回传的 turns_in_session（本页记录已见最大值）；
        「当前」行的 session_id 即工作台多轮续接所用——激活不同身份（切角色）会轮换出新行。
        问句原文在服务端 checkpoint 内，本页不展示（无只读出口，见上方横幅）。
      </Text>
    </Space>
  );
}
