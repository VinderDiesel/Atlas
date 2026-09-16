/**
 * 请求失败的错误详情（原文透传；0018 落地注记「前端零遥测」的渲染义务：
 * 不美化、不摘要、不吞错）。
 *
 * `ApiError`：HTTP status + detail 原文 + 429 的 Retry-After 秒差；
 * 其余（网络层异常等）：`Error.message` 或 `String(e)` 原样。
 */
import { Alert, Space } from "antd";

import { ApiError } from "../api/client";

interface Props {
  error: unknown;
  /** 标题前缀（默认「请求失败」；调用方可写明语境，如「角色清单加载失败」）。 */
  title?: string;
}

export default function ErrorNote({ error, title = "请求失败" }: Props) {
  if (error instanceof ApiError) {
    return (
      <Alert
        type="error"
        showIcon
        message={`${title}（错误原文透传）：HTTP ${error.status}`}
        description={
          <Space direction="vertical" size={0}>
            <span>{error.detail}</span>
            {error.retryAfterSeconds !== null && (
              <span>限流（429）：请 {error.retryAfterSeconds} 秒后重试（Retry-After 头）</span>
            )}
          </Space>
        }
      />
    );
  }
  return (
    <Alert
      type="error"
      showIcon
      message={title}
      description={<span>{error instanceof Error ? error.message : String(error)}</span>}
    />
  );
}
