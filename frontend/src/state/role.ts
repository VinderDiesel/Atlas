/**
 * 角色 + token 内存态的类型与纯逻辑（设计页 §4.1/§3.5；ADR-0021 决策 ⑤）。
 *
 * 本文件**不 import React**——React 状态（`useState`）由 App 持有，这里只放
 * 可被 vitest 直接断言的纯函数与文案常量（设计页 §4.1：「vitest 只测 lib/ 与
 * state/ 的纯函数」）。
 *
 * 三条硬约束（§3.5）在本文件的落点：
 * 1. 切角色即换会话——`identityKey` 与 `ROLE_SWITCH_NOTICE`（触发在 App）；
 * 2. 列表 claims 帮助文本——`LIST_CLAIM_HELP`（CLI 不解析列表值的如实说明）；
 * 3. token 只存内存——`ActiveIdentity` 无任何序列化/持久化函数；
 *    `__tests__/no-persist.test.ts` 扫描本目录断言无 localStorage/cookie 写入。
 *
 * 纪律：`decodeTokenPayload` **不验签**（前端无密钥、不验签的注释必须保留）——
 * 它只用于把 token 的真实身份如实渲染到界面（显示实际值而非声称值），
 * 信任判定始终在服务端 `serving.auth.verify_token`。
 */
import type { PolicyItem, RoleItem } from "../api/types";

// ---------------------------------------------------------------------------
// 身份（内存态）
// ---------------------------------------------------------------------------

/** 当前激活身份：token + 从 token 解码的真实身份（不验签，仅展示）。 */
export interface ActiveIdentity {
  token: string;
  /** 解码自 token payload 的 role（与服务端签发一致时才可能激活成功）。 */
  role: string;
  /** 解码自 token payload 的 user_context。 */
  claims: Record<string, unknown>;
  /** 获得通道：dev 签发（vite middleware）或手工粘贴。 */
  via: "sign" | "paste";
}

/** 身份稳定键（keys 排序后序列化）：用于「身份是否变化」的判定与断言。 */
export function identityKey(role: string, claims: Record<string, unknown>): string {
  const sorted = Object.keys(claims).sort();
  return JSON.stringify([role, sorted.map((key) => [key, claims[key]])]);
}

// ---------------------------------------------------------------------------
// token 解码（不验签）
// ---------------------------------------------------------------------------

export interface DecodedTokenPayload {
  role: string;
  user_context: Record<string, unknown>;
  exp: number | null;
}

