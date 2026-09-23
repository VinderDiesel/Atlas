-- ADR-0031 T12：反馈审核、归因与版本化数据集。
-- 追加列：节点级归因（attribution_node）+ 审核证据（reviewed_by/at）。
-- 审核证据只追加不修改：revoke 动作将 status 回退 pending_review 并清空审核字段，
-- 历史审核行由 feedback_reviews 表保留（追加式审计轨迹）。

-- 节点级归因：反馈可关联到具体节点类型（可选，旧反馈为 NULL）。
ALTER TABLE feedback ADD COLUMN attribution_node TEXT;

-- 审核证据（审核动作时填充，提交时为 NULL）。
ALTER TABLE feedback ADD COLUMN reviewed_by_issuer TEXT;
ALTER TABLE feedback ADD COLUMN reviewed_by_subject TEXT;
ALTER TABLE feedback ADD COLUMN reviewed_at TEXT;

-- 审核历史表（追加式审计轨迹：每次审核/撤销动作都记录一行）。
CREATE TABLE feedback_reviews (
    review_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL REFERENCES feedback(feedback_id),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'revoked')),
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_feedback_reviews_feedback ON feedback_reviews(feedback_id, created_at);

-- 审核队列索引：快速查找 pending_review 反馈。
CREATE INDEX idx_feedback_pending ON feedback(status, created_at)
    WHERE status = 'pending_review';
