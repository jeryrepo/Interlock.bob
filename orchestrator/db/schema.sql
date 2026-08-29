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
    documentation_status TEXT NOT NULL DEFAULT 'documented',
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
