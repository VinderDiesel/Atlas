/**
 * 治理子页 7：评测报告（reports）——P3。
 *
 * 消费端点 7（`/governance/reports`，全域一次返回，只解析主报告 body）。
 * §3.3 第 2/3 行的渲染义务：
 * - 分组由 `structured` 驱动（partitionReports；主报告 / 降级组均默认展开）——
 *   前端不得按文件名模式自行分类（模式标签是展示列，不是分类依据）；
 * - 主报告组含 body 解析出的 4 键（sha / created_at / dry / domains）；
 *   **dry 标签列**必须出现（dryNote 文案：dry 运行的 ex:"n/a" 不得被读成 EX=0）；
 * - 非主报告**按「原始 JSON + 模式标签」降级**（0018 代价 ⑥）：列表只展示
 *   文件元信息与 pattern，原始 JSON 在 ReportDrawer 里展开——不得伪造统一表头、
 *   不把 raw 摊平成表格列。
 *
 * 钻取（`/governance/reports/{name}`）走 ReportDrawer；name 即文件 stem。
 */
import { Button, Collapse, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import type { ReportsItem } from "../../api/types";
import { dryNote, partitionReports } from "../../lib/honesty";
import { shortSha } from "../../lib/sha";

import { dashOr, formatBytes, SectionData, type CollectionState } from "./GovernanceSection";
import ReportDrawer from "./ReportDrawer";

const { Text } = Typography;

interface Props {
  state: CollectionState<ReportsItem>;
  /** 治理端点一律 Bearer（实测 401）；钻取 drawer 复用同一 token。 */
  token: string;
}

function FileCell({ name }: { name: string }) {
  return (
    <Text code title={name}>
      {name}
    </Text>
  );
}

function DrillCell({ name, onDrill }: { name: string; onDrill: (name: string) => void }) {
  return (
    <Button size="small" onClick={() => onDrill(name)}>
      查看
    </Button>
  );
}

function MainReportsTable({
  rows,
  onDrill,
}: {
  rows: ReportsItem[];
  onDrill: (name: string) => void;
}) {
  return (
    <Table<ReportsItem>
      rowKey="name"
      size="small"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={rows}
      locale={{ emptyText: "（无主报告）" }}
      columns={[
        { title: "文件", dataIndex: "name", render: (name: string) => <FileCell name={name} /> },
        {
          title: "sha",
          dataIndex: "sha",
          render: (value: string | null | undefined) =>
            value === null || value === undefined ? (
              "—"
            ) : (
              <Text code title={value}>
                {shortSha(value)}
              </Text>
            ),
        },
        { title: "created_at", dataIndex: "created_at", render: (v: string | null | undefined) => dashOr(v) },
        {
          title: "dry",
          dataIndex: "dry",
          render: (value: boolean | null | undefined, row: ReportsItem) => {
            const note = dryNote({ structured: row.structured, pattern: row.pattern, dry: value });
            return note === null ? (
              dashOr(value)
            ) : (
              <Tag color="orange">{note}</Tag>
            );
          },
        },
        {
          title: "域",
          dataIndex: "domains",
          render: (value: string[] | null | undefined) =>
            value === null || value === undefined ? "—" : value.join(" / "),
        },
        { title: "大小", dataIndex: "size_bytes", render: (v: number) => formatBytes(v) },
        {
          title: "钻取",
          render: (_: unknown, row: ReportsItem) => <DrillCell name={row.name} onDrill={onDrill} />,
        },
      ]}
    />
  );
}

function DegradedReportsTable({
  rows,
  onDrill,
}: {
  rows: ReportsItem[];
  onDrill: (name: string) => void;
}) {
  return (
    <Table<ReportsItem>
      rowKey="name"
      size="small"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={rows}
      locale={{ emptyText: "（无降级报告）" }}
      columns={[
        { title: "文件", dataIndex: "name", render: (name: string) => <FileCell name={name} /> },
        {
          title: "模式标签（pattern）",
          dataIndex: "pattern",
          render: (value: string) => <Text code>{value}</Text>,
        },
        { title: "大小", dataIndex: "size_bytes", render: (v: number) => formatBytes(v) },
        { title: "mtime", dataIndex: "mtime", render: (v: string) => v },
        {
          title: "钻取",
          render: (_: unknown, row: ReportsItem) => <DrillCell name={row.name} onDrill={onDrill} />,
        },
      ]}
    />
  );
}

export default function ReportsSection({ state, token }: Props) {
  const [drillName, setDrillName] = useState<string | null>(null);
  return (
    <>
      <SectionData
        title="评测报告"
        state={state}
        render={(items) => {
          const { main, degraded } = partitionReports(items);
          return (
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Text type="secondary">
                {`共 ${items.length} 份 · 主报告 ${main.length} 份（统一结构）· 非主报告 ${degraded.length} 份（原始 JSON + 模式标签降级）`}
              </Text>
              <Collapse
                defaultActiveKey={["main", "degraded"]}
                items={[
                  {
                    key: "main",
                    label: (
                      <Space size={8}>
                        <span>主报告（structured）</span>
                        <Tag color="green">{main.length}</Tag>
                      </Space>
                    ),
                    children: <MainReportsTable rows={main} onDrill={setDrillName} />,
                  },
                  {
                    key: "degraded",
                    label: (
                      <Space size={8}>
                        <span>非主报告（降级：原始 JSON + 模式标签）</span>
                        <Tag color="orange">{degraded.length}</Tag>
                      </Space>
                    ),
                    children: <DegradedReportsTable rows={degraded} onDrill={setDrillName} />,
                  },
                ]}
              />
            </Space>
          );
        }}
      />
      <ReportDrawer name={drillName} token={token} onClose={() => setDrillName(null)} />
    </>
  );
}
