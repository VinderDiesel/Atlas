/**
 * 认证面消费断言（ADR-0031 D02/D13；T03d 前端登录入口）。
 *
 * 契约（单一事实源：后端 serving/control/routes.py 四端点 + 统一错误体）：
 * - `probeSession` 三分法投影：200 → 已登录；401 → 未登录（私有模式）；
 *   503 + `oidc_not_configured` → 演示模式；其余 503 → 配置阻塞（不伪装登录成功）。
 * - **退出门的机器断言**：`authView` 仅在 `demo` 时返回 `"demo"`——开发角色选择
 *   不能出现在私有模式（probing/blocked/login/session 一律不渲染 RoleSwitcher）。
 * - `logoutSession` 写请求带 CSRF 头（`X-Atlas-CSRF`）；失败体是统一错误体
 *   `{"error":{code,message,request_id}}`，取 message 为 ApiError.detail。
 *
 * 风格与 house 一致：纯函数 + fetch 桩（node 环境，无 DOM；模式见
 * analysis-stream.test.ts 的 globalThis.fetch 替换）。
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import {
  CSRF_HEADER,
  logoutSession,
  probeSession,
  readAuthError,
  type SessionProbe,
} from "../api/auth";
import { API } from "../api/endpoints";
import { authView, blockedMessage, sessionIdentity } from "../state/auth";

const SESSION = {
  subject: "user-1",
  issuer: "https://idp.example.com",
  role: "hq_admin",
  capabilities: ["publish", "viewer"],
  scopes: [],
  csrf_token: "csrf-token-1",
  expires_at: 1_800_003_600,
};

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorBody(code: string, message: string): unknown {
  return { error: { code, message, request_id: "0".repeat(32) } };
}

/** 临时替换 globalThis.fetch，回传捕获的 url/init（house 风格同 analysis-stream）。 */
async function capture<T>(
  respond: () => Response,
  run: () => Promise<T>,
): Promise<{ result: T; url: string; init: RequestInit }> {
  const orig = globalThis.fetch;
  let url = "";
  let init: RequestInit = {};
  globalThis.fetch = ((u: RequestInfo | URL, i?: RequestInit) => {
    url = String(u);
    init = i ?? {};
    return Promise.resolve(respond());
  }) as unknown as typeof fetch;
  try {
    const result = await run();
    return { result, url, init };
  } finally {
    globalThis.fetch = orig;
  }
}

describe("probeSession（GET /api/v1/auth/session 的三分法投影）", () => {
  it("200 → authenticated，会话体逐字透传（不重组字段）", async () => {
    const { result, url, init } = await capture(
      () => jsonResponse(SESSION, 200),
      () => probeSession(),
    );
    expect(result).toEqual({ kind: "authenticated", session: SESSION });
    expect(url).toBe(API.authSession);
    expect(init.method).toBe("GET");
  });

  it("401（not_authenticated）→ anonymous（私有模式未登录）", async () => {
    const { result } = await capture(
      () => jsonResponse(errorBody("not_authenticated", "缺少会话 Cookie，请先登录"), 401),
      () => probeSession(),
    );
    expect(result).toEqual({ kind: "anonymous" });
  });

  it("503 + oidc_not_configured → demo（演示模式角色切换的唯一入口）", async () => {
    const { result } = await capture(
      () => jsonResponse(errorBody("oidc_not_configured", "私有登录未配置"), 503),
      () => probeSession(),
    );
    expect(result).toEqual({ kind: "demo" });
  });

  it("503 + oidc_config_incomplete → blocked，message 原文透传（部署者自诊）", async () => {
    const { result } = await capture(
      () =>
        jsonResponse(
          errorBody("oidc_config_incomplete", "缺少 ATLAS_OIDC_ISSUER、ATLAS_OIDC_CLIENT_ID"),
          503,
        ),
      () => probeSession(),
    );
    expect(result).toEqual({
      kind: "blocked",
      code: "oidc_config_incomplete",
      message: "缺少 ATLAS_OIDC_ISSUER、ATLAS_OIDC_CLIENT_ID",
    });
  });

  it("503 但响应体不可解析 → blocked（不因解析失败退回 demo）", async () => {
    const { result } = await capture(
      () => new Response("bad gateway", { status: 503 }),
      () => probeSession(),
    );
    expect(result).toMatchObject({ kind: "blocked", code: null });
    if (result.kind !== "blocked") {
      throw new Error("expected blocked");
    }
    expect(result.message).toContain("503");
  });

  it("200 但响应体不可解析 → unreachable（不伪造已登录会话）", async () => {
    const { result } = await capture(
      () => new Response("<html>proxy login page</html>", { status: 200 }),
      () => probeSession(),
    );
    expect(result).toMatchObject({ kind: "unreachable" });
  });

  it("网络层失败 → unreachable（不猜测模式）", async () => {
    const orig = globalThis.fetch;
    globalThis.fetch = (() => Promise.reject(new Error("connection refused"))) as typeof fetch;
    try {
      const result = await probeSession();
      expect(result).toEqual({ kind: "unreachable", message: "connection refused" });
    } finally {
      globalThis.fetch = orig;
    }
  });
});

