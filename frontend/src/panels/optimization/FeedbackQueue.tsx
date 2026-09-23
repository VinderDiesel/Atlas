/**
 * T12 反馈审核队列（FeedbackQueue）：跨用户反馈审核界面。
 *
 * 功能：
 * - 显示 pending_review 反馈列表（审核队列）
 * - 审核操作：通过（approved）/ 拒绝（rejected）
 * - 撤销审核（revoke）
 * - 归因节点显示
 *
 * 边界：
 * - 审核动作需要 reviewer 能力
 * - 已审核的反馈不可重复审核（不可变审核证据）
 * - 审核撤销保留审计轨迹
 * - API 集成待完成——首版为界面骨架
 */

import React, { useState, useCallback } from "react";
import {
  Card,
  Table,
  Button,
  Tag,
  Space,
  Alert,
  Popconfirm,
  Typography,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";

const { Text } = Typography;

/** 反馈状态 */
type FeedbackStatus = "pending_review" | "approved" | "rejected";

/** 反馈记录（与后端 FeedbackRecord 合同对齐） */
interface FeedbackRecord {
  feedback_id: string;
  run_id: string;
  owner: { issuer: string; subject: string };
  verdict: "up" | "down" | "corrected";
  comment: string | null;
  correction: Record<string, unknown> | null;
  status: FeedbackStatus;
  training_eligible: boolean;
  created_at: string;
  attribution_node: string | null;
  reviewed_by: { issuer: string; subject: string } | null;
  reviewed_at: string | null;
}

/** 审核决定 */
type ReviewDecision = "approved" | "rejected";

/**
 * 反馈审核队列面板：显示待审核反馈并提供审核操作。
 */
export const FeedbackQueuePanel: React.FC = () => {
  const [dataSource, setDataSource] = useState<FeedbackRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  // TODO: 接入真实 API（GET /api/v1/manage/feedback/pending）
  // 首版为界面骨架，不调后端
  const fetchPendingFeedback = useCallback(async () => {
    setLoading(true);
    try {
      // TODO: const response = await fetch('/api/v1/manage/feedback/pending');
      // const data = await response.json();
      // setDataSource(data.items);
      console.log("Fetching pending feedback...");
      setMessage("API 集成待完成——审核队列将在后端端点就绪后显示");
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const handleReview = useCallback(
    async (feedbackId: string, decision: ReviewDecision) => {
      // TODO: 接入真实 API（POST /api/v1/manage/feedback/{id}/review）
      console.log(`Review ${feedbackId}: ${decision}`);
      setMessage(`审核操作已提交：${decision}（API 集成待完成）`);
    },
    []
  );

  const handleRevoke = useCallback(async (feedbackId: string) => {
    // TODO: 接入真实 API（POST /api/v1/manage/feedback/{id}/revoke）
    console.log(`Revoke ${feedbackId}`);
    setMessage(`撤销操作已提交（API 集成待完成）`);
  }, []);

  const columns: ColumnsType<FeedbackRecord> = [
    {
      title: "反馈 ID",
      dataIndex: "feedback_id",
      key: "feedback_id",
      render: (id: string) => <Text code>{id.slice(0, 12)}...</Text>,
    },
    {
      title: "运行 ID",
      dataIndex: "run_id",
      key: "run_id",
      render: (id: string) => <Text code>{id.slice(0, 12)}...</Text>,
    },
    {
      title: "提交者",
      dataIndex: "owner",
      key: "owner",
      render: (owner: { subject: string }) => owner.subject,
    },
    {
      title: "判定",
      dataIndex: "verdict",
      key: "verdict",
      render: (verdict: string) => {
        const colorMap: Record<string, string> = {
          up: "green",
          down: "red",
          corrected: "orange",
        };
        return <Tag color={colorMap[verdict] || "default"}>{verdict}</Tag>;
      },
    },
    {
      title: "归因节点",
      dataIndex: "attribution_node",
      key: "attribution_node",
      render: (node: string | null) =>
        node ? <Tag color="blue">{node}</Tag> : <Text type="secondary">-</Text>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (status: FeedbackStatus) => {
        const colorMap: Record<string, string> = {
          pending_review: "orange",
          approved: "green",
          rejected: "red",
        };
        const labelMap: Record<string, string> = {
          pending_review: "待审核",
          approved: "已通过",
          rejected: "已拒绝",
        };
        return <Tag color={colorMap[status]}>{labelMap[status]}</Tag>;
      },
    },
    {
      title: "训练资格",
      dataIndex: "training_eligible",
      key: "training_eligible",
      render: (eligible: boolean) =>
        eligible ? (
          <Tag color="green">可训练</Tag>
        ) : (
          <Tag color="default">不可训练</Tag>
        ),
    },
    {
      title: "提交时间",
      dataIndex: "created_at",
      key: "created_at",
      render: (ts: string) => new Date(ts).toLocaleString("zh-CN"),
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, record: FeedbackRecord) => {
        if (record.status === "pending_review") {
          return (
            <Space>
              <Button
                type="primary"
                size="small"
                icon={<CheckCircleOutlined />}
                onClick={() => handleReview(record.feedback_id, "approved")}
              >
                通过
              </Button>
              <Button
                danger
                size="small"
                icon={<CloseCircleOutlined />}
                onClick={() => handleReview(record.feedback_id, "rejected")}
              >
                拒绝
              </Button>
            </Space>
          );
        }
        return (
          <Popconfirm
            title="确定撤销审核？"
            description="撤销后训练资格将回退为不可训练"
            onConfirm={() => handleRevoke(record.feedback_id)}
          >
            <Button size="small" icon={<UndoOutlined />}>
              撤销
            </Button>
          </Popconfirm>
        );
      },
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Card title="反馈审核队列" style={{ marginBottom: 24 }}>
        <Alert
          message="审核队列显示所有待审核反馈；审核动作需要 reviewer 能力"
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
        <Alert
          message="已审核的反馈不可重复审核（不可变审核证据）；撤销操作保留审计轨迹"
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />
        {message && (
          <Alert
            message={message}
            type="info"
            showIcon
            closable
            style={{ marginBottom: 16 }}
            onClose={() => setMessage(null)}
          />
        )}
        <Table
          columns={columns}
          dataSource={dataSource}
          rowKey="feedback_id"
          loading={loading}
          pagination={{ pageSize: 20 }}
          locale={{ emptyText: "暂无待审核反馈" }}
        />
      </Card>

      <Card title="审核说明">
        <Space direction="vertical">
          <Text>
            <strong>通过（approved）：</strong>
            反馈被采纳为训练标签，training_eligible 设为 true
          </Text>
          <Text>
            <strong>拒绝（rejected）：</strong>
            反馈被拒绝，training_eligible 保持 false
          </Text>
          <Text>
            <strong>撤销（revoke）：</strong>
            已审核的反馈回退到待审核状态，training_eligible 回退为 false
          </Text>
          <Text type="secondary">
            所有审核动作记录在 feedback_reviews 审计轨迹表中
          </Text>
        </Space>
      </Card>
    </div>
  );
};

export default FeedbackQueuePanel;
