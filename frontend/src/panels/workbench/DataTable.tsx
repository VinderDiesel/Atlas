/**
 * ④ 数据表（设计页 §3.4 answer 段 ④）：columns + rows 仅渲染前 renderedRows 行
 * （前端渲染上限，判定来自 lib/honesty.ts 的 truncationState）。
 *
 * null 值显示为 NULL（与评测 result_hash 的标量规约同口径）：显示空串会把
 * 「值为 NULL」与「值为空字符串」混在一起——诚实性细则。
 */
import { Table, Typography } from "antd";

interface Props {
  columns: string[];
  rows: unknown[][];
  /** 实际渲染行数（= min(row_count, RENDER_CAP)，由调用方传入不在此重算）。 */
  renderedRows: number;
}

export default function DataTable({ columns, rows, renderedRows }: Props) {
  if (columns.length === 0) {
    return <Typography.Text type="secondary">结果无列（rows 与 columns 均为空）。</Typography.Text>;
  }
  const data = rows.slice(0, renderedRows).map((row, index) => {
    const record: Record<string, unknown> = { __key: String(index) };
    columns.forEach((_, ci) => {
      record[`c${ci}`] = row[ci];
    });
    return record;
  });
  // 数值列检测：非空值中 ≥60% 为 number 即判为数值列（右对齐 + tabular-nums）
  const numericCols = new Set<number>();
  columns.forEach((_, ci) => {
    const vals = data.map((row) => row[`c${ci}`]).filter((v) => v !== null && v !== "NULL");
    if (vals.length > 0 && vals.filter((v) => typeof v === "number").length / vals.length >= 0.6) {
      numericCols.add(ci);
    }
  });
  return (
    <Table
      size="small"
      rowKey="__key"
      pagination={false}
      aria-label="查询结果数据表"
      scroll={{ x: "max-content", y: 480 }}
      rowClassName={(_, index) => ((index ?? 0) % 2 === 1 ? "atlas-row-stripe" : "")}
      columns={columns.map((name, ci) => ({
        title: name,
        dataIndex: `c${ci}`,
        key: `c${ci}`,
        ellipsis: true,
        align: numericCols.has(ci) ? "right" : "left",
        onCell: numericCols.has(ci)
          ? () => ({ style: { fontVariantNumeric: "tabular-nums" } })
          : undefined,
        render: (value: unknown) => (value === null ? "NULL" : String(value)),
      }))}
      dataSource={data}
    />
  );
}
