/**
 * 治理子页 8：快照清单（snapshots）——P3。
 *
 * 消费端点 8（`/governance/snapshots`，全域一次返回，无钻取）。
 *
 * §3.3 第 6 行的渲染义务：`is_latest_by_created_at` 为 true 的行必须出现
 * 「最新（按 created_at）」徽标（LATEST_SNAPSHOT_BADGE 单一文案出口）——
 * 列表已由后端按 created_at 降序排序，前端**不得**按文件名排序后给首行加徽标
 * （0019 背景的实测陷阱：字典序最大 ≠ created_at 最新）。
 *
 * `bound_to_head` 是本行 sha 与当前 HEAD 的相等事实（服务端比较），如实展示
 * 两个方向，不做「已过期」之类的引申断言。
 */
import { Space, Table, Tag, Typography } from "antd";

import type { SnapshotsItem } from "../../api/types";
import { LATEST_SNAPSHOT_BADGE } from "../../lib/honesty";
import { shortSha } from "../../lib/sha";

import { dashOr, formatBytes, SectionData, type CollectionState } from "./GovernanceSection";

const { Text } = Typography;

interface Props {
  state: CollectionState<SnapshotsItem>;
}

/** data_range 契约内为字符串；契约外形态 JSON 原文兜底（不编造解释）。 */
function dataRangeText(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** namespaces（命名空间 → 行数）如实平铺；空对象 → 「—」。 */
function namespacesText(namespaces: Record<string, number>): string {
  const entries = Object.entries(namespaces);
  if (entries.length === 0) {
    return "—";
  }
  return entries.map(([namespace, rows]) => `${namespace}=${rows}`).join(" · ");
}

export default function SnapshotsSection({ state }: Props) {
  return (
    <SectionData
      title="快照清单"
      state={state}
      render={(items) => (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Text type="secondary">
            列表由后端按 created_at 降序排列；「最新」徽标由 is_latest_by_created_at
            标志驱动，不按文件名排序推断（文件名字典序 ≠ created_at 序，0019 实测陷阱）。
          </Text>
          <Table<SnapshotsItem>
            rowKey="sha"
            size="small"
            pagination={false}
            scroll={{ x: "max-content" }}
            dataSource={items}
            locale={{ emptyText: "（无快照）" }}
            columns={[
              {
                title: "sha",
                dataIndex: "sha",
                render: (value: string, row: SnapshotsItem) => (
                  <Space size={4}>
                    <Text code title={value}>
                      {shortSha(value)}
                    </Text>
                    {row.is_latest_by_created_at && <Tag color="gold">{LATEST_SNAPSHOT_BADGE}</Tag>}
                  </Space>
                ),
              },
              {
                title: "created_at",
                dataIndex: "created_at",
                render: (value: string | null) => dashOr(value),
              },
              {
                title: "来源（source）",
                dataIndex: "source",
                render: (value: string | null) => dashOr(value),
              },
              {
                title: "数据范围（data_range）",
                dataIndex: "data_range",
                render: (value: unknown) => dataRangeText(value),
              },
              {
                title: "与 HEAD",
                dataIndex: "bound_to_head",
                render: (value: boolean) =>
                  value ? <Tag color="green">与 HEAD 一致</Tag> : <Tag>与 HEAD 不一致</Tag>,
              },
              { title: "表数", dataIndex: "table_count" },
              {
                title: "总行数",
                dataIndex: ["row_counts", "total_rows"],
                render: (value: number) => value.toLocaleString("en-US"),
              },
              {
                title: "命名空间行数（row_counts.namespaces）",
                dataIndex: ["row_counts", "namespaces"],
                render: (value: Record<string, number>) => namespacesText(value),
              },
              {
                title: "原始大小",
                dataIndex: "raw_size_bytes",
                render: (value: number | null) => (value === null ? "—" : formatBytes(value)),
              },
            ]}
          />
        </Space>
      )}
    />
  );
}
