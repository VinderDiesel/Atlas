/**
 * SQL 预览（answer 段 ③「出口 SQL」与「只编译不执行」结果共用）。
 *
 * 可折叠但**默认展开**（设计页 §3.4：不折叠等于丢失，折到底等于藏起来——默认
 * 展开是 0018 ⑦ 与诚实性口径的折中）。sql 为 null（非 answer 轮）如实标注。
 */
import { App as AntdApp, Button, Collapse, Typography } from "antd";

interface Props {
  sql: string | null;
  /** 折叠面板标题（answer 轮与只编译轮各自标注来源）。 */
  title: string;
}

export default function SqlPreview({ sql, title }: Props) {
  const { message } = AntdApp.useApp();

  async function copySql(): Promise<void> {
    if (sql === null) return;
    try {
      await navigator.clipboard.writeText(sql);
      void message.success("SQL 已复制");
    } catch {
      void message.error("复制失败（浏览器不支持剪贴板 API）");
    }
  }

  return (
    <Collapse
      defaultActiveKey={["sql"]}
      items={[
        {
          key: "sql",
          label: `${title}${sql === null ? "（无）" : ""}`,
          extra: sql !== null ? (
            <Button
              type="text"
              size="small"
              onClick={(e) => { e.stopPropagation(); void copySql(); }}
              aria-label="复制 SQL"
            >
              复制
            </Button>
          ) : undefined,
          children:
            sql === null ? (
              <Typography.Text type="secondary">本轮无 SQL</Typography.Text>
            ) : (
              <pre className="atlas-pre" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                {sql}
              </pre>
            ),
        },
      ]}
    />
  );
}
