"""
interlock_cli/core.py
======================
The verbs, as plain functions returning plain dicts.

Kept separate from `cli.py` so the same logic serves three surfaces without
duplication: the terminal, the MCP server that IBM Bob and other coding agents
call, and the GitHub Action. Each is a thin shell over these functions.

Everything here runs **in-process against a local SQLite ledger** — no server
required. That is what makes `interlock gate` usable in a pre-push hook or a CI
step, where standing up uvicorn would be absurd.

The one rule these functions must never break: the verdict comes from
`gate.evaluate_gate()` and nowhere else. The CLI does not re-derive, cache, or
second-guess it (AGENTS.md invariant 1).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

import orchestrator.gate as gate
import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.agent_runner import run_workflow
from orchestrator.schemas import ChangeSpec

_SPEC_ADAPTER = TypeAdapter(ChangeSpec)

# Exit codes. 0 and 1 carry meaning for CI: a NOT_PROVEN_SAFE gate must fail the
# build, and it must be distinguishable from the tool itself erroring.
EXIT_OK = 0
EXIT_NOT_PROVEN_SAFE = 1
EXIT_ERROR = 2


def open_ledger(db_path: str) -> sqlite3.Connection:
    return ledger.init_db(db_path)


def build_spec(
    kind: str,
    provider: str,
    old: str,
    new: str,
    components_root: str,
    topic: str | None = None,
    webhook_path: str | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    """
    Assemble and validate a ChangeSpec from flat CLI arguments.

    Validation happens here rather than at use time so a typo in `--kind` fails
    immediately with a clear message instead of surfacing as a mysteriously
    empty agent registry three phases later.
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "provider": provider,
        "components_root": components_root,
    }
    if kind == "transport_migration":
        payload.update(
            {
                "topic": topic or "events",
                "webhook_path": webhook_path or "/hooks",
                "old_symbol": old,
                "new_symbol": new,
            }
        )
    else:
        payload.update({"old_field": old, "new_field": new})
        if endpoint:
            payload["endpoint"] = endpoint
    return _SPEC_ADAPTER.validate_python(payload).model_dump()


def start(conn: sqlite3.Connection, description: str, spec: dict) -> dict[str, Any]:
    """Create a change and run agents up to the first human gate."""
    change_id = str(uuid.uuid4())
    ledger.create_change(conn, change_id, description)
    ledger.set_change_spec(conn, change_id, spec["kind"], spec)
    run_workflow(conn, change_id)
    return status(conn, change_id)


def approve(
    conn: sqlite3.Connection, change_id: str, gate_name: str, approved_by: str
) -> dict[str, Any]:
    """
    Record a human approval and continue the workflow.

    `legacy_removal` is re-checked against the gate here exactly as the API does
    it: a human must not be able to approve past an unverified consumer just
    because they used the terminal instead of the browser.
    """
    if gate_name == "legacy_removal":
        decision = gate.evaluate_gate(conn, change_id)
        if decision.result != "VERIFIED":
            raise PermissionError(
                f"gate is {decision.result}: {decision.reason}"
            )

    ledger.record_approval(conn, change_id, gate_name, approved_by)
    sm.advance(conn, change_id)
    if gate_name == "coordinate":
        run_workflow(conn, change_id)
    return status(conn, change_id)


def gate_status(conn: sqlite3.Connection, change_id: str) -> dict[str, Any]:
    """
    The deterministic verdict, read from the one place that computes it.

    Returns the recorded decision when there is one, otherwise a live preview
    marked `decided: false` — the same contract the HTTP projection uses, so a
    caller cannot mistake a preview for a settled verdict.
    """
    recorded = ledger.get_latest_gate_decision(conn, change_id)
    decision = gate.evaluate_gate(conn, change_id)
    return {
        "change_id": change_id,
        "decided": recorded is not None,
        "result": recorded["result"] if recorded else decision.result,
        "reason": recorded["reason"] if recorded else decision.reason,
        "required_consumers": decision.required_consumers,
        "unresolved": decision.unresolved,
        "work_items": [
            {
                "component": w["component"],
                "step_kind": w["step_kind"],
                "status": w["status"],
            }
            for w in ledger.get_work_items(conn, change_id)
        ],
    }


def status(conn: sqlite3.Connection, change_id: str) -> dict[str, Any]:
    change = ledger.get_change(conn, change_id)
    if change is None:
        raise KeyError(f"no such change: {change_id}")
    spec_row = ledger.get_change_spec(conn, change_id)
    return {
        "change_id": change_id,
        "description": change["description"],
        "state": change["status"],
        "kind": spec_row["kind"] if spec_row else None,
        "gate": gate_status(conn, change_id),
    }


def evidence(conn: sqlite3.Connection, change_id: str) -> list[dict[str, Any]]:
    return ledger.get_evidence(conn, change_id)


def graph(conn: sqlite3.Connection, change_id: str) -> dict[str, Any]:
    return gate.build_graph(conn, change_id)


def changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, description, status, updated_at FROM change_request "
        "ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def check(
    conn: sqlite3.Connection,
    description: str,
    spec: dict,
    auto_approve_coordination: bool = True,
) -> dict[str, Any]:
    """
    One-shot: run a change as far as the deterministic gate allows.

    This is the PR-time entry point. Coordination is auto-approved because in
    CI there is no human at a terminal — but `legacy_removal` is NOT, and never
    will be: that is the approval that authorises destroying the old field, and
    it stays human.
    """
    result = start(conn, description, spec)
    change_id = result["change_id"]
    if auto_approve_coordination and result["state"] == "COORDINATE":
        approve(conn, change_id, "coordinate", "interlock-cli")
    return status(conn, change_id)
