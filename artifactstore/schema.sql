-- ArtifactStore schema. Source of truth: ArtifactStore_PLAN.md §7.

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id        TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    parent_artifact_id TEXT,
    creator_agent_id   TEXT,
    tool_name          TEXT,
    artifact_type      TEXT NOT NULL,
    raw_uri            TEXT,                       -- nullable; reserved for file-backed escape hatch
    raw_blob           TEXT,                       -- canonical raw text storage (CLAUDE.md: BLOB)
    raw_hash           TEXT NOT NULL,              -- sha256 hex of raw_text
    token_count        INTEGER,
    preview            TEXT,
    sensitivity_label  TEXT DEFAULT 'internal',
    metadata_json      TEXT,                       -- per-artifact metadata (target, live, ...)
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type    ON artifacts(artifact_type);

CREATE TABLE IF NOT EXISTS artifact_spans (
    span_id      TEXT PRIMARY KEY,
    artifact_id  TEXT NOT NULL,
    span_type    TEXT NOT NULL,
    file_path    TEXT,
    line_start   INTEGER,
    line_end     INTEGER,
    text         TEXT NOT NULL,
    token_count  INTEGER,
    importance   REAL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_spans_artifact ON artifact_spans(artifact_id);
CREATE INDEX IF NOT EXISTS idx_spans_type     ON artifact_spans(span_type);

CREATE TABLE IF NOT EXISTS artifact_links (
    src_artifact_id TEXT NOT NULL,
    dst_artifact_id TEXT NOT NULL,
    relation        TEXT NOT NULL,
    confidence      REAL,
    PRIMARY KEY (src_artifact_id, dst_artifact_id, relation)
);

CREATE TABLE IF NOT EXISTS artifact_grants (
    grant_id           TEXT PRIMARY KEY,
    subject_agent_id   TEXT NOT NULL,
    issuer_agent_id    TEXT NOT NULL,
    artifact_predicate TEXT NOT NULL,  -- JSON
    allowed_ops        TEXT NOT NULL,  -- JSON array
    allowed_views      TEXT NOT NULL,  -- JSON array
    max_tokens         INTEGER,        -- NULL = unlimited; else cumulative cap
    consumed_tokens    INTEGER DEFAULT 0,  -- ticks up on every successful read
    expires_at         TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_grants_subject ON artifact_grants(subject_agent_id);

CREATE TABLE IF NOT EXISTS artifact_access_log (
    access_id          TEXT PRIMARY KEY,
    grant_id           TEXT,
    subject_agent_id   TEXT,
    artifact_id        TEXT,
    operation          TEXT,
    view               TEXT,
    timestamp          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    result_token_count INTEGER,
    allowed            BOOLEAN,
    denial_reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_grant   ON artifact_access_log(grant_id);
CREATE INDEX IF NOT EXISTS idx_access_subject ON artifact_access_log(subject_agent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts USING fts5(
    artifact_id UNINDEXED,
    artifact_type,
    preview,
    span_text,
    tool_name
);

-- Synthetic supervisor grant. The supervisor harness uses this for citation
-- verification (PLAN §20.2) so audit-log FKs always resolve and there's no
-- special case in app code. Idempotent.
INSERT OR IGNORE INTO artifact_grants (
    grant_id, subject_agent_id, issuer_agent_id,
    artifact_predicate, allowed_ops, allowed_views,
    max_tokens, expires_at
) VALUES (
    '__supervisor__', '__supervisor__', '__system__',
    '{}',
    '["search","get_spans","expand_view","find_related"]',
    '["preview","evidence","redacted","raw","provenance"]',
    NULL, NULL
);
