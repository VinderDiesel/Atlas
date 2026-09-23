/**
 * 认证面请求封装（ADR-0031 D02/D13；T03d 前端登录入口）。
 *
 * 与旧 `api/client.ts` 的关系：旧封装错误体取 `{"detail":…}`（FastAPI 默认）；
 * 新控制 API（D13）统一 `{"error":{code,message,request_id}}`——本文件按新体
 * 解析（`readAuthError`），并把响应投影为**判别联合** `SessionProbe`，让
 * 「演示模式 / 私有未登录 / 配置阻塞 / 已登录」在类型层穷尽（不猜测模式）。
 *
 * 纪律：
 * - 路径只从 `api/endpoints.ts` 取（判据 9 防漂移前提）；
 * - **不返回、不存储 IdP token**：会话是不透明 HttpOnly Cookie（D02 ③），
 *   浏览器侧只有 `GET /auth/session` 的身份投影与 CSRF token（内存态）；
 * - `logoutSession` 是写请求：CSRF 头 `X-Atlas-CSRF`（token 由 session 响应下发）；
 *   同源 Origin 由浏览器自动携带，前端不伪造。
 */
import { ApiError } from "./client";
import { API } from "./endpoints";

/** CSRF 头名（后端 serving/control/routes.py 的 `CSRF_HEADER`，两端同名字面量）。 */
export const CSRF_HEADER = "X-Atlas-CSRF";

/** `GET /auth/session` 200 响应体（D13：不返回 IdP token）。 */
export interface AuthSessionPayload {
  subject: string;
  issuer: string;
  role: string;
  capabilities: string[];
  scopes: string[];
  csrf_token: string;
  /** 会话到期时刻（epoch 秒；后端 int）。 */
  expires_at: number;
}

/**
 * 会话探测投影（前端登录入口的唯一模式判定源）：
 * - `demo`：503 `oidc_not_configured`（ATLAS_OIDC_* 全空，演示模式）；
 * - `blocked`：其余 503（配置不全 / 授予非法）——显示配置阻塞，**不伪装登录成功**；
 * - `anonymous`：401（未登录 / 会话过期 / 双凭据冲突）；
 * - `authenticated`：200；
 * - `unreachable`：网络层失败（不猜测模式，交调用侧给行动入口）。
 */
export type SessionProbe =
  | { kind: "demo" }
  | { kind: "blocked"; code: string | null; message: string }
  | { kind: "anonymous" }
  | { kind: "authenticated"; session: AuthSessionPayload }
  | { kind: "unreachable"; message: string };

/** 统一错误体解析结果（D13；缺字段不编造，回落状态码文案）。 */
export interface AuthErrorBody {
  code: string | null;
  message: string;
}

/**
 * 解析统一错误体 `{"error":{code,message,request_id}}`（纯函数）。
 *
 * 非该形态（代理页 / FastAPI 默认体 / 空体）→ `code=null` + 状态码回落文案：
 * 不把底层原文当 message 冒充，也不让调用侧把解析失败误判为某一具体原因。
 */
export function readAuthError(body: unknown, status: number): AuthErrorBody {
  const error = (body as { error?: { code?: unknown; message?: unknown } } | null)?.error;
  return {
    code: typeof error?.code === "string" ? error.code : null,
    message:
      typeof error?.message === "string"
        ? error.message
        : `HTTP ${status}（响应体不是统一错误体，原文不展示）`,
  };
}

async function readJsonOrNull(res: Response): Promise<unknown> {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

/** 探测会话：GET /auth/session → SessionProbe（判别联合，穷尽模式）。 */
export async function probeSession(): Promise<SessionProbe> {
  let res: Response;
  try {
    res = await fetch(API.authSession, { method: "GET" });
  } catch (error) {
    return {
      kind: "unreachable",
      message: error instanceof Error ? error.message : String(error),
    };
  }
  if (res.status === 200) {
    const body = await readJsonOrNull(res);
    if (body === null || typeof body !== "object") {
      // 代理页/非 JSON 响应不得被当成已登录会话（不伪造身份）
      return { kind: "unreachable", message: "session 响应不是 JSON 对象" };
    }
    return { kind: "authenticated", session: body as AuthSessionPayload };
  }
  const { code, message } = readAuthError(await readJsonOrNull(res), res.status);
  if (res.status === 503 && code === "oidc_not_configured") {
    return { kind: "demo" };
  }
  if (res.status === 503) {
    return { kind: "blocked", code, message };
  }
  return { kind: "anonymous" };
}

/**
 * 退出登录：POST /auth/logout（CSRF 头必带；服务端校验同源 Origin + CSRF）。
 *
 * 204 成功（Cookie 已由服务端清除）；失败抛 `ApiError`（detail = 统一错误体
 * message 原文，不塞原始 JSON）。调用侧负责成功后刷新到匿名态。
 */
export async function logoutSession(csrfToken: string): Promise<void> {
  const res = await fetch(API.authLogout, {
    method: "POST",
    headers: { [CSRF_HEADER]: csrfToken },
  });
  if (res.ok) {
    return;
  }
  const { message } = readAuthError(await readJsonOrNull(res), res.status);
  throw new ApiError(res.status, message, null);
}
