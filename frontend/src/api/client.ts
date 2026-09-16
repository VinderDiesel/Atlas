/**
 * fetch 封装：Bearer 注入、429（Retry-After）识别、错误原文透传（设计页 §4.1）。
 *
 * 纪律：
 * - 路径只从 `api/endpoints.ts` 取（本文件不硬写任何路径；组件同理，0022 判据 9
 *   的防漂移前提）；
 * - 非 2xx 一律抛 `ApiError`，`message` = 后端 detail 原文（不美化不摘要——
 *   0018 落地注记「前端零遥测」的渲染义务：错误原文如实展示）；
 * - **不自动重试 429**：重试会放大限流；`Retry-After`（下一窗口起点秒差）只透传
 *   给 UI 展示，由用户决定何时再试。
 */

export class ApiError extends Error {
  readonly status: number;
  /** 后端 detail 原文（字符串直取；校验错误数组 JSON 序列化）。 */
  readonly detail: string;
  /** 429 的 Retry-After 秒差；无该头或非 429 时为 null。 */
  readonly retryAfterSeconds: number | null;

  constructor(status: number, detail: string, retryAfterSeconds: number | null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** 读错误体：FastAPI 恒为 {"detail": ...}；detail 可能是字符串或校验错误数组。 */
async function readDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
    return JSON.stringify(body);
  } catch {
    return `HTTP ${res.status}（响应体不是 JSON，无法透传原文）`;
  }
}

function parseRetryAfter(res: Response): number | null {
  const raw = res.headers.get("Retry-After");
  if (raw === null) {
    return null;
  }
  const seconds = Number.parseInt(raw, 10);
  return Number.isNaN(seconds) ? null : seconds;
}

async function request<T>(path: string, init: RequestInit, token: string | null): Promise<T> {
  const headers: Record<string, string> = {
    ...((init.headers ?? {}) as Record<string, string>),
  };
  if (token !== null && token !== "") {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    throw new ApiError(res.status, await readDetail(res), parseRetryAfter(res));
  }
  return (await res.json()) as T;
}

/** POST JSON；token 为空串则不注入 Authorization（由后端 401 语义说话）。 */
export function postJson<T>(path: string, body: unknown, token: string): Promise<T> {
  return request<T>(
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    token,
  );
}

/** GET JSON；公开面（/api/v1/health）不需 token。 */
export function getJson<T>(path: string, token: string | null = null): Promise<T> {
  return request<T>(path, { method: "GET" }, token);
}