describe("logoutSession（POST /api/v1/auth/logout）", () => {
  it("204 → 成功；请求带 CSRF 头与 POST 方法", async () => {
    const { url, init } = await capture(
      () => new Response(null, { status: 204 }),
      () => logoutSession("csrf-token-1"),
    );
    expect(url).toBe(API.authLogout);
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)[CSRF_HEADER]).toBe("csrf-token-1");
  });

  it("403 → ApiError，detail 取统一错误体的 message（不塞原始 JSON）", async () => {
    let caught: unknown = null;
    try {
      await capture(
        () => jsonResponse(errorBody("csrf_failed", "CSRF 校验失败"), 403),
        () => logoutSession("wrong"),
      );
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ApiError);
    const apiError = caught as ApiError;
    expect(apiError.status).toBe(403);
    expect(apiError.detail).toBe("CSRF 校验失败");
  });
});

describe("readAuthError（统一错误体解析）", () => {
  it("合法错误体 → code/message", () => {
    expect(readAuthError({ error: { code: "x", message: "y" } }, 503)).toEqual({
      code: "x",
      message: "y",
    });
  });

  it("非统一错误体 → code null + 状态码回落文案（不编造 message）", () => {
    const parsed = readAuthError({ detail: "FastAPI default" }, 500);
    expect(parsed.code).toBeNull();
    expect(parsed.message).toContain("500");
  });
});

describe("authView（退出门：开发角色选择不能出现在私有模式）", () => {
  it("probing/blocked/anonymous/authenticated 一律不映射到 demo", () => {
    const probes: (SessionProbe | null)[] = [
      null,
      { kind: "blocked", code: "oidc_config_incomplete", message: "m" },
      { kind: "anonymous" },
      { kind: "authenticated", session: SESSION },
      { kind: "unreachable", message: "e" },
    ];
    for (const probe of probes) {
      expect(authView(probe)).not.toBe("demo");
    }
  });

  it("仅 demo 探测 → demo；其余逐一对应 login/blocked/session/probing", () => {
    expect(authView({ kind: "demo" })).toBe("demo");
    expect(authView({ kind: "anonymous" })).toBe("login");
    expect(authView({ kind: "unreachable", message: "e" })).toBe("login");
    expect(authView({ kind: "blocked", code: null, message: "m" })).toBe("blocked");
    expect(authView({ kind: "authenticated", session: SESSION })).toBe("session");
    expect(authView(null)).toBe("probing");
  });

  it("blockedMessage 仅对 blocked 有值；sessionIdentity 仅对 authenticated 有值", () => {
    expect(blockedMessage({ kind: "blocked", code: "c", message: "m" })).toBe("c：m");
    expect(blockedMessage({ kind: "demo" })).toBeNull();
    expect(sessionIdentity({ kind: "authenticated", session: SESSION })).toEqual(SESSION);
    expect(sessionIdentity(null)).toBeNull();
  });
});
