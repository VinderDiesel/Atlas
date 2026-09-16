/**
 * 治理子页 3：维度（dimensions）——P2。
 *
 * 消费端点 3（`/governance/dimensions?model=`，域切换重拉）。`value_domain`
 * 是**注册状态索引**（registered / skipped / none——none = 无值域文件），
 * 原样标签展示：值域清单（端点 5）里没有该字段时这里显示 none，两页互证。
 */
import { Table, Tag, Typography } from "antd";

import type { DimensionsItem } from "../../api/types";

import { dashOr, SectionData, type CollectionState } from "./GovernanceSection";

const { Text } = Typography;

interface Props {
  state: CollectionState<DimensionsItem>;
}

function statusColor(value: string): string {
  if (value === "registered") {
    return "green";
  }
  if (value === "skipped") {
    return "orange";
  }
  return "default";
}

export default function DimensionsSection({ state }: Props) {
  return (
    <SectionData
      title="维度"
      state={state}
      render={(items) => (
        <Table<DimensionsItem>
          rowKey={(item) => `${item.dataset}.${item.field}`}
          size="small"
          pagination={false}
          scroll={{ x: "max-content" }}
          dataSource={items}
          columns={[
            { title: "数据集（dataset）", dataIndex: "dataset" },
            { title: "维度（field）", dataIndex: "field" },
            {
              title: "物理列（physical）",
              dataIndex: "physical",
              render: (value: string) => <Text code>{value}</Text>,
            },
            {
              title: "时间维（is_time）",
              dataIndex: "is_time",
              render: (value: boolean) => (value ? "是" : "否"),
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
            {
              title: "值域注册状态（value_domain）",
              dataIndex: "value_domain",
              render: (value: string) => <Tag color={statusColor(dashOr(value))}>{value}</Tag>,
            },
          ]}
        />
      )}
    />
  );
}
