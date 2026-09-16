/**
 * 面板 5：图表区块（工作台内嵌组件区，数据表与截断声明之后；0018 ⑦ / 设计页 §3.4 ⑥）。
 *
 * 消费 `/api/v1/ask`（与 `/plan/execute`）响应的独立 `chart` 键——**spec 级
 * 确定性渲染**（ADR-0025 决策 ①②；本组件零图表类型决策）：
 * - `chart === null` 不渲染占位、不推断原因（设计页 §3.4 ⑥：只能照实展示后端
 *   给的 note，而不是把「画不了」伪装成空图）；
 * - 三分支直接建映射：`bar` → BarChart / `line` → LineChart / `table` → 降级
 *   表格（note 原文 + skipped 计数必渲染，工作项 6 前端半侧）；
 * - 契约外 `type` 原样展示原始 JSON（不猜分支，同 AskWorkbench 的未知 kind 处理）；
 * - 轴键与数值转换走 lib/chart.ts 纯函数（vitest 断言对象，§6.1 前端级约束）。
 *
 * 折线语义（chart.py）：按行序连线，不排序不插值；NULL 点保留为空
 * （connectNulls=false，缺口如实）。
 */
import { Alert, Space, Table, Typography } from "antd";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ChartBarSpec, ChartLineSpec, ChartSpec, ChartTableSpec } from "../../api/types";
import { chartSeries, tableFallbackLines } from "../../lib/chart";

const { Text } = Typography;

/** 图表描边主色（AntD 主色，与顶栏视觉一致）。 */
const SERIES_COLOR = "#1677ff";

function footerNote(spec: { x?: string; y?: string[]; sql_sha256: string }): string {
  const parts: string[] = [];
  if (spec.x !== undefined) {
    parts.push(`x=${spec.x}`);
  }
  if (spec.y !== undefined) {
    parts.push(`y=${spec.y.join(", ")}`);
  }
  parts.push(`出口 SQL 摘要 ${spec.sql_sha256}`);
  return parts.join(" · ");
}

/**
 * bar/line 分支：Recharts 折线/柱状（轴键零推断，见 lib/chart.ts）。
 *
 * **行键恒为 "x"/"y"**（chart.py 的 data 行形状 `{"x", "y"}`）：Recharts 的
 * dataKey 必须用行内实际键；`spec.x` / `spec.y` 是语义列名（footer 展示用），
 * 误作 dataKey 会让全部数据点取不到值（2026-09-16 P3 走查实测：曲线无 d
 * 路径、两轴刻度为空、图面只剩网格）。
 */
function SeriesChart({ spec }: { spec: ChartBarSpec | ChartLineSpec }) {
  const { points } = chartSeries(spec);
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      {spec.note !== undefined && (
        <Alert type="info" showIcon message={spec.note} style={{ margin: 0 }} />
      )}
      <ResponsiveContainer width="100%" height={320}>
        {spec.type === "line" ? (
          <LineChart data={points}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="x" />
            {/* 宽 90：亿级刻度（9 位数字）超出 YAxis 默认 60px 会被裁
                （2026-09-16 P3 走查实测："100000000" 显示为 "0000000"） */}
            <YAxis width={90} />
            <Tooltip />
            {/* 不插值（chart.py：按行序连线）；NULL 留缺口不补 0 */}
            <Line type="linear" dataKey="y" stroke={SERIES_COLOR} connectNulls={false} />
          </LineChart>
        ) : (
          <BarChart data={points}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="x" />
            <YAxis width={90} />
            <Tooltip />
            <Bar dataKey="y" fill={SERIES_COLOR} />
          </BarChart>
        )}
      </ResponsiveContainer>
      <Text type="secondary">{footerNote(spec)}</Text>
    </Space>
  );
}

/** table 分支：note 与 skipped 的渲染义务收敛在 tableFallbackLines（0025 决策 ④）。 */
function TableFallback({ spec }: { spec: ChartTableSpec }) {
  const lines = tableFallbackLines(spec);
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Alert type="warning" showIcon message={lines[0]} style={{ margin: 0 }} />
      {lines.slice(1).map((line) => (
        <Text key={line} type="warning">
          {line}
        </Text>
      ))}
      <Table
        size="small"
        rowKey="__key"
        pagination={false}
        scroll={{ x: "max-content", y: 320 }}
        dataSource={spec.rows.map((row, rowIndex) => {
          const record: Record<string, unknown> = { __key: String(rowIndex) };
          spec.columns.forEach((_, ci) => {
            record[`c${ci}`] = row[ci];
          });
          return record;
        })}
        columns={spec.columns.map((name, ci) => ({
          title: name,
          dataIndex: `c${ci}`,
          key: `c${ci}`,
          ellipsis: true,
          // NULL 显示为 NULL（与 DataTable 同口径：空串 ≠ NULL）
          render: (value: unknown) => (value === null ? "NULL" : String(value)),
        }))}
      />
      <Text type="secondary">{footerNote(spec)}</Text>
    </Space>
  );
}

/**
 * 图表区块入口。`spec === null` 返回 null（非 answer 轮 / 渲染被拒轮——
 * chart.py 的 ChartError 已被 turn_from_state 吞掉成 null，不在这里补第二种原因）。
 */
export default function ChartBlock({ spec }: { spec: ChartSpec | null }) {
  if (spec === null) {
    return null;
  }
  switch (spec.type) {
    case "bar":
    case "line":
      return <SeriesChart spec={spec} />;
    case "table":
      return <TableFallback spec={spec} />;
    default:
      // 运行时兜底（TS 认为不可达；契约外 type 只能原样展示，不猜分支）
      return (
        <Alert
          type="warning"
          showIcon
          message="契约外图表类型（原样展示）"
          description={
            <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(spec, null, 2)}</pre>
          }
        />
      );
  }
}
