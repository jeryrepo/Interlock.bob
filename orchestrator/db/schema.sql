-- Interlock orchestrator schema
-- SQLite; apply once via ledger.init_db()

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- change_request
-- Holds one row per proposed migration.
-- `status` is the current state-machine state.
-- `entered_at` and `retry_count` support resumable execution.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_request (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'INTAKE',
    entered_at  TEXT NOT NULL,          -- ISO-8601 UTC of last state entry
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ------------------------------------------------------------
-- evidence
-- Structured evidence rows written by the orchestrator after
-- validating agent results.  source_revision carries the Git
-- commit SHA or equivalent when available.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
    id              TEXT PRIMARY KEY,
    change_id       TEXT NOT NULL REFERENCES change_request(id),
    claim_type      TEXT NOT NULL,   -- dependency | migration_status | test_result | risk
    subject         TEXT NOT NULL,
    content         TEXT NOT NULL,   -- JSON blob
    source_ref      TEXT NOT NULL,
    confidence      TEXT NOT NULL,   -- hypothesis | confirmed | refuted
    source_revision TEXT,            -- Git SHA or None
    created_at      TEXT NOT NULL
);

-- ------------------------------------------------------------
-- dependency_edge
-- Graph edges.  The NetworkX graph is derived from these rows
-- on every read; no second graph state is maintained.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dependency_edge (
    id             TEXT PRIMARY KEY,
    change_id      TEXT NOT NULL REFERENCES change_request(id),
    from_component TEXT NOT NULL,
    to_component   TEXT NOT NULL,
    edge_type      TEXT NOT NULL,    -- api | event | db | undocumented
    reason         TEXT,
    created_at     TEXT NOT NULL
);

-- ------------------------------------------------------------
-- consumer_migration
-- One row per (change, consumer).  UPSERT keeps it idempotent.
-- status: pending | in_progress | verified | failed
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consumer_migration (
    id         TEXT PRIMARY KEY,
    change_id  TEXT NOT NULL REFERENCES change_request(id),
    consumer   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    UNIQUE (change_id, consumer)
);

-- ------------------------------------------------------------
-- approval
-- Human approvals at named gates.
-- gate: coordinate | legacy_removal
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval (
    id          TEXT PRIMARY KEY,
    change_id   TEXT NOT NULL REFERENCES change_request(id),
    gate        TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL
);

-- ------------------------------------------------------------
-- gate_decision
-- Written once per gate evaluation by the orchestrator.
-- result: VERIFIED | NOT_PROVEN_SAFE
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_decision (
    id         TEXT PRIMARY KEY,
    change_id  TEXT NOT NULL REFERENCES change_request(id),
    result     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

-- ------------------------------------------------------------
-- Indexes for fast per-change lookups
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_evidence_change          ON evidence(change_id);
CREATE INDEX IF NOT EXISTS idx_dependency_edge_change   ON dependency_edge(change_id);
CREATE INDEX IF NOT EXISTS idx_consumer_migration_change ON consumer_migration(change_id);
CREATE INDEX IF NOT EXISTS idx_approval_change          ON approval(change_id);
CREATE INDEX IF NOT EXISTS idx_gate_decision_change     ON gate_decision(change_id);

-- ------------------------------------------------------------
-- change_spec
-- 1:1 with change_request.  Structured intent for a change.
--
-- A side table rather than columns on change_request, for two reasons.
-- init_db() runs executescript(schema.sql) on EVERY startup, so a new
-- CREATE TABLE IF NOT EXISTS applies itself to existing databases, while a
-- new column would silently not.  And main.py builds ChangeResponse(**row)
-- from change_request, so widening that table would change the response
-- shape for every existing client.
--
-- Absent row => a legacy description-only change, which runs the stub
-- workflow.  kind: field_rename | api_contract_change | transport_migration
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_spec (
    change_id  TEXT PRIMARY KEY REFERENCES change_request(id),
    kind       TEXT NOT NULL,
    spec       TEXT NOT NULL,      -- JSON, validated before insert
    created_at TEXT NOT NULL
);

-- ------------------------------------------------------------
-- work_item
-- One row per (change, component, step_kind).  Supersedes
-- consumer_migration, whose ledger functions are now a projection
-- over step_kind = 'migrate'.
--
-- step_kind: provider_patch | migrate | subscribe | webhook_quiet
-- status:    pending | in_progress | verified | failed | blocked
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_item (
    id         TEXT PRIMARY KEY,
    change_id  TEXT NOT NULL REFERENCES change_request(id),
    component  TEXT NOT NULL,
    step_kind  TEXT NOT NULL DEFAULT 'migrate',
    status     TEXT NOT NULL DEFAULT 'pending',
    detail     TEXT,               -- JSON: commit_sha, error, blocked_reason
    updated_at TEXT NOT NULL,
    UNIQUE (change_id, component, step_kind)
);

CREATE INDEX IF NOT EXISTS idx_work_item_change ON work_item(change_id);
