-- ADR-0031 T07：只读数据源修订（D03 源合同，追加式版本）与受限探测证据。
-- 秘密只存 env:<NAME> 引用（N9，明文凭据/DSN 永不入库）；探测结果是带时间戳的
-- 证据，不是永久保证（D03）——每次探测一行，最新证据在列表视图聚合展示。
-- 旧版本不可变：同 source 新修订只追加新行，不原地改变既有绑定引用的修订。
CREATE TABLE source_revisions (
    source_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    revision TEXT NOT NULL CHECK (length(revision) BETWEEN 1 AND 128),
    connector_kind TEXT NOT NULL CHECK (connector_kind IN ('doris')),
    secret_ref TEXT NOT NULL CHECK (secret_ref LIKE 'env:%'),
    allowed_catalogs_json TEXT NOT NULL CHECK (json_valid(allowed_catalogs_json)),
    allowed_tables_json TEXT NOT NULL CHECK (json_valid(allowed_tables_json)),
    timezone TEXT NOT NULL,
    tls_policy TEXT NOT NULL CHECK (tls_policy IN ('required', 'disabled')),
    query_budget INTEGER NOT NULL CHECK (query_budget BETWEEN 1 AND 10000000),
    created_by_issuer TEXT NOT NULL,
    created_by_subject TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, version)
);

-- 探测证据：status=ok 时 schema_digest/capabilities 必有；blocked 时必带理由。
-- 正文（凭据、连接串）永不入库——findings 只允许脱敏事实（版本、白名单表清单）。
CREATE TABLE source_probes (
    probe_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'blocked')),
    blocked_reason TEXT,
    observed_at TEXT NOT NULL,
    engine_version TEXT,
    schema_digest TEXT,
    capabilities_json TEXT NOT NULL CHECK (json_valid(capabilities_json)),
    findings_json TEXT NOT NULL CHECK (json_valid(findings_json)),
    created_by_issuer TEXT NOT NULL,
    created_by_subject TEXT NOT NULL,
    CHECK (
        (status = 'blocked' AND blocked_reason IS NOT NULL)
        OR (status = 'ok' AND blocked_reason IS NULL)
    ),
    FOREIGN KEY (source_id, version) REFERENCES source_revisions(source_id, version)
);
CREATE INDEX idx_source_probes_source ON source_probes(source_id, observed_at);
