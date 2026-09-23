/**
 * 治理页发布身份（只读；ADR-0031 D04「governance 显示发布身份」；T08c）。
 *
 * 显示各部署当前绑定的**活动发布制品身份**：release_id 是内容身份，不是可移动
 * 的 latest（D04）——active_release_id=null 表示该部署尚未绑定发布，如实显示
 * 「未绑定发布」（不猜测、不用占位符冒充）。
 *
 * 为什么加载是显式用户动作而非挂载即发：治理页挂载 8 条请求是 ADR-0022 决策 ⑥
 * 限流推导（240/min ≈ 24 次导航/分钟）的前提；本区块新增挂载请求会改变该口径，
 * 需重推 ADR——因此保持「点击加载」，不静默改动推导前提。
 *
 * 只读边界（ADR-0028 决策 ③）：本文件只有 getJson 读取与一个触发按钮——零写
 * 控件（governance-readonly.test.ts 静态守线）；发布/回退等写动作在「接入」页
 * 的语义草稿面板，不属治理面。
 */
import { Alert, Button, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { getJson } from "../../api/client";
import { API } from "../../api/endpoints";
import type { DeploymentRecord } from "../../api/types";
import ErrorNote from "../../components/ErrorNote";
import Section from "../../components/Section";

const { Text } = Typography;

/** 部署 → 发布身份表（只渲染固定列；夹带隐藏字段不出现在输出）。 */
export function PublishIdentityRows({ items }: { items: DeploymentRecord[] }) {
  return (
    <Table
      size="small"
      rowKey="deployment_id"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={items}
      columns={[
        {
          title: "deployment_id",
          dataIndex: "deployment_id",
          render: (value: string) => <Text code>{value}</Text>,
        },
        { title: "领域", dataIndex: "scope", width: 96 },
        { title: "source_id", dataIndex: "source_id", width: 152 },
        {
          title: "活动发布（内容身份）",
          dataIndex: "active_release_id",
          width: 560,
          render: (value: string | null) =>
            value === null ? (
              <Tag>未绑定发布</Tag>
            ) : (
              <Text code>{value}</Text>
            ),
        },
        { title: "指针版本", dataIndex: "revision", width: 88 },
        { title: "更新时间", dataIndex: "updated_at", width: 220 },
      ]}
    />
  );
}

interface Props {
  /** Bearer token（治理页已收窄为非空；未认证态由布局整页替换）。 */
  token: string;
}

export default function PublishIdentitySection({ token }: Props) {
  const [items, setItems] = useState<DeploymentRecord[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  function load(): void {
    setLoading(true);
    setError(null);
    getJson<{ items: DeploymentRecord[] }>(API.manageDeployments, token)
      .then((page) => setItems(page.items))
      .catch((e: unknown) => setError(e))
      .finally(() => setLoading(false));
  }

  return (
    <Section title="发布身份" meta="只读 · 显式加载">
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Space size={8} wrap>
          <Button size="small" loading={loading} onClick={load}>
            {items === null ? "加载发布身份" : "刷新发布身份"}
          </Button>
          <Text type="secondary">
            各部署当前绑定的活动发布制品（release_id 是内容身份，不是可移动的 latest——ADR-0031
            D04）；显式加载，不改变本页挂载 8 条的限流口径。
          </Text>
        </Space>
        {error !== null ? (
          <ErrorNote error={error} title="发布身份加载失败" />
        ) : items === null ? (
          <Alert
            type="info"
            showIcon
            message="尚未加载：点击「加载发布身份」读取部署指针（GET /manage/deployments）。"
          />
        ) : items.length === 0 ? (
          <Text type="secondary">尚无部署：先在「接入」页创建部署绑定。</Text>
        ) : (
          <PublishIdentityRows items={items} />
        )}
      </Space>
    </Section>
  );
}
