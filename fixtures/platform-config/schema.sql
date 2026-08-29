-- platform-config schema
-- PRE-MIGRATION state: customer_id is the canonical customer identifier.
-- This schema will be updated as part of the customer_id -> account_id migration.

-- Core accounts table: customer_id is the primary key field.
CREATE TABLE IF NOT EXISTS accounts (
    customer_id  TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Orders table: references customer_id as the foreign key.
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES accounts(customer_id),
    item         TEXT NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Analytics events table: events carry customer_id as the identifier.
CREATE TABLE IF NOT EXISTS analytics_events (
    event_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    payload      TEXT,
    recorded_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fraud checks table: fraud assessments keyed by customer_id.
CREATE TABLE IF NOT EXISTS fraud_checks (
    check_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES accounts(customer_id),
    risk_score   REAL NOT NULL DEFAULT 0.0,
    checked_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Migration note: when customer_id -> account_id migration is applied,
-- the following changes will be needed across this schema:
--   1. ALTER TABLE accounts ADD COLUMN account_id TEXT;
--   2. UPDATE accounts SET account_id = customer_id;
--   3. Add new FK references using account_id.
--   4. Keep customer_id during compatibility window.
