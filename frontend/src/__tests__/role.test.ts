/**
 * state/role.ts 纯函数断言（P2；设计页 §3.5 三条硬约束 + §3.3 第 5 行）。
 *
 * fixture 取自 2026-09-16 对本机服务的实测响应（`/governance/policies`）：
 * 两策略 7 行（retail 3 / finance 4，hq_admin 两域共用），实测全部
 * `registered: true`（P-2sec 已落地）。
 *
 * 文案断言为逐字（改文案即改诚实性表述——先改设计页再改这里）。
 * token fixture 为**手造**（本文件不保存任何真实签发 token）；decode 不验签，
 * 手造 payload 足够覆盖解码路径。编码用 btoa/TextEncoder（DOM lib 类型，
 * 测试运行于 vitest node 环境，两者均为全局可用）。
 */
import { describe, expect, it } from "vitest";

import type { PolicyItem, RoleItem } from "../api/types";
import {
  LIST_CLAIM_HELP,
  ROLE_SWITCH_NOTICE,
  ROLE_UNREGISTERED_NOTICE,
  buildUserContext,
  claimFieldsOf,
  decodeTokenPayload,
  formatClaimValue,
  identityKey,
  makeTokenCommand,
  parseListClaimValue,
  rolesForDomain,
} from "../state/role";

function role(
  name: string,
  required: string[] = [],
  lists: string[] = [],
  registered = true,
): RoleItem {
  return {
    name,
    condition: null,
    description: null,
    required_claims: required,
    list_claims: lists,
    registered,
  };
}

const POLICIES: PolicyItem[] = [
  {
    name: "rp_dept_visible",
    description: "零售",
    default_deny: true,
    declared_by_models: ["retail"],
    roles: [
      role("hq_admin"),
      role("region_manager", ["region"]),
      role("category_analyst", ["region", "categories"], ["categories"]),
    ],
  },
  {
    name: "rp_branch_visible",
    description: "金融",
    default_deny: true,
    declared_by_models: ["finance"],
    roles: [
      role("hq_admin"),
      role("branch_manager", ["branch"]),
      role("broker", ["brokerid"]),
      role("compliance_auditor", ["max_tier"]),
    ],
  },
];

