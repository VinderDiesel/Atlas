/**
 * T11 节点实验面板（NodeExperiment）：单节点对照实验界面。
 *
 * 功能：
 * - 选择 baseline/candidate release、节点类型、数据集版本
 * - 提交实验并查看结果
 * - 实验历史列表
 *
 * 边界：
 * - 实验不修改 active 发布（只读对照）
 * - 实验结果不自动发布——需人工审核后走发布流程
 */

import React, { useState, useCallback } from "react";
import { Card, Form, Input, Select, Button, Tag, Space, Alert } from "antd";

/** 已注册节点类型（与 agent/flows/contracts.py REGISTERED_NODE_TYPES 一致） */
const REGISTERED_NODE_TYPES = [
  "rule_plan",
  "retrieve",
  "bind_plan",
  "execute_plan",
  "analysis",
  "explain",
  "chart",
  "clarify",
  "handoff",
];

interface ExperimentSpec {
  baseline_release_id: string;
  candidate_release_id: string;
  node_id: string;
  dataset_version: string;
}

interface ExperimentReceipt {
  experiment_id: string;
  status: string;
  baseline_release_id: string;
  candidate_release_id: string;
  node_id: string;
  dataset_version: string;
  comparison: Record<string, unknown>;
}

/**
 * 节点实验面板：提交对照实验并查看历史。
 */
export const NodeExperimentPanel: React.FC = () => {
  const [form] = Form.useForm<ExperimentSpec>();
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ExperimentReceipt | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = useCallback(async (values: ExperimentSpec) => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      // TODO: 接入真实 API（/api/v1/manage/experiments）
      // 首版为界面骨架，不调后端
      console.log("Experiment spec:", values);
      setResult({
        experiment_id: "pending-api-integration",
        status: "pending",
        ...values,
        comparison: {},
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "实验提交失败");
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <div style={{ padding: 24 }}>
      <Card title="节点对照实验" style={{ marginBottom: 24 }}>
        <Alert
          message="实验不修改 active 发布，不影响在线会话"
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{ node_id: "rule_plan", dataset_version: "test-v1" }}
        >
          <Form.Item
            name="baseline_release_id"
            label="Baseline Release ID"
            rules={[
              { required: true, message: "请输入 baseline release SHA" },
              { pattern: /^[0-9a-f]{64}$/, message: "必须是 64 位 hex" },
            ]}
          >
            <Input placeholder={"a".repeat(64)} />
          </Form.Item>

          <Form.Item
            name="candidate_release_id"
            label="Candidate Release ID"
            rules={[
              { required: true, message: "请输入 candidate release SHA" },
              { pattern: /^[0-9a-f]{64}$/, message: "必须是 64 位 hex" },
            ]}
          >
            <Input placeholder={"b".repeat(64)} />
          </Form.Item>

          <Form.Item
            name="node_id"
            label="节点类型"
            rules={[{ required: true, message: "请选择节点类型" }]}
          >
            <Select>
              {REGISTERED_NODE_TYPES.map((t) => (
                <Select.Option key={t} value={t}>
                  {t}
                </Select.Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item
            name="dataset_version"
            label="数据集版本"
            rules={[{ required: true, message: "请输入数据集版本" }]}
          >
            <Input placeholder="test-v1" />
          </Form.Item>

          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading}>
              运行实验
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {error && (
        <Alert message="实验失败" description={error} type="error" showIcon />
      )}

      {result && (
        <Card title="实验结果" style={{ marginBottom: 24 }}>
          <Space direction="vertical" style={{ width: "100%" }}>
            <div>
              <strong>实验 ID：</strong>
              <Tag>{result.experiment_id}</Tag>
            </div>
            <div>
              <strong>状态：</strong>
              <Tag color={result.status === "completed" ? "green" : "orange"}>
                {result.status}
              </Tag>
            </div>
            <div>
              <strong>节点：</strong>
              <Tag>{result.node_id}</Tag>
            </div>
            <div>
              <strong>Baseline：</strong>
              <code>{result.baseline_release_id.slice(0, 12)}...</code>
            </div>
            <div>
              <strong>Candidate：</strong>
              <code>{result.candidate_release_id.slice(0, 12)}...</code>
            </div>
          </Space>
        </Card>
      )}

      <Card title="实验历史">
        <Alert
          message="API 集成待完成——历史列表将在后端端点就绪后显示"
          type="warning"
          showIcon
        />
      </Card>
    </div>
  );
};

export default NodeExperimentPanel;