function base64UrlToBytes(segment: string): Uint8Array {
  const b64 = segment.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const binary = atob(b64 + pad);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/**
 * 解码 JWT 的 payload 段（三段式 base64url；**不做签名校验**）。
 *
 * 非三段 / base64 非法 / JSON 非法 / 缺 role 键 → `null`（调用方按「无法解码」
 * 处理，不得猜测身份）。
 */
export function decodeTokenPayload(token: string): DecodedTokenPayload | null {
  const parts = token.trim().split(".");
  if (parts.length !== 3) {
    return null;
  }
  try {
    const payload: unknown = JSON.parse(new TextDecoder().decode(base64UrlToBytes(parts[1])));
    if (typeof payload !== "object" || payload === null) {
      return null;
    }
    const record = payload as Record<string, unknown>;
    if (typeof record.role !== "string") {
      return null;
    }
    const context = record.user_context;
    return {
      role: record.role,
      user_context:
        typeof context === "object" && context !== null
          ? (context as Record<string, unknown>)
          : {},
      exp: typeof record.exp === "number" ? record.exp : null,
    };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 角色矩阵（按域过滤；矩阵本身不得硬编码——§3.5）
// ---------------------------------------------------------------------------

/**
 * 当前域的角色清单：`declared_by_models` 含该域的策略 → 其 `roles[]`（去重，
 * 保持响应顺序）。域清单与角色矩阵的唯一事实源都是 `/governance/policies`
 * 响应（含 P-2sec 后的 `registered` 字段），前端不复制矩阵。
 */
export function rolesForDomain(policies: readonly PolicyItem[], domain: string): RoleItem[] {
  const seen = new Set<string>();
  const roles: RoleItem[] = [];
  for (const policy of policies) {
    if (!policy.declared_by_models.includes(domain)) {
      continue;
    }
    for (const role of policy.roles) {
      if (seen.has(role.name)) {
        continue;
      }
      seen.add(role.name);
      roles.push(role);
    }
  }
  return roles;
}

// ---------------------------------------------------------------------------
// claims 表单（标量 / 列表两类输入；§3.5 约束 2）
// ---------------------------------------------------------------------------

export interface ClaimFields {
  /** 单值输入框的键（required_claims 中不属于 list_claims 的）。 */
  scalars: string[];
  /** 列表输入的键（list_claims ∩ required_claims）。 */
  lists: string[];
}

export function claimFieldsOf(role: RoleItem): ClaimFields {
  return {
    scalars: role.required_claims.filter((key) => !role.list_claims.includes(key)),
    lists: role.list_claims.filter((key) => role.required_claims.includes(key)),
  };
}

/** 列表值解析：中英文逗号与空白均可作分隔（`"TN, KY"` → `["TN","KY"]`）。 */
export function parseListClaimValue(input: string): string[] {
  return input
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/**
 * claims 值的展示形态（如实：数组按序 join、对象 JSON 序列化；不摘要不改写）。
 *
 * 用途：把 token 里的 user_context「显示实际值而非声称值」（§3.5 约束 1 的
 * 显示面）——`null` 显示为字面 `null`（不是空串），未知类型回落 `String()`。
 */
export function formatClaimValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => formatClaimValue(item)).join(", ");
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

export type UserContextBuild =
  | { ok: true; context: Record<string, unknown> }
  | { ok: false; missing: string[] };

/**
 * 表单值 → user_context（签发前置校验在服务端 `sign_token`，这里只做「必填
 * 非空」的本地预检；缺键如实列出，不静默补默认值）。
 */
export function buildUserContext(
  role: RoleItem,
  values: Readonly<Record<string, string>>,
): UserContextBuild {
  const fields = claimFieldsOf(role);
  const missing: string[] = [];
  const context: Record<string, unknown> = {};
  for (const key of [...fields.scalars, ...fields.lists]) {
    const raw = (values[key] ?? "").trim();
    if (fields.lists.includes(key)) {
      const list = parseListClaimValue(raw);
      if (list.length === 0) {
        missing.push(key);
        continue;
      }
      context[key] = list;
    } else if (raw === "") {
      missing.push(key);
      continue;
    } else {
      context[key] = raw;
    }
  }
  return missing.length > 0 ? { ok: false, missing } : { ok: true, context };
}

// ---------------------------------------------------------------------------
// make token 命令生成（CLI 签发路径；§3.5「dev 签发路径」的精确格式）
// ---------------------------------------------------------------------------

/**
 * shell 单引号串的转义（两件事都要做）：
 * - `'` → `'\''`（跳出-转义-重入单引号，POSIX shell 惯用法）；
 * - `$` → `$$`——**这是 Make 的转义**：命令最终经 make 变量展开，单个 `$`
 *   会被 make 当作变量引用吞掉，必须写成 `$$` 才能逐字到达 shell（本函数
 *   生成的是「在终端执行的 make 命令」，不是直接执行的 shell 命令）。
 */
function escapeForMakeSingleQuoted(value: string): string {
  return value.replace(/\$/g, "$$$$").replace(/'/g, "'\\''");
}

/** `make token ROLE=… CONTEXT='<json>'`（Makefile token 目标同构；JSON 紧凑）。 */
export function makeTokenCommand(role: string, context: Record<string, unknown>): string {
  const json = JSON.stringify(context);
  return `make token ROLE=${role} CONTEXT='${escapeForMakeSingleQuoted(json)}'`;
}

// ---------------------------------------------------------------------------
// 文案常量（渲染义务——改文案先读设计页 §3.5 与 §3.3，并同步 role.test.ts）
// ---------------------------------------------------------------------------

/** §3.5 约束 1 的提示原文：切换角色时必须显式告知，不得静默新建。 */
export const ROLE_SWITCH_NOTICE = "切换角色将开启新会话";

/** §3.3 第 5 行：未注册角色的禁用说明（由 `registered` 字段驱动，非硬编码角色名）。 */
export const ROLE_UNREGISTERED_NOTICE = "角色未注册，无法签发 token";

/** §3.5 约束 2 的面板帮助文本（CLI 不解析列表值的如实说明）。 */
export const LIST_CLAIM_HELP =
  "列表 claims（categories）须以 JSON 数组注入：make token 的 CONTEXT 支持数组，" +
  "而 agent/cli.py --role-ctx 不解析列表值——CLI 交互中该角色不可用，本面板表单是便捷入口。";
