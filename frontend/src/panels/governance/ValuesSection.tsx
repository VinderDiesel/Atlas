/**
 * 治理子页 5：值域注册表（values）——P2。
 *
 * 消费端点 5（`/governance/values`，全域一次返回）。§3.3 第 1 行的渲染义务：
 * skipped 组**默认展开**且显示 `skip_reason` 原文——分组由 `partitionValueDomains`
 * 承担（status 驱动，与 values_count 无关），「默认展开」由两组面板的
 * `defaultActiveKey` 承担；禁止平铺、禁止折叠进「高级／更多」。
 *
 * 钻取（`/governance/values/{item}`，返回裸对象非信封）走 ValuesDrawer；
 * 条目键形态为 `<model>.<field>`（实测文件名 `atlas_finance_analytics.Branch.json`）。
 * model/field 为 null 的条目没有钻取键——按钮禁用并说明原因，不猜键。
 */
import { Button, Collapse, Space, Table, Tag, Tooltip, Typography } from "antd";
import { useState } from "react";

import type { ValuesItem } from "../../api/types";
import { partitionValueDomains, valueSkipNote } from "../../lib/honesty";
import { shortSha } from "../../lib/sha";

import { dashOr, SectionData, type CollectionState } from "./GovernanceSection";
import ValuesDrawer from "./ValuesDrawer";

const { Text } = Typography;

interface Props {
  state: CollectionState<ValuesItem>;
  /** 治理端点一律 Bearer（实测 401）；钻取 drawer 复用同一 token。 */
  token: string;
}

/** `<model>.<field>`；缺一即无钻取键（端点正则不接受其他形态）。 */
function drillKeyOf(row: ValuesItem): string | null {
  return row.model === null || row.field === null ? null : `${row.model}.${row.field}`;
}

function rowKeyOf(row: ValuesItem): string {
  return [row.model, row.field, row.source_column, row.bound_dataset].join("\u0000");
}

const SOURCE_COLUMNS = [
  { title: "模型", dataIndex: "model", render: (value: string | null) => dashOr(value) },
  { title: "字段", dataIndex: "field", render: (value: string | null) => dashOr(value) },
] as const;

const META_COLUMNS = [
  {
    title: "快照 sha",
    dataIndex: "snapshot_sha",
    render: (value: string | null) =>
      value === null ? (
        "—"
      ) : (
        <Text code title={value}>
          {shortSha(value)}
        </Text>
      ),
  },
  {
    title: "生成时间",
    dataIndex: "generated_at",
    render: (value: string | null) => dashOr(value),
  },
  {
    title: "绑定数据集",
    dataIndex: "bound_dataset",
    render: (value: string | null) => dashOr(value),
  },
  {
    title: "来源（table.column）",
    render: (_: unknown, row: ValuesItem) =>
      row.source_table === null && row.source_column === null
        ? "—"
        : `${dashOr(row.source_table)}.${dashOr(row.source_column)}`,
  },
] as const;

function ValueTable({
  rows,
  showSkipReason,
  onDrill,
}: {
  rows: ValuesItem[];
  showSkipReason: boolean;
  onDrill: (item: string) => void;
}) {
  return (
    <Table<ValuesItem>
      rowKey={rowKeyOf}
      size="small"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={rows}
      locale={{ emptyText: showSkipReason ? "（无 skipped 条目——全部已覆盖）" : "（无已覆盖条目）" }}
      columns={[
        ...SOURCE_COLUMNS,
        ...(showSkipReason
          ? [
              {
                title: "跳过原因（skip_reason 原文）",
                render: (_: unknown, row: ValuesItem) => (
                  <Text type="warning">{valueSkipNote(row)}</Text>
                ),
              },
            ]
          : []),
        {
          title: "值个数",
          dataIndex: "values_count",
          render: (value: number) => value,
        },
        ...META_COLUMNS,
        {
          title: "钻取",
          render: (_: unknown, row: ValuesItem) => {
            const key = drillKeyOf(row);
            if (key === null) {
              return (
                <Tooltip title="条目缺 model / field，无钻取键（不猜）">
                  <span>
                    <Button size="small" disabled>
                      查看明细
                    </Button>
                  </span>
                </Tooltip>
              );
            }
            return (
              <Button size="small" onClick={() => onDrill(key)}>
                查看明细
              </Button>
            );
          },
        },
      ]}
    />
  );
}

export default function ValuesSection({ state, token }: Props) {
  const [drillItem, setDrillItem] = useState<string | null>(null);
  return (
    <>
      <SectionData
        title="值域注册表"
        state={state}
        render={(items) => {
          const { covered, skipped } = partitionValueDomains(items);
          return (
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Text type="secondary">
                {`已覆盖 ${covered.length} 条 · 已跳过 ${skipped.length} 条（两组均默认展开；skipped 原因引用 skip_reason 原文）`}
              </Text>
              <Collapse
                defaultActiveKey={["covered", "skipped"]}
                items={[
                  {
                    key: "covered",
                    label: (
                      <Space size={8}>
                        <span>已覆盖</span>
                        <Tag color="green">{covered.length}</Tag>
                      </Space>
                    ),
                    children: (
                      <ValueTable rows={covered} showSkipReason={false} onDrill={setDrillItem} />
                    ),
                  },
                  {
                    key: "skipped",
                    label: (
                      <Space size={8}>
                        <span>已跳过</span>
                        <Tag color="orange">{skipped.length}</Tag>
                      </Space>
                    ),
                    children: (
                      <ValueTable rows={skipped} showSkipReason={true} onDrill={setDrillItem} />
                    ),
                  },
                ]}
              />
            </Space>
          );
        }}
      />
      <ValuesDrawer item={drillItem} token={token} onClose={() => setDrillItem(null)} />
    </>
  );
}
