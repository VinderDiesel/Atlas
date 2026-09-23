-- ADR-0031 T08a-s2：确定性校验与人工审核证据（追加式，绑定 revision + 摘要）。
-- 证据只追加不修改：编辑（revision+1、status=draft）使旧校验/审核自然失效，
-- 历史行保留供审计（不删除、不覆写）；状态推进另由 draft_actions 记录。
CREATE TABLE draft_validations (
    validation_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    findings_json TEXT NOT NULL CHECK (json_valid(findings_json)),
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_draft_validations_draft ON draft_validations(draft_id, created_at);

-- comment 可为 NULL（少说不是错）；approved 推进 reviewed、rejected 只落证据。
CREATE TABLE draft_reviews (
    review_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content_digest TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    comment TEXT,
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_draft_reviews_draft ON draft_reviews(draft_id, created_at);
