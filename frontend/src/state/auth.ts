/**
 * 认证视图的纯逻辑（ADR-0031 D02 退出门；T03d 前端登录入口）。
 *
 * 本文件**不 import React**——与 `state/role.ts` 同纪律：可被 vitest 直接断言的
 * 纯函数与派生量。App/组件只消费这里的函数，不自行解释 `SessionProbe`。
 *
 * 退出门的机器断言载体：`authView` 是整个前端**唯一**把探测结果映射到
 * 「显示开发角色选择」的判定点——只有 `demo` 映射到 `"demo"`，私有模式的
 * 任何探测（probing/blocked/login/session）都不会渲染 RoleSwitcher。
 */
import type { AuthSessionPayload, SessionProbe } from "../api/auth";

/** 顶栏认证区形态（互斥穷尽；组件按此渲染，不再看 probe.kind）。 */
export type AuthView = "probing" | "demo" | "login" | "blocked" | "session";

/**
 * 探测 → 顶栏形态：
 * - null（探测中）→ `probing`（不渲染任何认证控件，避免私有模式闪现代理入口）；
 * - demo → `demo`（唯一显示 RoleSwitcher 的形态）；
 * - blocked → `blocked`（配置阻塞提示，不给登录入口——不伪装登录可用）；
 * - anonymous / unreachable → `login`（未认证或状态未知，给登录行动入口）；
 * - authenticated → `session`（身份 + 退出）。
 */
export function authView(probe: SessionProbe | null): AuthView {
  if (probe === null) {
    return "probing";
  }
  switch (probe.kind) {
    case "demo":
      return "demo";
    case "authenticated":
      return "session";
    case "blocked":
      return "blocked";
    case "anonymous":
    case "unreachable":
      return "login";
  }
}

/** 配置阻塞的说明文本（仅 blocked 有值；`code：message`，供 Tooltip 原文展示）。 */
export function blockedMessage(probe: SessionProbe | null): string | null {
  if (probe === null || probe.kind !== "blocked") {
    return null;
  }
  return probe.code === null ? probe.message : `${probe.code}：${probe.message}`;
}

/** 已登录会话（仅 authenticated 有值；私有模式顶栏身份展示的数据源）。 */
export function sessionIdentity(probe: SessionProbe | null): AuthSessionPayload | null {
  return probe !== null && probe.kind === "authenticated" ? probe.session : null;
}
