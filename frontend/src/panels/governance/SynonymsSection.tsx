/**
 * 治理子页 4：locale 词典（synonyms）——P2。
 *
 * 消费端点 4（`/governance/synonyms?locale=`；locale 切换重拉一条）。空占位
 * 横幅是渲染义务（§3.3 第 4 行）：`empty_placeholder == true` 时**直接引用
 * `authority_note` 原文**（权威源在 ossie 的 ai_context.synonyms）——禁止显示
 * 「中文暂无同义词」（那是错的）。判定与文案由 lib/honesty.ts 的
 * synonymsBanner 承担（vitest 断言对象），本组件只负责渲染。
 *
 * `patterns` 是 YAML **原文结构**（服务端不编译为正则），按分节 JSON 序列化
 * 原样展示——不拆解、不改写。
 */
import { Alert, Collapse, Space, Table, Tag, Typography } from "antd";

import type { SynonymsItem } from "../../api/types";
import { synonymsBanner } from "../../lib/honesty";

import { dashOr, SectionData, type CollectionState } from "./GovernanceSection";

const { Text } = Typography;

interface Props {
  state: CollectionState<SynonymsItem>;
}

interface SynonymRow {
  key: string;
  values: string[];
}

function synonymRows(mapping: Record<string, string[]>): SynonymRow[] {
  return Object.entries(mapping).map(([key, values]) => ({ key, values }));
}

function SynonymTable({ rows, emptyText }: { rows: SynonymRow[]; emptyText: string }) {
  return (
    <Table<SynonymRow>
      rowKey="key"
      size="small"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={rows}
      locale={{ emptyText }}
      columns={[
        { title: "键", dataIndex: "key" },
        {
          title: "同义词",
          dataIndex: "values",
          render: (values: string[]) => (
            <Space wrap size={4}>
              {values.map((value) => (
                <Tag key={value}>{value}</Tag>
              ))}
            </Space>
          ),
        },
      ]}
    />
  );
}

export default function SynonymsSection({ state }: Props) {
  return (
    <SectionData
      title="locale 词典"
      state={state}
      render={(items) => {
        if (items.length === 0) {
          return <Text type="secondary">（响应 items 为空——服务端未给出词典载荷）</Text>;
        }
        const item = items[0];
        const banner = synonymsBanner(item);
        const patternEntries = Object.entries(item.patterns);
        return (
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            {banner !== null && (
              <Alert type="warning" showIcon message="空占位词典（empty_placeholder）" description={banner} />
            )}
            {banner === null && (
              <Text type="secondary">{`权威源说明（authority_note）：${dashOr(item.authority_note)}`}</Text>
            )}
            <Text strong>指标同义词（metric_synonyms）</Text>
            <SynonymTable
              rows={synonymRows(item.metric_synonyms)}
              emptyText="（空——见上方横幅说明）"
            />
            <Text strong>维度同义词（dimension_synonyms）</Text>
            <SynonymTable
              rows={synonymRows(item.dimension_synonyms)}
              emptyText="（空——见上方横幅说明）"
            />
            <Text strong>形态词分节（patterns，YAML 原文结构；服务端不编译为正则）</Text>
            {patternEntries.length === 0 ? (
              <Text type="secondary">（无形态词分节）</Text>
            ) : (
              <Collapse
                size="small"
                items={patternEntries.map(([key, value]) => ({
                  key,
                  label: key,
                  children: (
                    <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                      {JSON.stringify(value, null, 2)}
                    </pre>
                  ),
                }))}
              />
            )}
          </Space>
        );
      }}
    />
  );
}
