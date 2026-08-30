"""
frontend/utils/derive.py
=========================
Pure presentation helpers.

Everything here is a *projection* of data the backend already returned.
No value is invented, no gate is computed, no result is assumed successful.
If the backend has not produced a fact, these helpers return empty/None and
the UI renders an explicit "not yet" state.
"""

from __future__ import annotations

from typing import Any

# Workflow states in order, mirroring orchestrator/state_machine.py STATES.
# Used only for rendering a progress rail; the backend remains the sole
# authority on the current state.
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

STATE_CAPTIONS: dict[str, str] = {
    "INTAKE": "Change request accepted",
    "DISCOVERY": "Agents mapping dependencies",
    "PLANNING": "Building compatibility strategy",
    "COORDINATE": "Waiting for human coordinate approval",
    "MODIFY": "Patching provider and consumers",
    "REHEARSE": "Docker coexistence rehearsal",
    "VERIFY": "Running contract tests and critic",
    "GATE_DECISION": "Deterministic safety gate evaluated",
    "APPROVE": "Waiting for human legacy-removal approval",
    "DONE": "Legacy field removal approved",
}

# claim_type -> (phase label, terminal accent)
_CLAIM_PHASE: dict[str, str] = {
    "dependency": "discovery",
    "migration_status": "implementation",
    "test_result": "verification",
    "risk": "critic",
}


def state_index(state: str) -> int:
    """Index of a state in STATES, or -1 if unknown."""
    try:
        return STATES.index(state)
    except ValueError:
        return -1


def state_progress(state: str) -> float:
    """Fraction of the workflow completed, for a progress bar."""
    idx = state_index(state)
    if idx < 0:
        return 0.0
    return (idx + 1) / len(STATES)


# ---------------------------------------------------------------------------
# Agent activity feed
# ---------------------------------------------------------------------------


def _phase_for(row: dict) -> str:
    claim = row.get("claim_type", "")
    subject = row.get("subject", "")
    if claim == "migration_status" and subject == "migration-plan":
        return "planning"
    return _CLAIM_PHASE.get(claim, "agent")


def _summarise_content(content: Any) -> str:
    """One-line rendering of an evidence content blob."""
    if not isinstance(content, dict):
        return str(content)
    parts: list[str] = []
    for key, value in content.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value) or "none"
        parts.append(f"{key}={value}")
    return "  ".join(parts)


def build_activity_feed(evidence: list[dict]) -> list[dict]:
    """
    Turn evidence rows into terminal-style events, oldest first.

    Each event carries the backend's own confidence value.  A ``hypothesis``
    row is rendered as a discovery signal, not as a confirmed fact.
    """
    events: list[dict] = []
    for row in evidence:
        confidence = row.get("confidence", "")
        content = row.get("content") or {}
        subject = row.get("subject", "?")
        phase = _phase_for(row)

        is_skipped = isinstance(content, dict) and content.get("skipped") is True
        if confidence == "hypothesis" and not is_skipped:
            outcome = "hidden dependency discovered"
            level = "alert"
        elif confidence == "hypothesis" and is_skipped:
            outcome = "skipped (not proven)"
            level = "alert"
        elif confidence == "refuted":
            outcome = "refuted"
            level = "error"
        else:
            outcome = "completed"
            level = "ok"

        events.append(
            {
                "phase": phase,
                "subject": subject,
                "claim_type": row.get("claim_type", ""),
                "outcome": outcome,
                "level": level,
                "confidence": confidence,
                "detail": _summarise_content(content),
                "source_ref": row.get("source_ref", ""),
                "created_at": row.get("created_at", ""),
            }
        )
    return events


# ---------------------------------------------------------------------------
# Graph projections
# ---------------------------------------------------------------------------

UNDOCUMENTED_EDGE_TYPES = {"undocumented"}


def split_consumers(graph: dict | None) -> tuple[list[str], list[str]]:
    """
    Split graph consumers into (documented, undocumented) by edge type.

    Returns empty lists when no graph is available — never a guess.
    """
    if not graph:
        return [], []
    documented: set[str] = set()
    undocumented: set[str] = set()
    for edge in graph.get("edges", []):
        target = edge.get("to")
        if not target:
            continue
        if edge.get("edge_type") in UNDOCUMENTED_EDGE_TYPES:
            undocumented.add(target)
        else:
            documented.add(target)
    # A component proven undocumented stays in that bucket.
    documented -= undocumented
    return sorted(documented), sorted(undocumented)


def hidden_dependencies(graph: dict | None) -> list[dict]:
    """Return the undocumented edges, the centrepiece of the demo story."""
    if not graph:
        return []
    return [
        e
        for e in graph.get("edges", [])
        if e.get("edge_type") in UNDOCUMENTED_EDGE_TYPES
    ]


# ---------------------------------------------------------------------------
# Evidence projections
# ---------------------------------------------------------------------------


def evidence_by_type(evidence: list[dict]) -> dict[str, list[dict]]:
    """Group evidence rows by claim_type, preserving order."""
    grouped: dict[str, list[dict]] = {}
    for row in evidence:
        grouped.setdefault(row.get("claim_type", "unknown"), []).append(row)
    return grouped


def verification_results(evidence: list[dict]) -> list[dict]:
    """Evidence rows recording test outcomes."""
    return [r for r in evidence if r.get("claim_type") == "test_result"]


def contract_test_results(evidence: list[dict]) -> list[dict]:
    """
    Test-result rows that explicitly report a suite outcome.

    The backend's current verifier records process ``returncode`` while older
    evidence records ``tests_passed``.  The coexistence rehearsal is also a
    ``test_result`` row, but has its own passport line and is excluded here so
    it cannot inflate the contract-test count.
    """
    return [
        r
        for r in verification_results(evidence)
        if r.get("subject") not in {"coexistence", "coexistence-rehearsal"}
        and (
            "tests_passed" in (r.get("content") or {})
            or isinstance((r.get("content") or {}).get("returncode"), int)
        )
    ]


def evidence_result_passed(row: dict) -> bool:
    """Project either supported backend test-result shape to a pass boolean."""
    content = row.get("content") or {}
    if "tests_passed" in content:
        return content.get("tests_passed") is True
    return content.get("returncode") == 0


def coexistence_result(evidence: list[dict]) -> dict | None:
    """The coexistence rehearsal evidence row, if the backend produced one."""
    for row in evidence:
        if row.get("subject") in {"coexistence", "coexistence-rehearsal"}:
            return row
    return None


def critic_assessment(evidence: list[dict]) -> dict | None:
    """The critic's risk evidence row, if present."""
    for row in evidence:
        if row.get("claim_type") == "risk":
            return row
    return None


# ---------------------------------------------------------------------------
# Migration progress
# ---------------------------------------------------------------------------

MIGRATION_STATUS_ICON = {
    "pending": "○",
    "in_progress": "◐",
    "verified": "●",
    "failed": "✕",
}


def migration_progress(gate: dict | None) -> tuple[int, int]:
    """Return (verified_count, total_count) from the backend gate projection."""
    if not gate:
        return 0, 0
    consumers = gate.get("consumers", []) or []
    verified = sum(1 for c in consumers if c.get("status") == "verified")
    return verified, len(consumers)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def approvals_by_gate(approvals: dict | None) -> dict[str, dict]:
    """Map gate name -> approval row for the approvals the backend recorded."""
    if not approvals:
        return {}
    return {a["gate"]: a for a in approvals.get("approvals", [])}
