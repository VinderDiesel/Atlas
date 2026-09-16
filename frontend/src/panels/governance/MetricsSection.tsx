/**
 * 治理子页 2：指标（metrics）——P2。
 *
 * 消费端点 2（`/governance/metrics?model=`，域切换重拉）。FIBO 对齐的实测形态
 * （2026-09-16）：`fibo_alignment.mappings` 恒 0|1 键（`metric:<name>`，条目在
 * **模型级** mappings 下）——0 键如实显示「无对齐条目」，不补造。
 * 明细（血缘 / 黄金用例 / 对齐字段全文）走行展开。
 */
import { Descriptions, Space, Table, Tag, Typography } from "antd";

import type { FiboMappingEntry, MetricsItem } from "../../api/types";

import { dashOr, SectionData, type CollectionState } from "./GovernanceSection";

const { Text } = Typography;

interface Props {
  state: CollectionState<MetricsItem>;
}

function mappingsOf(item: MetricsItem): [string, FiboMappingEntry][] {
  return Object.entries(item.fibo_alignment.mappings);
}

function joinOrDash(list: readonly string[]): string {
  return list.length === 0 ? "—" : list.join("、");
}

function MetricsDetail({ item }: { item: MetricsItem }) {
  const mappings = mappingsOf(item);
  return (
    <Descriptions size="small" column={1}>
      <Descriptions.Item label="血缘列（lineage.source_columns）">
        {joinOrDash(item.lineage.source_columns)}
      </Descriptions.Item>
      <Descriptions.Item label="黄金用例（quality.gold_test_cases）">
        {joinOrDash(item.quality.gold_test_cases)}
      </Descriptions.Item>
      <Descriptions.Item label="期望值快照（quality.expected_value_snapshot_sha）">
        {dashOr(item.quality.expected_value_snapshot_sha)}
      </Descriptions.Item>
      <Descriptions.Item label="supersedes（被替代指标）">
        {dashOr(item.supersedes)}
      </Descriptions.Item>
      <Descriptions.Item label="FIBO 对齐明细（mappings 全文）">
        {mappings.length === 0 ? (
          <Text type="secondary">无对齐条目（mappings 为空——不补造）</Text>
        ) : (
          <Space direction="vertical" size="small">
            {mappings.map(([key, entry]) => (
              <Space key={key} direction="vertical" size={0}>
                <Text code>{key}</Text>
                <span>{`concept=${entry.concept}`}</span>
                <span>
                  {`match_type=${entry.match_type} · confidence=${entry.confidence} · verified_at=${entry.verified_at}`}
                </span>
                {entry.note !== undefined && <span>{`note=${entry.note}`}</span>}
              </Space>
            ))}
          </Space>
        )}
      </Descriptions.Item>
    </Descriptions>
  );
}

export default function MetricsSection({ state }: Props) {
  return (
    <SectionData
      title="指标"
      state={state}
      render={(items) => (
        <Table<MetricsItem>
          rowKey="name"
          size="small"
          pagination={false}
          scroll={{ x: "max-content" }}
          dataSource={items}
          expandable={{ expandedRowRender: (item) => <MetricsDetail item={item} /> }}
          columns={[
            { title: "指标", dataIndex: "name" },
            {
              title: "表达式",
              dataIndex: "expression",
              render: (value: string) => <Text code>{value}</Text>,
            },
            {
              title: "描述",
              dataIndex: "description",
              render: (value: string | null) => dashOr(value),
            },
            {
              title: "同义词",
              dataIndex: "synonyms",
              render: (list: string[]) =>
                list.length === 0 ? (
                  "—"
                ) : (
                  list.map((synonym) => <Tag key={synonym}>{synonym}</Tag>)
                ),
            },
            { title: "owner", dataIndex: "owner", render: (value: string | null) => dashOr(value) },
            {
              title: "version",
              dataIndex: "version",
              render: (value: number | null) => dashOr(value),
            },
            {
              title: "status",
              dataIndex: "status",
              render: (value: string | null) => dashOr(value),
            },
            {
              title: "FIBO 对齐",
              render: (_, item) => {
                const mappings = mappingsOf(item);
                if (mappings.length === 0) {
                  return <Text type="secondary">无对齐条目</Text>;
                }
                const entry = mappings[0][1];
                return (
                  <Space direction="vertical" size={0}>
                    <Text style={{ maxWidth: 300 }} ellipsis={{ tooltip: entry.concept }}>
                      {entry.concept}
                    </Text>
                    <Text type="secondary">{`${entry.match_type} · ${entry.confidence}`}</Text>
                  </Space>
                );
              },
            },
          ]}
        />
      )}
    />
  );
}
