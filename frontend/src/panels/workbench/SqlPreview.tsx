/**
 * SQL 预览（answer 段 ③「出口 SQL」与「只编译不执行」结果共用）。
 *
 * 可折叠但**默认展开**（设计页 §3.4：不折叠等于丢失，折到底等于藏起来——默认
 * 展开是 0018 ⑦ 与诚实性口径的折中）。sql 为 null（非 answer 轮）如实标注。
 */
import { Collapse, Typography } from "antd";

interface Props {
  sql: string | null;
  /** 折叠面板标题（answer 轮与只编译轮各自标注来源）。 */
  title: string;
}

export default function SqlPreview({ sql, title }: Props) {
  return (
    <Collapse
      defaultActiveKey={["sql"]}
      items={[
        {
          key: "sql",
          label: `${title}${sql === null ? "（无）" : ""}`,
          children:
            sql === null ? (
              <Typography.Text type="secondary">本轮无 SQL</Typography.Text>
            ) : (
              <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{sql}</pre>
            ),
        },
      ]}
    />
  );
}
