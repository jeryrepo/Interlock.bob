"""
orchestrator/ledger.py
======================
The single database-writing layer for Interlock.

Rules enforced here:
- Only this module may execute INSERT/UPDATE statements against SQLite.
- Agents must never import this module.
- All public functions accept explicit typed arguments and return plain dicts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "db" / "schema.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db(db_path: str = ":memory:") -> sqlite3.Connection:
    """
    Open (or create) a SQLite database, apply the schema, and return the
    connection.  Enables WAL journal mode and enforces foreign keys.

    Pass ``":memory:"`` for an in-memory database (tests / CI).
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    dependency_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(dependency_edge)")
    }
    if "documentation_status" not in dependency_columns:
        conn.execute(
            "ALTER TABLE dependency_edge ADD COLUMN documentation_status "
            "TEXT NOT NULL DEFAULT 'documented'"
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# change_request
# ---------------------------------------------------------------------------

def create_change(conn: sqlite3.Connection, change_id: str, description: str) -> dict:
    """Insert a new change request in INTAKE state and return it."""
    now = _now()
    conn.execute(
        """
        INSERT INTO change_request (id, description, status, entered_at,
                                    retry_count, created_at, updated_at)
        VALUES (?, ?, 'INTAKE', ?, 0, ?, ?)
        """,
        (change_id, description, now, now, now),
    )
    conn.commit()
    return get_change(conn, change_id)


def update_change_status(
    conn: sqlite3.Connection,
    change_id: str,
    status: str,
    increment_retry: bool = False,
) -> None:
    """Update the state-machine status (and optionally bump retry_count)."""
    now = _now()
    if increment_retry:
        conn.execute(
            """
            UPDATE change_request
               SET status = ?, entered_at = ?, retry_count = retry_count + 1,
                   updated_at = ?
             WHERE id = ?
            """,
            (status, now, now, change_id),
        )
    else:
        conn.execute(
            """
            UPDATE change_request
               SET status = ?, entered_at = ?, retry_count = 0, updated_at = ?
             WHERE id = ?
            """,
            (status, now, now, change_id),
        )
    conn.commit()


def get_change(conn: sqlite3.Connection, change_id: str) -> dict | None:
    """Return the change_request row as a dict, or None if not found."""
    row = conn.execute(
        "SELECT * FROM change_request WHERE id = ?", (change_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------

def add_evidence(
    conn: sqlite3.Connection,
    change_id: str,
    claim_type: str,
    subject: str,
    content: dict,
    source_ref: str,
    confidence: str,
    source_revision: str | None = None,
) -> dict:
    """Insert a validated evidence row and return it."""
    row_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO evidence
            (id, change_id, claim_type, subject, content, source_ref,
             confidence, source_revision, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            change_id,
            claim_type,
            subject,
            json.dumps(content),
            source_ref,
            confidence,
            source_revision,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM evidence WHERE id = ?", (row_id,)).fetchone()
    d = _row_to_dict(row)
    d["content"] = json.loads(d["content"])
    return d


def get_evidence(conn: sqlite3.Connection, change_id: str) -> list[dict]:
    """Return all evidence rows for a change, content parsed back to dict."""
    rows = conn.execute(
        "SELECT * FROM evidence WHERE change_id = ? ORDER BY created_at, rowid",
        (change_id,),
    ).fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        d["content"] = json.loads(d["content"])
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# dependency_edge
# ---------------------------------------------------------------------------

def add_dependency(
    conn: sqlite3.Connection,
    change_id: str,
    from_component: str,
    to_component: str,
    edge_type: str,
    reason: str | None = None,
    documentation_status: str = "documented",
) -> dict:
    """Insert a dependency edge and return it."""
    row_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO dependency_edge
            (id, change_id, from_component, to_component, edge_type,
             documentation_status, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row_id, change_id, from_component, to_component, edge_type,
         documentation_status, reason, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM dependency_edge WHERE id = ?", (row_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_dependencies(conn: sqlite3.Connection, change_id: str) -> list[dict]:
    """Return all dependency_edge rows for a change."""
    rows = conn.execute(
        "SELECT * FROM dependency_edge WHERE change_id = ? ORDER BY created_at",
        (change_id,),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# consumer_migration
# ---------------------------------------------------------------------------

def upsert_consumer_migration(
    conn: sqlite3.Connection,
    change_id: str,
    consumer: str,
    status: str,
) -> dict:
    """
    Insert or update a consumer migration status row.
    Uses INSERT OR REPLACE to honour the UNIQUE(change_id, consumer) constraint.
    """
    now = _now()
    # Preserve the existing id if this is an update
    existing = conn.execute(
        "SELECT id FROM consumer_migration WHERE change_id = ? AND consumer = ?",
        (change_id, consumer),
    ).fetchone()
    row_id = existing["id"] if existing else str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO consumer_migration (id, change_id, consumer, status, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(change_id, consumer) DO UPDATE
            SET status = excluded.status, updated_at = excluded.updated_at
        """,
        (row_id, change_id, consumer, status, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM consumer_migration WHERE change_id = ? AND consumer = ?",
        (change_id, consumer),
    ).fetchone()
    return _row_to_dict(row)


def get_consumer_migrations(conn: sqlite3.Connection, change_id: str) -> list[dict]:
    """Return all consumer_migration rows for a change."""
    rows = conn.execute(
        "SELECT * FROM consumer_migration WHERE change_id = ? ORDER BY consumer",
        (change_id,),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------

def record_approval(
    conn: sqlite3.Connection,
    change_id: str,
    gate: str,
    approved_by: str,
) -> dict:
    """Insert an approval record and return it."""
    row_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO approval (id, change_id, gate, approved_by, approved_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (row_id, change_id, gate, approved_by, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM approval WHERE id = ?", (row_id,)).fetchone()
    return _row_to_dict(row)


def get_approvals(conn: sqlite3.Connection, change_id: str) -> list[dict]:
    """Return all approval rows for a change."""
    rows = conn.execute(
        "SELECT * FROM approval WHERE change_id = ? ORDER BY approved_at",
        (change_id,),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# gate_decision
# ---------------------------------------------------------------------------

def record_gate_decision(
    conn: sqlite3.Connection,
    change_id: str,
    result: str,
    reason: str,
) -> dict:
    """Insert a gate_decision row and return it."""
    row_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO gate_decision (id, change_id, result, reason, decided_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (row_id, change_id, result, reason, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM gate_decision WHERE id = ?", (row_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_gate_decisions(conn: sqlite3.Connection, change_id: str) -> list[dict]:
    """Return all gate_decision rows for a change, most recent last."""
    rows = conn.execute(
        "SELECT * FROM gate_decision WHERE change_id = ? ORDER BY decided_at",
        (change_id,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_latest_gate_decision(conn: sqlite3.Connection, change_id: str) -> dict | None:
    """Return the most recent gate_decision row for a change, or None."""
    row = conn.execute(
        """
        SELECT * FROM gate_decision
         WHERE change_id = ?
         ORDER BY decided_at DESC, rowid DESC
         LIMIT 1
        """,
        (change_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None
