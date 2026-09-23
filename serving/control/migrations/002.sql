-- ADR-0031 T05：运行事实、事件、显式捕获与最小反馈；只存控制状态，不含业务源 DDL。
-- 事件表 append-only（触发器拒绝 UPDATE/DELETE）；正文（问句/SQL/结果）默认不落库，
-- 只有显式授权捕获才进入 artifacts，且超过 retain_until 只删正文、保留删除标记。

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    owner_issuer TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(deployment_id),
    scope TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('ask', 'analyze', 'execute_plan')),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
    client_request_id TEXT NOT NULL CHECK (length(client_request_id) BETWEEN 1 AND 128),
    request_digest TEXT NOT NULL,
    release_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'blocked', 'failed', 'interrupted'
    )),
    result_kind TEXT CHECK (result_kind IS NULL OR result_kind IN (
        'answer', 'clarify', 'handoff', 'blocked', 'error'
    )),
    result_availability TEXT NOT NULL CHECK (result_availability IN (
        'pending', 'available', 'not_retained', 'expired', 'restricted'
    )),
    replay_of TEXT REFERENCES runs(run_id),
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_issuer, owner_subject, deployment_id, client_request_id)
);
CREATE INDEX idx_runs_owner_created ON runs(owner_issuer, owner_subject, created_at);

CREATE TABLE run_events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    node_id TEXT,
    node_run_id TEXT,
    parent_node_run_id TEXT,
    attempt INTEGER,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'RUN_ACCEPTED', 'RUN_STARTED', 'NODE_STARTED', 'NODE_FINISHED', 'NODE_FAILED',
        'NODE_SKIPPED', 'EDGE_TAKEN', 'TOOL_STARTED', 'TOOL_FINISHED', 'FALLBACK',
        'STATE_SNAPSHOT', 'RUN_FINISHED', 'RUN_INTERRUPTED'
    )),
    release_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY (run_id, seq)
);
CREATE TRIGGER immutable_run_event_update BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'immutable run event');
END;
CREATE TRIGGER immutable_run_event_delete BEFORE DELETE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'immutable run event');
END;

-- 显式授权捕获（D13）：fields 为 question/node_io/result 子集；content 只允许被授权字段。
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    kind TEXT NOT NULL CHECK (kind IN ('capture')),
    purpose TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 256),
    fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),
    content_json TEXT CHECK (content_json IS NULL OR json_valid(content_json)),
    content_digest TEXT,
    retain_until TEXT NOT NULL,
    cleaned_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_artifacts_run ON artifacts(run_id);

-- 最小反馈采集（T12 前固定 pending_review / training_eligible=0，不提前开放训练）。
CREATE TABLE feedback (
    feedback_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    owner_issuer TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('up', 'down', 'corrected')),
    comment TEXT CHECK (comment IS NULL OR length(comment) <= 2000),
    correction_json TEXT CHECK (correction_json IS NULL OR json_valid(correction_json)),
    status TEXT NOT NULL CHECK (status IN ('pending_review', 'approved', 'rejected')),
    training_eligible INTEGER NOT NULL CHECK (training_eligible IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_feedback_owner_created ON feedback(owner_issuer, owner_subject, created_at);
