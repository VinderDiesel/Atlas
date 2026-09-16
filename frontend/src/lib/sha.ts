/**
 * sha 展示格式与快照徽标 4 态（纯函数；0018 判据 9 的 vitest 断言对象）。
 *
 * 语义依据 ADR-0019 决策 ①：`snapshot_source ∈ {env, head, latest}`，
 * `snapshot_bound_to_head` 按「解析出的 sha == 当前 HEAD」比较得出——
 * env 级也可以是不等于 HEAD 的 sha（枚举指定是该决策的合法用法），
 * 故 env 文案只披露来源、不声称绑定态（设计页 §3.4 徽标表）。
 */

/** 仓库短 sha 口径：git short sha 7 位（如 a11d779）。 */
export const SHORT_SHA_LEN = 7;

/** /api/v1/health 响应中与快照绑定相关的 4 键（其余键本函数不消费）。 */
export interface HealthSnapshotFacts {
  /** 后端 status：`"ok"` | `"degraded"` | 未知字符串（键集恒定的降级路径见 serving/api.py）。 */
  status: string;
  snapshot_sha: string | null;
  snapshot_source: string | null;
  snapshot_bound_to_head: boolean | null;
}

export type SnapshotBadgeKind = "env" | "head" | "latest" | "unavailable" | "unknown";

export interface SnapshotBadge {
  kind: SnapshotBadgeKind;
  severity: "info" | "warning" | "error";
  /** 徽标文案；改文案即改诚实性表述，先读设计页 §3.4 徽标表。 */
  text: string;
}

/** 短 sha：超 7 位则截断；已是短形态（或空串）原样返回，不造值。 */
export function shortSha(sha: string): string {
  return sha.length <= SHORT_SHA_LEN ? sha : sha.slice(0, SHORT_SHA_LEN);
}

/**
 * 徽标判定（§3.4 徽标 4 态表）。
 *
 * - 非 ok（degraded 等）：显示状态事实并走 error 色，**不得**显示「数据加载中」；
 * - ok 但组合不在契约取值内（sha/source 为空、source 与 bound 对不上）：
 *   报异常并禁用绑定声明——宁可说「异常」也不猜「与 HEAD 绑定」。
 */
export function snapshotBadge(facts: HealthSnapshotFacts): SnapshotBadge {
  if (facts.status !== "ok") {
    return {
      kind: "unavailable",
      severity: "error",
      text: `快照不可用（/api/v1/health 报 ${facts.status}）`,
    };
  }
  if (facts.snapshot_sha === null || facts.snapshot_source === null) {
    return {
      kind: "unknown",
      severity: "error",
      text: "快照状态异常：health 报 ok 但 snapshot_sha / snapshot_source 为空",
    };
  }
  const sha = shortSha(facts.snapshot_sha);
  const { snapshot_source: source, snapshot_bound_to_head: bound } = facts;
  if (source === "env") {
    return { kind: "env", severity: "info", text: `快照 ${sha}（环境变量指定）` };
  }
  if (source === "head" && bound === true) {
    return { kind: "head", severity: "info", text: `快照 ${sha}（与 HEAD 绑定）` };
  }
  if (source === "latest" && bound === false) {
    return {
      kind: "latest",
      severity: "warning",
      text: `快照 ${sha}（未绑定 HEAD，取自最新已锁）`,
    };
  }
  return {
    kind: "unknown",
    severity: "error",
    text: `快照状态异常：source=${source}, bound_to_head=${String(bound)}（不在契约组合内）`,
  };
}
