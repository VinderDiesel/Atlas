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
  // 列键用位序（c0/c1/…）：列名可能在结果集中重复（别名撞车），以名字为键会静默丢列
  const data = rows.slice(0, renderedRows).map((row, index) => {
    const record: Record<string, unknown> = { __key: String(index) };
    columns.forEach((_, ci) => {
      record[`c${ci}`] = row[ci];
    });
    return record;
  });
  return (
    <Table
      size="small"
      rowKey="__key"
      pagination={false}
      scroll={{ x: "max-content", y: 480 }}
      columns={columns.map((name, ci) => ({
        title: name,
        dataIndex: `c${ci}`,
        key: `c${ci}`,
        ellipsis: true,
        render: (value: unknown) => (value === null ? "NULL" : String(value)),
      }))}
      dataSource={data}
    />
  );
}
