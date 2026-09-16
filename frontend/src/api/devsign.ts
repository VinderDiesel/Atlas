/**
 * dev 签发客户端（P2；设计页 §3.5「dev 便利由 vite middleware 承担」）。
 *
 * 中间件本体在 `vite.config.ts`（dev-only、`apply:"serve"`），spawn 仓库 `.venv`
 * 的 python 调 `serving.auth.sign_token`——**唯一签发实现与唯一密钥来源**；
 * 本模块只做 HTTP 调用与三态归一，不得出现任何密钥或签发逻辑（N9）。
 *
 * 三态归一（调用侧 RoleSwitcher 按此降级）：
 * - `ok`：200 + `{token}`（仅 dev 中间件在运行时可达）；
 * - `unavailable`：端点不可达 / 404 / 405 / 501——`make serve-dev` 或容器下
 *   的预期形态 → 降级为「复制 make token 命令 + 粘贴」；
 * - `error`：非 2xx 且非「不可用」类——detail 原文透传（零遥测纪律，
 *   不美化不摘要）。
 */
import { ApiError, postJson } from "./client";
import { DEV_SIGN } from "./endpoints";

export type DevSignOutcome =
  | { kind: "ok"; token: string }
  | { kind: "unavailable"; detail: string }
  | { kind: "error"; status: number; detail: string };

export async function devSign(
  role: string,
  context: Record<string, unknown>,
): Promise<DevSignOutcome> {
  try {
    const body = await postJson<{ token?: unknown }>(DEV_SIGN, { role, context }, "");
    if (typeof body.token !== "string" || body.token === "") {
      return { kind: "error", status: 200, detail: "响应缺少 token 字段（契约外形态）" };
    }
    return { kind: "ok", token: body.token };
  } catch (e: unknown) {
    if (e instanceof ApiError) {
      if (e.status === 404 || e.status === 405) {
        return {
          kind: "unavailable",
          detail: `${e.detail}（端点不存在：仅 make ui-dev 的 vite dev 提供 ${DEV_SIGN}）`,
        };
      }
      if (e.status === 501) {
        return { kind: "unavailable", detail: `dev 签发未配置：${e.detail}` };
      }
      return { kind: "error", status: e.status, detail: e.detail };
    }
    return {
      kind: "unavailable",
      detail: `dev 签发不可达：${e instanceof Error ? e.message : String(e)}`,
    };
  }
}
