/**
 * T14 训练任务面板（TrainingJob）：训练配方查看与任务状态监控。
 *
 * 功能：
 * - 查看 intent_v1 训练配方（D11 参数）
 * - 查看训练任务状态（queued/running/succeeded/failed/blocked/cancelled）
 * - 预检结果展示（blocked 理由集合）
 *
 * 边界：
 * - 训练任务不自动发布——需人工审核后走模型发布流程（T15）
 * - 权重不入 Git（.gitignore）
 * - 无 GPU 环境显示 BLOCKED 状态，不伪装可训练
 */

import React from "react";
import { Card, Descriptions, Tag, Space, Alert, Table, Typography } from "antd";

const { Text } = Typography;

/** 训练任务状态 */
type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "cancelled";

/** 训练配方（intent_v1.yaml 摘要） */
interface TrainingRecipe {
  adapter_name: string;
  base_model: string;
  base_model_revision: string;
  lora_r: number;
  lora_alpha: number;
  lora_dropout: number;
  epochs: number;
  learning_rate: number;
  batch_size: number;
  grad_accum_steps: number;
  max_length: number;
  seed: number;
  min_samples: number;
}

/** 训练任务状态 */
interface TrainingJobState {
  job_id: string;
  status: JobStatus;
  budget_charged: boolean;
  created_at: string;
  updated_at: string;
}

/** 预检结果 */
interface PreflightResult {
  status: "ready" | "blocked";
  blocked_reasons: string[];
  messages: string[];
}

/** 默认配方（D11 初始值） */
const DEFAULT_RECIPE: TrainingRecipe = {
  adapter_name: "intent_v1",
  base_model: "Qwen/Qwen2.5-7B-Instruct",
  base_model_revision: "unknown",
  lora_r: 16,
  lora_alpha: 32,
  lora_dropout: 0.05,
  epochs: 1,
  learning_rate: 0.0002,
  batch_size: 1,
  grad_accum_steps: 16,
  max_length: 2048,
  seed: 42,
  min_samples: 50,
};

/** 状态标签颜色 */
const STATUS_COLOR: Record<JobStatus, string> = {
  queued: "blue",
  running: "processing",
  succeeded: "success",
  failed: "error",
  blocked: "warning",
  cancelled: "default",
};

/** 阻塞理由说明 */
const BLOCKED_REASON_LABELS: Record<string, string> = {
  budget_not_approved: "预算未批准",
  dataset_not_found: "数据集不存在",
  empty_dataset: "数据集为空",
  model_not_licensed: "模型未获许可",
  no_cuda: "无可用 CUDA GPU",
  no_torch: "缺少 ML 依赖 torch",
};

/**
 * 训练配方卡片（D11 参数摘要）
 */
const RecipeCard: React.FC<{ recipe: TrainingRecipe }> = ({ recipe }) => (
  <Card title="训练配方（intent_v1）" size="small" style={{ marginBottom: 16 }}>
    <Descriptions column={2} size="small">
      <Descriptions.Item label="Adapter">{recipe.adapter_name}</Descriptions.Item>
      <Descriptions.Item label="基座模型">{recipe.base_model}</Descriptions.Item>
      <Descriptions.Item label="基座 Revision">
        <Text code>{recipe.base_model_revision}</Text>
      </Descriptions.Item>
      <Descriptions.Item label="许可">
        <Tag color="green">Apache-2.0</Tag>
      </Descriptions.Item>
      <Descriptions.Item label="LoRA r/α">{recipe.lora_r}/{recipe.lora_alpha}</Descriptions.Item>
      <Descriptions.Item label="Dropout">{recipe.lora_dropout}</Descriptions.Item>
      <Descriptions.Item label="Epochs">{recipe.epochs}</Descriptions.Item>
      <Descriptions.Item label="Learning Rate">{recipe.learning_rate}</Descriptions.Item>
      <Descriptions.Item label="Batch × GradAccum">
        {recipe.batch_size} × {recipe.grad_accum_steps} = {recipe.batch_size * recipe.grad_accum_steps}
      </Descriptions.Item>
      <Descriptions.Item label="Max Length">{recipe.max_length}</Descriptions.Item>
      <Descriptions.Item label="Seed">{recipe.seed}</Descriptions.Item>
      <Descriptions.Item label="Min Samples">{recipe.min_samples}</Descriptions.Item>
    </Descriptions>
  </Card>
);

/**
 * 预检结果面板
 */
const PreflightPanel: React.FC<{ result: PreflightResult | null }> = ({ result }) => {
  if (!result) return null;

  if (result.status === "ready") {
    return (
      <Alert
        type="success"
        message="预检通过"
        description={result.messages.join("；")}
        showIcon
        style={{ marginBottom: 16 }}
      />
    );
  }

  return (
    <Alert
      type="warning"
      message="预检阻塞"
      description={
        <Space direction="vertical" size={4}>
          {result.blocked_reasons.map((reason) => (
            <Text key={reason}>
              <Tag color="orange">{BLOCKED_REASON_LABELS[reason] || reason}</Tag>
              {result.messages.find((m) => m.includes(reason)) || ""}
            </Text>
          ))}
        </Space>
      }
      showIcon
      style={{ marginBottom: 16 }}
    />
  );
};

/**
 * 训练任务面板（骨架实现）
 *
 * API 集成待后续接入——当前展示静态配方与示例状态。
 */
export const TrainingJobPanel: React.FC = () => {
  // TODO: 接入真实 API 获取任务列表与预检结果
  const [jobs] = React.useState<TrainingJobState[]>([]);
  const [preflight] = React.useState<PreflightResult | null>(null);

  const columns = [
    { title: "Job ID", dataIndex: "job_id", key: "job_id" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (status: JobStatus) => (
        <Tag color={STATUS_COLOR[status]}>{status}</Tag>
      ),
    },
    {
      title: "预算已扣",
      dataIndex: "budget_charged",
      key: "budget_charged",
      render: (charged: boolean) => (charged ? "是" : "否"),
    },
    { title: "创建时间", dataIndex: "created_at", key: "created_at" },
    { title: "更新时间", dataIndex: "updated_at", key: "updated_at" },
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={16}>
      <RecipeCard recipe={DEFAULT_RECIPE} />
      <PreflightPanel result={preflight} />
      <Card title="训练任务历史" size="small">
        <Table
          dataSource={jobs}
          columns={columns}
          rowKey="job_id"
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: "暂无训练任务" }}
          size="small"
        />
      </Card>
      <Alert
        type="info"
        message="训练任务说明"
        description={
          <>
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              <li>训练需要显式预算批准（budget_approved=True），即使 CUDA 可见</li>
              <li>失败/阻塞的任务不注册模型 artifact（artifact_digest=None）</li>
              <li>权重不入 Git（.gitignore），只入库 train-report.json</li>
              <li>训练完成后需人工审核走模型发布流程（T15），不自动切换 active 模型</li>
              <li>无 GPU 环境显示 BLOCKED 状态——这是设计结论，不是 bug</li>
            </ul>
          </>
        }
        showIcon
      />
    </Space>
  );
};

export default TrainingJobPanel;
