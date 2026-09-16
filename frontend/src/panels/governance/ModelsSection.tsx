/**
 * 治理子页 1：语义模型（models）——P2。
 *
 * 消费端点 1（`/governance/models`，全域一次返回）。嵌套块（time_dimension /
 * lineage / 治理复核周期）走**行展开**展示——本面板不把嵌套结构摊平成列
 * （P2 的统一约定：表格列只放标量，复合结构进展开区或 Drawer）。
 */
import { Descriptions, Space, Table, Tag, Typography } from "antd";

import type { ModelsItem } from "../../api/types";

import { dashOr, SectionData, type CollectionState } from "./GovernanceSection";

const { Text } = Typography;

interface Props {
  state: CollectionState<ModelsItem>;
}

function ModelDetail({ item }: { item: ModelsItem }) {
  const timeDimension =
    item.time_dimension === null ? (
      <Text type="secondary">未声明（null——ossie 无 time_dimension 块）</Text>
    ) : (
      <Space direction="vertical" size={0}>
        <span>{`table=${item.time_dimension.table} · mode=${item.time_dimension.mode}`}</span>
        <span>
          {Object.entries(item.time_dimension.columns)
            .map(([granularity, column]) => `${granularity}→${column}`)
            .join(" · ") || "（columns 为空）"}
        </span>
      </Space>
    );
  return (
    <Descriptions size="small" column={1}>
      <Descriptions.Item label="时间维声明（time_dimension）">{timeDimension}</Descriptions.Item>
      <Descriptions.Item label="血缘来源表（lineage.source_tables）">
        {item.lineage.source_tables.length === 0 ? "—" : item.lineage.source_tables.join("、")}
      </Descriptions.Item>
      <Descriptions.Item label="治理复核周期（review_cycle_days，天）">
        {dashOr(item.governance.review_cycle_days)}
      </Descriptions.Item>
    </Descriptions>
  );
}

export default function ModelsSection({ state }: Props) {
  return (
    <SectionData
      title="语义模型"
      state={state}
      render={(items) => (
        <Table<ModelsItem>
          rowKey="domain"
          size="small"
          pagination={false}
          scroll={{ x: "max-content" }}
          dataSource={items}
          expandable={{ expandedRowRender: (item) => <ModelDetail item={item} /> }}
          columns={[
            {
              title: "域",
              dataIndex: "domain",
              render: (domain: string) => <Tag color="blue">{domain}</Tag>,
            },
            { title: "模型", dataIndex: "model_name" },
            {
              title: "来源文件",
              dataIndex: "source_file",
              render: (value: string) => <Text code>{value}</Text>,
            },
            { title: "数据集", dataIndex: "datasets" },
            { title: "指标", dataIndex: "metrics" },
            { title: "关系", dataIndex: "relationships" },
            { title: "字段", dataIndex: "fields" },
            {
              title: "默认行级策略",
              render: (_, item) => dashOr(item.policy.default_row_policy),
            },
            {
              title: "治理（owner · version · status）",
              render: (_, item) =>
                `${dashOr(item.governance.owner)} · ${dashOr(item.governance.version)} · ${dashOr(
                  item.governance.status,
                )}`,
            },
            {
              title: "新鲜度（schedule · SLA 分钟）",
              render: (_, item) =>
                `${dashOr(item.freshness.schedule)} · ${dashOr(item.freshness.sla_minutes)}`,
            },
          ]}
        />
      )}
    />
  );
}