function b64urlBytes(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 手造三段 token（payload 以 UTF-8 编码，模拟真实 JWT 段形态）。 */
function fakeToken(payload: unknown): string {
  return `x.${b64urlBytes(new TextEncoder().encode(JSON.stringify(payload)))}.y`;
}

describe("role.ts 角色矩阵（§3.5：矩阵不得硬编码，按域过滤）", () => {
  it("finance 域：4 行（hq_admin 两域共用，出现在各自域清单里）", () => {
    expect(rolesForDomain(POLICIES, "finance").map((r) => r.name)).toEqual([
      "hq_admin",
      "branch_manager",
      "broker",
      "compliance_auditor",
    ]);
  });

  it("retail 域：3 行", () => {
    expect(rolesForDomain(POLICIES, "retail").map((r) => r.name)).toEqual([
      "hq_admin",
      "region_manager",
      "category_analyst",
    ]);
  });

  it("未知域返回空清单（不猜测、不落回默认域）", () => {
    expect(rolesForDomain(POLICIES, "unknown")).toEqual([]);
  });

  it("多策略同域时按 name 去重（保持首次出现顺序）", () => {
    const dup: PolicyItem[] = [
      { name: "p1", description: null, default_deny: true, declared_by_models: ["x"], roles: [role("hq_admin")] },
      {
        name: "p2",
        description: null,
        default_deny: true,
        declared_by_models: ["x"],
        roles: [role("hq_admin"), role("broker", ["brokerid"])],
      },
    ];
    expect(rolesForDomain(dup, "x").map((r) => r.name)).toEqual(["hq_admin", "broker"]);
  });

  it("未注册角色透传不剔除（禁用态由组件按 registered 字段渲染，此处不得静默过滤）", () => {
    const withUnregistered: PolicyItem[] = [
      { name: "p", description: null, default_deny: true, declared_by_models: ["y"], roles: [role("future_role", [], [], false)] },
    ];
    const roles = rolesForDomain(withUnregistered, "y");
    expect(roles).toHaveLength(1);
    expect(roles[0].registered).toBe(false);
  });
});

describe("role.ts token 解码（不验签——信任判定在服务端）", () => {
  it("正常三段 token：解码 role / user_context / exp", () => {
    const token = fakeToken({ role: "branch_manager", user_context: { branch: "east" }, exp: 1735689600 });
    expect(decodeTokenPayload(token)).toEqual({
      role: "branch_manager",
      user_context: { branch: "east" },
      exp: 1735689600,
    });
  });

  it("UTF-8 中文 claims 值正确解码（TextDecoder 路径）", () => {
    const token = fakeToken({ role: "hq_admin", user_context: { 简称: "总部" } });
    expect(decodeTokenPayload(token)?.user_context).toEqual({ 简称: "总部" });
  });

  it("非三段 / 段非法 base64 / payload 非 JSON / 缺 role：一律 null（不猜测身份）", () => {
    expect(decodeTokenPayload("opaque")).toBeNull();
    expect(decodeTokenPayload("a.b")).toBeNull();
    expect(decodeTokenPayload("x.%%%.y")).toBeNull();
    expect(decodeTokenPayload(fakeToken("not-an-object"))).toBeNull();
    expect(decodeTokenPayload(fakeToken({ sub: "atlas-user" }))).toBeNull();
    expect(decodeTokenPayload("")).toBeNull();
  });

  it("缺 user_context 与 exp：如实落空对象 / null，不补造", () => {
    const decoded = decodeTokenPayload(fakeToken({ role: "hq_admin" }));
    expect(decoded).toEqual({ role: "hq_admin", user_context: {}, exp: null });
  });
});

describe("role.ts 身份键（切角色判定与断言用）", () => {
  it("claims 键顺序无关：同一身份产生同一键", () => {
    expect(identityKey("r", { a: 1, b: 2 })).toBe(identityKey("r", { b: 2, a: 1 }));
  });

  it("role 或 claims 值变化 → 键不同", () => {
    expect(identityKey("r", { a: 1 })).not.toBe(identityKey("s", { a: 1 }));
    expect(identityKey("r", { a: 1 })).not.toBe(identityKey("r", { a: 2 }));
  });
});

describe("role.ts claims 表单与命令生成（§3.5 约束 2）", () => {
  it("claimFieldsOf：标量与列表分流（category_analyst 实测 required=[region,categories]、list=[categories]）", () => {
    const analyst = role("category_analyst", ["region", "categories"], ["categories"]);
    expect(claimFieldsOf(analyst)).toEqual({ scalars: ["region"], lists: ["categories"] });
    expect(claimFieldsOf(role("hq_admin"))).toEqual({ scalars: [], lists: [] });
  });

  it("parseListClaimValue：中英文逗号与空白均可分隔；空串为空列表", () => {
    expect(parseListClaimValue("TN, KY")).toEqual(["TN", "KY"]);
    expect(parseListClaimValue("鞋类，饮料")).toEqual(["鞋类", "饮料"]);
    expect(parseListClaimValue("  Shoes  Bikes  ")).toEqual(["Shoes", "Bikes"]);
    expect(parseListClaimValue("")).toEqual([]);
    expect(parseListClaimValue(" , ， ")).toEqual([]);
  });

  it("buildUserContext：标量为字符串、列表为 JSON 数组；缺键如实列出", () => {
    const analyst = role("category_analyst", ["region", "categories"], ["categories"]);
    expect(buildUserContext(analyst, { region: "TN", categories: "Shoes, Bikes" })).toEqual({
      ok: true,
      context: { region: "TN", categories: ["Shoes", "Bikes"] },
    });
    expect(buildUserContext(analyst, { region: "TN", categories: "" })).toEqual({
      ok: false,
      missing: ["categories"],
    });
    expect(buildUserContext(analyst, {})).toEqual({ ok: false, missing: ["region", "categories"] });
  });

  it("makeTokenCommand：与 Makefile token 目标同构的精确命令（JSON 紧凑单引号包裹）", () => {
    expect(makeTokenCommand("category_analyst", { region: "TN", categories: ["Shoes"] })).toBe(
      `make token ROLE=category_analyst CONTEXT='{"region":"TN","categories":["Shoes"]}'`,
    );
  });

  it("makeTokenCommand：$ 转义为 $$（make 变量展开语义）；单引号走 '\"'\"' 跳出转义", () => {
    expect(makeTokenCommand("r", { branch: "A$B" })).toContain(`"A$$B"`);
    const quoted = makeTokenCommand("r", { branch: "O'Brien" });
    expect(quoted).toContain(`O'\\''Brien`);
    expect(quoted.startsWith("make token ROLE=r CONTEXT='")).toBe(true);
    expect(quoted.endsWith("'")).toBe(true);
  });

  it("文案逐字（渲染义务）：切换提示与未注册说明与设计页 §3.5/§3.3 一致", () => {
    expect(ROLE_SWITCH_NOTICE).toBe("切换角色将开启新会话");
    expect(ROLE_UNREGISTERED_NOTICE).toBe("角色未注册，无法签发 token");
  });

  it("列表 claims 帮助文本包含两条关键事实（CONTEXT 支持数组 / CLI 不解析列表值）", () => {
    expect(LIST_CLAIM_HELP).toContain("make token 的 CONTEXT 支持数组");
    expect(LIST_CLAIM_HELP).toContain("--role-ctx 不解析列表值");
  });
});

describe("role.ts claims 展示（显示实际值而非声称值）", () => {
  it("formatClaimValue：数组按序 join、null 如实为字面 null、对象 JSON、标量 String", () => {
    expect(formatClaimValue(["Shoes", "Bikes"])).toBe("Shoes, Bikes");
    expect(formatClaimValue("TN")).toBe("TN");
    expect(formatClaimValue(7)).toBe("7");
    expect(formatClaimValue(null)).toBe("null");
    expect(formatClaimValue({ a: 1 })).toBe('{"a":1}');
    expect(formatClaimValue([["x"], null])).toBe("x, null");
  });
});
