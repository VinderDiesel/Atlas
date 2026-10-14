/**
 * 星体徽章（Star Badge）——状态显示元素（治理面 + 数据接入港 + 优化引擎舱）。
 *
 * 星图隐喻：每个语义对象/流水线制品/任务是一颗星，状态决定星体的颜色与光晕。
 * 状态映射（契约值；未知值不猜分支，灰类原文展示）：
 *
 * - active / passed / ok / registered / bound          → 绿（--atlas-success）
 * - source_imported / release_ready / published        → 绿（制品已生效/已就绪；Phase 2c）
 * - up / approved / completed                          → 绿（反馈采纳 / 实验完成；Phase 3）
 * - draft / pending / dry / skipped                    → 琥珀（--atlas-accent，人工处理中）
 * - corrected / pending_review                         → 琥珀（待审反馈 / 纠正标注；Phase 3）
 * - validated / reviewed                               → 青（--atlas-primary，已验证；Phase 2c）
 * - running                                            → 青（进行中；Phase 3）
 * - retired / rejected                                 → 灰（--atlas-muted）
 * - failed / error / blocked / unregistered            → 红（--atlas-danger）
 * - down                                               → 红（反馈否决；Phase 3）
 * - superseded                                         → 紫（--atlas-handoff）
 * - 其余（契约外值）                                   → 灰 + 原文展示
 */
interface Props {
  /** 状态原文（服务端契约值；未知值如实展示）。 */
  status: string;
}

/** 已知状态 → 星体类名（测试直接断言类名；未命中走 --unknown）。 */
export const STAR_STATUS_CLASSES: Record<string, string> = {
  active: "active",
  passed: "active",
  ok: "active",
  registered: "active",
  bound: "active",
  source_imported: "active",
  release_ready: "active",
  published: "active",
  up: "active",
  approved: "active",
  completed: "active",
  draft: "draft",
  pending: "draft",
  dry: "draft",
  skipped: "draft",
  corrected: "draft",
  pending_review: "draft",
  validated: "verified",
  reviewed: "verified",
  running: "verified",
  retired: "retired",
  rejected: "retired",
  failed: "failed",
  error: "failed",
  blocked: "failed",
  unregistered: "failed",
  down: "failed",
  superseded: "superseded",
};

export default function StarBadge({ status }: Props) {
  const tone = STAR_STATUS_CLASSES[status] ?? "unknown";
  return (
    <span className={`star-badge star-badge--${tone}`} data-testid="star-badge">
      <span className="star-badge__dot" aria-hidden="true" />
      <span className="star-badge__label atlas-mono">{status}</span>
    </span>
  );
}
