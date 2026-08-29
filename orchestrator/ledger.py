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
        "SELECT * FROM evidence WHERE change_id = ? ORDER BY created_at",
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
) -> dict:
    """Insert a dependency edge and return it."""
    row_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO dependency_edge
            (id, change_id, from_component, to_component, edge_type, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row_id, change_id, from_component, to_component, edge_type, reason, now),
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
# change_spec
# ---------------------------------------------------------------------------

def set_change_spec(
    conn: sqlite3.Connection,
    change_id: str,
    kind: str,
    spec: dict,
) -> dict:
    """
    Persist the structured spec for a change.  Idempotent per change_id.

    The caller validates against orchestrator.schemas.ChangeSpec before calling;
    the ledger persists, it does not validate.
    """
    now = _now()
    conn.execute(
        """
        INSERT INTO change_spec (change_id, kind, spec, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(change_id) DO UPDATE
            SET kind = excluded.kind, spec = excluded.spec
        """,
        (change_id, kind, json.dumps(spec), now),
    )
    conn.commit()
    return get_change_spec(conn, change_id)


def get_change_spec(conn: sqlite3.Connection, change_id: str) -> dict | None:
    """
    Return ``{"change_id", "kind", "spec": {...}, "created_at"}`` or None.

    None means a legacy description-only change: the workflow falls back to
    stubs rather than attempting to drive real agents with no inputs.
    """
    row = conn.execute(
        "SELECT * FROM change_spec WHERE change_id = ?", (change_id,)
    ).fetchone()
    if row is None:
        return None
    out = _row_to_dict(row)
    out["spec"] = json.loads(out["spec"])
    return out


# ---------------------------------------------------------------------------
# work_item
# ---------------------------------------------------------------------------

def upsert_work_item(
    conn: sqlite3.Connection,
    change_id: str,
    component: str,
    status: str,
    step_kind: str = "migrate",
    detail: dict | None = None,
) -> dict:
    """
    Insert or update one (change, component, step_kind) work item.

    UPSERT keeps it idempotent, which matters because run_workflow() resumes
    from persisted state and may re-run a phase.
    """
    now = _now()
    existing = conn.execute(
        "SELECT id FROM work_item "
        "WHERE change_id = ? AND component = ? AND step_kind = ?",
        (change_id, component, step_kind),
    ).fetchone()
    row_id = existing["id"] if existing else str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO work_item
            (id, change_id, component, step_kind, status, detail, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(change_id, component, step_kind) DO UPDATE
            SET status = excluded.status,
                detail = excluded.detail,
                updated_at = excluded.updated_at
        """,
        (
            row_id,
            change_id,
            component,
            step_kind,
            status,
            json.dumps(detail or {}),
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM work_item "
        "WHERE change_id = ? AND component = ? AND step_kind = ?",
        (change_id, component, step_kind),
    ).fetchone()
    return _work_row(row)


def get_work_items(
    conn: sqlite3.Connection,
    change_id: str,
    step_kind: str | None = None,
) -> list[dict]:
    """Return work items for a change, optionally filtered to one step kind."""
    if step_kind is None:
        rows = conn.execute(
            "SELECT * FROM work_item WHERE change_id = ? "
            "ORDER BY component, step_kind",
            (change_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM work_item WHERE change_id = ? AND step_kind = ? "
            "ORDER BY component",
            (change_id, step_kind),
        ).fetchall()
    return [_work_row(r) for r in rows]


def _work_row(row: sqlite3.Row) -> dict:
    """Row -> dict with detail parsed back from JSON."""
    out = _row_to_dict(row)
    try:
        out["detail"] = json.loads(out["detail"]) if out.get("detail") else {}
    except (TypeError, ValueError):
        out["detail"] = {}
    return out


# ---------------------------------------------------------------------------
# consumer_migration  (back-compat facades over work_item)
# ---------------------------------------------------------------------------
# These keep the ledger's public surface stable so the state machine, the API
# projections and the existing test suite need no edits.  The `component AS
# consumer` alias is what preserves the dict shape callers expect.
# ---------------------------------------------------------------------------

def upsert_consumer_migration(
    conn: sqlite3.Connection,
    change_id: str,
    consumer: str,
    status: str,
) -> dict:
    """Back-compat facade: a consumer migration is a work item with step_kind='migrate'."""
    upsert_work_item(conn, change_id, consumer, status, step_kind="migrate")
    row = conn.execute(
        "SELECT id, change_id, component AS consumer, status, updated_at "
        "FROM work_item "
        "WHERE change_id = ? AND component = ? AND step_kind = 'migrate'",
        (change_id, consumer),
    ).fetchone()
    return _row_to_dict(row)


def get_consumer_migrations(conn: sqlite3.Connection, change_id: str) -> list[dict]:
    """Back-compat facade returning rows keyed `consumer`, not `component`."""
    rows = conn.execute(
        "SELECT id, change_id, component AS consumer, status, updated_at "
        "FROM work_item "
        "WHERE change_id = ? AND step_kind = 'migrate' "
        "ORDER BY component",
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
