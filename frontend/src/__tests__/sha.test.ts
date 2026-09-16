/**
 * lib/sha.ts 纯函数断言（0018 判据 9 的「sha 展示格式」；设计页 §3.4 徽标 4 态表）。
 *
 * 语义依据 ADR-0019 决策 ①：`source ∈ {env, head, latest}`，且 env 也参与
 * `sha == HEAD` 比较（枚举指定不等于 HEAD 的 sha 是该决策的合法用法）——
 * 故 env 文案只披露来源、不声称绑定态。只测纯函数，不做渲染断言。
 */
import { describe, expect, it } from "vitest";

import { SHORT_SHA_LEN, shortSha, snapshotBadge, type HealthSnapshotFacts } from "../lib/sha";

/** 默认构造一条 ok 事实，按需覆盖单个字段。 */
const ok = (over: Partial<HealthSnapshotFacts>): HealthSnapshotFacts => ({
  status: "ok",
  snapshot_sha: "a11d779",
  snapshot_source: "head",
  snapshot_bound_to_head: true,
  ...over,
});

describe("sha.ts（0018 判据 9）", () => {
  it("shortSha 截断到 7 位；不长于 7 位时原样返回", () => {
    expect(SHORT_SHA_LEN).toBe(7);
    expect(shortSha("a11d779ffff")).toBe("a11d779");
    expect(shortSha("a11d779")).toBe("a11d779");
    expect(shortSha("abc")).toBe("abc");
  });

  it("env 来源：文案为「环境变量指定」，且不因 bound=false 改口径（§3.4 表第 1 行）", () => {
    const badge = snapshotBadge(ok({ snapshot_source: "env", snapshot_bound_to_head: false }));
    expect(badge.kind).toBe("env");
    expect(badge.severity).toBe("info");
    expect(badge.text).toBe("快照 a11d779（环境变量指定）");
  });

  it("head 且 bound=true：「与 HEAD 绑定」（§3.4 表第 2 行）", () => {
    const badge = snapshotBadge(ok({}));
    expect(badge.kind).toBe("head");
    expect(badge.severity).toBe("info");
    expect(badge.text).toBe("快照 a11d779（与 HEAD 绑定）");
  });

  it("latest 且 bound=false：警示色，文案写明未绑定（§3.4 表第 3 行）", () => {
    const badge = snapshotBadge(ok({ snapshot_source: "latest", snapshot_bound_to_head: false }));
    expect(badge.kind).toBe("latest");
    expect(badge.severity).toBe("warning");
    expect(badge.text).toBe("快照 a11d779（未绑定 HEAD，取自最新已锁）");
  });

  it("degraded：显示 degraded 事实，不得显示「数据加载中」（§3.4 表第 4 行）", () => {
    const badge = snapshotBadge({
      status: "degraded",
      snapshot_sha: null,
      snapshot_source: null,
      snapshot_bound_to_head: null,
    });
    expect(badge.kind).toBe("unavailable");
    expect(badge.severity).toBe("error");
    expect(badge.text).toBe("快照不可用（/api/v1/health 报 degraded）");
    expect(badge.text).not.toContain("加载中");
  });

  it("未知 status（非 ok）：同样走不可用分支并透传状态原文", () => {
    const badge = snapshotBadge(ok({ status: "maintenance" }));
    expect(badge.kind).toBe("unavailable");
    expect(badge.text).toContain("maintenance");
  });

  it("ok 但 sha/source 为空：报异常，不猜绑定态", () => {
    const badge = snapshotBadge(ok({ snapshot_sha: null }));
    expect(badge.kind).toBe("unknown");
    expect(badge.severity).toBe("error");
    expect(badge.text).toContain("异常");
    expect(badge.text).not.toContain("与 HEAD 绑定");
  });

  it("ok 但 source/bound 组合不在契约内（如 latest+true）：报异常，不声称绑定", () => {
    const badge = snapshotBadge(ok({ snapshot_source: "latest", snapshot_bound_to_head: true }));
    expect(badge.kind).toBe("unknown");
    expect(badge.severity).toBe("error");
    expect(badge.text).toContain("不在契约组合内");
  });
});
