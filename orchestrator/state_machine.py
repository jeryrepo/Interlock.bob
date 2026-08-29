"""
orchestrator/state_machine.py
==============================
Persisted state machine for Interlock change-request workflow.

States (in order):
  INTAKE → DISCOVERY → PLANNING → COORDINATE → MODIFY
  → REHEARSE → VERIFY → GATE_DECISION → APPROVE → DONE

Rules:
- Current state lives in change_request.status (written by ledger).
- can_advance() reads only ledger facts — never trusts agent claims.
- advance() is the only public mutator; force_state() is for tests only.
- InvalidTransition is raised on any illegal move.
"""

from __future__ import annotations

import sqlite3

import orchestrator.ledger as ledger

# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

STATES: list[str] = [
    "INTAKE",
    "DISCOVERY",
    "PLANNING",
    "COORDINATE",
    "MODIFY",
    "REHEARSE",
    "VERIFY",
    "GATE_DECISION",
    "APPROVE",
    "DONE",
]

# Maps each state to its single legal successor.
TRANSITIONS: dict[str, str] = {
    STATES[i]: STATES[i + 1] for i in range(len(STATES) - 1)
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

# Step kinds that are proved during VERIFY rather than MODIFY. They are
# expected to still be "pending" when MODIFY completes.
_PROVED_AFTER_MODIFY: frozenset[str] = frozenset({"webhook_quiet"})


class InvalidTransition(Exception):
    """Raised when an advance is attempted that cannot succeed."""


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_state(conn: sqlite3.Connection, change_id: str) -> str:
    """Return the current state string for a change, or raise if not found."""
    row = ledger.get_change(conn, change_id)
    if row is None:
        raise InvalidTransition(f"change_id '{change_id}' not found")
    return row["status"]


# ---------------------------------------------------------------------------
# Advancement rules
# ---------------------------------------------------------------------------

def can_advance(conn: sqlite3.Connection, change_id: str, current_state: str) -> bool:
    """
    Return True if the change is ready to leave *current_state*.

    All decisions are made from ledger facts only.
    """
    if current_state == "INTAKE":
        # Always ready to proceed from intake once the row exists.
        return ledger.get_change(conn, change_id) is not None

    if current_state == "DISCOVERY":
        # Need at least one dependency edge to confirm discovery ran.
        deps = ledger.get_dependencies(conn, change_id)
        return len(deps) > 0

    if current_state == "PLANNING":
        # Need at least one work item (planning seeded them).
        #
        # Reads work items across ALL step kinds, not just "migrate": a
        # transport migration seeds "subscribe" and "webhook_quiet" instead, and
        # filtering to "migrate" made those changes unable to leave PLANNING.
        return len(ledger.get_work_items(conn, change_id)) > 0

    if current_state == "COORDINATE":
        # Requires a human approval with gate='coordinate'.
        approvals = ledger.get_approvals(conn, change_id)
        return any(a["gate"] == "coordinate" for a in approvals)

    if current_state == "MODIFY":
        # Every work item must be at least in_progress.
        #
        # Steps that no MODIFY-phase agent touches — "webhook_quiet" is proved
        # in VERIFY, not MODIFY — would otherwise sit at "pending" forever and
        # stall the change. They are excluded here and still gated later: the
        # gate requires them verified regardless.
        items = [
            w for w in ledger.get_work_items(conn, change_id)
            if w["step_kind"] not in _PROVED_AFTER_MODIFY
        ]
        if not items:
            return False
        terminal = {"in_progress", "verified", "failed"}
        return all(w["status"] in terminal for w in items)

    if current_state == "REHEARSE":
        # Coexistence rehearsal evidence must exist.
        evidence = ledger.get_evidence(conn, change_id)
        return any(e["claim_type"] == "test_result" for e in evidence)

    if current_state == "VERIFY":
        # Every work item must have reached a terminal status.
        items = ledger.get_work_items(conn, change_id)
        if not items:
            return False
        done = {"verified", "failed"}
        return all(w["status"] in done for w in items)

    if current_state == "GATE_DECISION":
        # Gate decision must have been recorded and result must be VERIFIED.
        decision = ledger.get_latest_gate_decision(conn, change_id)
        return decision is not None and decision["result"] == "VERIFIED"

    if current_state == "APPROVE":
        # Requires a human approval with gate='legacy_removal'.
        approvals = ledger.get_approvals(conn, change_id)
        return any(a["gate"] == "legacy_removal" for a in approvals)

    if current_state == "DONE":
        return False  # Terminal state — nothing to advance to.

    raise InvalidTransition(f"Unknown state: '{current_state}'")


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------

def advance(conn: sqlite3.Connection, change_id: str) -> str:
    """
    Attempt to advance the change to its next state.

    Returns the new state name on success.
    Raises InvalidTransition if advancement is not yet permitted or if the
    change is already in a terminal state.
    """
    current = get_state(conn, change_id)

    if current not in TRANSITIONS:
        raise InvalidTransition(f"State '{current}' has no successor (terminal).")

    if not can_advance(conn, change_id, current):
        raise InvalidTransition(
            f"Cannot advance from '{current}': preconditions not met."
        )

    next_state = TRANSITIONS[current]
    ledger.update_change_status(conn, change_id, next_state)
    return next_state


def force_state(conn: sqlite3.Connection, change_id: str, state: str) -> None:
    """
    Unconditionally set the state.  For use in tests and stub workflows only.
    Do NOT call this from production request handlers.
    """
    if state not in STATES:
        raise InvalidTransition(f"Unknown state: '{state}'")
    ledger.update_change_status(conn, change_id, state)
