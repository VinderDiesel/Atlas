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
 */
import { Alert, Descriptions, Space, Table, Tag, Typography } from "antd";

import type { HealthPayload } from "../../api/types";
import type { SessionLogEntry } from "../../state/session";
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

      <Table<SessionLogEntry>
        rowKey="id"
        size="small"
        pagination={false}
        dataSource={[...log]}
        columns={[
          {
            title: "会话 id",
            dataIndex: "id",
            render: (id: string) => (
              <Space size={4}>
                <Text code>{id}</Text>
                {id === currentId && <Tag color="blue">当前</Tag>}
              </Space>
            ),
          },
          {
            title: "激活时角色",
            dataIndex: "role",
            render: (role: string | null) => role ?? "未认证",
          },
          {
            title: "开始时间（本地）",
            dataIndex: "startedAt",
            render: (startedAt: number) => formatTime(startedAt),
          },
          { title: "轮数（本页已见）", dataIndex: "turnsSeen" },
        ]}
      />

      <Text type="secondary">
        轮数来自工作台响应回传的 turns_in_session（本页记录已见最大值）；
        「当前」行的 session_id 即工作台多轮续接所用——激活不同身份（切角色）会轮换出新行。
        问句原文在服务端 checkpoint 内，本页不展示（无只读出口，见上方横幅）。
      </Text>
    </Space>
  );
}
