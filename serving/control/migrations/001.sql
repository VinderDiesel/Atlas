-- ADR-0031 T02：私有控制库首版草稿与修订证据，不含业务源 DDL。
CREATE TABLE schema_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version >= 0)
);
INSERT INTO schema_version VALUES (1, 0);

CREATE TABLE drafts (
    draft_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('semantic', 'flow', 'node_config')),
    owner_issuer TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    scope TEXT NOT NULL,
    base_git_sha TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'draft', 'validated', 'reviewed', 'source_imported',
        'release_ready', 'published', 'retired'
    )),
    content_json TEXT NOT NULL CHECK (json_valid(content_json)),
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE draft_actions (
    action_id INTEGER PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    revision INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    evidence_id TEXT NOT NULL CHECK (length(evidence_id) > 0),
    created_at TEXT NOT NULL,
    UNIQUE(draft_id, revision, status)
);

-- 内容与部署作用域绑定不可修改；仅 deployments 的活动指针可 CAS 切换。
CREATE TABLE releases (
    release_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    owner_issuer TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    scope TEXT NOT NULL,
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(release_id, scope, source_id)
);
CREATE TRIGGER immutable_release_update BEFORE UPDATE ON releases
BEGIN
    SELECT RAISE(ABORT, 'immutable release');
END;
CREATE TRIGGER immutable_release_delete BEFORE DELETE ON releases
BEGIN
    SELECT RAISE(ABORT, 'immutable release');
END;
CREATE TABLE deployments (
    deployment_id TEXT PRIMARY KEY,
    owner_issuer TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    scope TEXT NOT NULL,
    source_id TEXT NOT NULL,
    active_release_id TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(active_release_id, scope, source_id) REFERENCES releases(release_id, scope, source_id)
);
