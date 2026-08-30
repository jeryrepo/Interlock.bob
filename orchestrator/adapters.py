"""
orchestrator/adapters.py
=========================
Translate real agent return shapes into the orchestrator's Pydantic contracts.

The agents in `agents/` were built to a different contract than
`orchestrator/schemas/`, and each one carries a `# SCHEMA INTEGRATION POINT`
comment marking where the translation was meant to go. This module is that
translation. Without it, wiring the real agents into `run_workflow()` fails
validation immediately:

    compatibility_strategy -> {affected_consumers, migration_steps, ...}
                              but PlanningResult wants {change_id, migration_order, evidence}

    provider_patch         -> {repository, commit_sha, status, ...}
                              but ImplementationResult wants {change_id, consumer, commit_ref, evidence}

Every function here is pure: dict in, dict out, no database, no subprocess. That
makes them testable against recorded agent output with no fixtures at all.

The most important line in this file is the `status != "success"` raise in
`implementation()`. It is the single point where "the agent said it failed"
could otherwise become "the orchestrator recorded a success", which is exactly
what AGENTS.md invariant 4 exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class ImplementationFailed(ValueError):
    """
    An implementation agent reported failure, carrying its own reason.

    Subclasses ValueError so existing `except ValueError` handlers still catch
    it, while callers that know about it can recover the agent's evidence
    instead of only the message.
    """

    def __init__(self, message: str, evidence: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.evidence = evidence or []


def _first_detail(raw: dict[str, Any]) -> str | None:
    """The most specific explanation the agent produced, if any."""
    for item in raw.get("evidence", []):
        content = item.get("content") or {}
        detail = content.get("detail") or content.get("error")
        if detail:
            return str(detail)
    return None


def discovery(raw: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Discovery agents already emit DiscoveryResult's shape.

    Only `change_id` needs filling in, because the agents are given the id but
    do not all echo it back.
    """
    out = dict(raw)
    out.setdefault("change_id", ctx["change_id"])
    out.setdefault("evidence", [])
    out.setdefault("dependencies", [])
    return out


def planning(raw: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    compatibility_strategy -> PlanningResult.

    `migration_steps`, `compatibility_requirements` and `verification_requirements`
    have no home in PlanningResult. Dropping them would discard real derived
    reasoning, so they are folded into a single `migration_status` evidence row
    and stay visible through GET /change-requests/{id}/evidence.
    """
    evidence = list(raw.get("evidence", []))
    evidence.append(
        {
            "claim_type": "migration_status",
            "subject": "migration-plan",
            "content": {
                "migration_steps": raw.get("migration_steps", []),
                "compatibility_requirements": raw.get("compatibility_requirements", []),
                "verification_requirements": raw.get("verification_requirements", []),
            },
            "source_ref": "agents/planning/compatibility_strategy.py",
            "confidence": "confirmed",
            # A plan describes intent, not code, so it legitimately has no
            # commit. The critic knows not to flag planning subjects for this.
            "source_revision": None,
        }
    )
    return {
        "change_id": ctx["change_id"],
        "migration_order": list(raw.get("affected_consumers", [])),
        "evidence": evidence,
    }


def implementation(raw: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    provider_patch / consumer_migration -> ImplementationResult.

    Raises if the agent reported anything other than success. The phase runner
    catches this, marks the work item failed, and the gate then blocks — which
    is the correct outcome. Silently mapping a failure onto a well-formed
    result would let unverified code through the gate.
    """
    status = raw.get("status")
    if status != "success":
        # Carry the agent's own explanation, not just the fact of failure. The
        # agent computed exactly why — "still references 'customer_id' in
        # worker.py" — and without this the ledger records only that something
        # returned failed, which is useless to a reader and to narration.
        reason = raw.get("summary") or _first_detail(raw) or "no reason recorded"
        raise ImplementationFailed(
            f"{ctx.get('role', 'implementation agent')} could not verify "
            f"{ctx.get('component') or raw.get('consumer') or 'the component'}: "
            f"{reason}",
            evidence=list(raw.get("evidence", [])),
        )

    # provider_patch returns no `consumer` key — only `repository`.
    consumer = (
        raw.get("consumer")
        or ctx.get("component")
        or Path(raw["repository"]).name
    )

    return {
        "change_id": ctx["change_id"],
        "consumer": consumer,
        "commit_ref": raw.get("commit_sha"),
        "evidence": list(raw.get("evidence", [])),
    }


def verification(raw: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    contract-test / coexistence-rehearsal / critic -> VerificationResult.

    These already match; only the identifying fields may need defaulting. Note
    the status is passed through untouched — a "failed" verification must stay
    failed.
    """
    out = dict(raw)
    out.setdefault("change_id", ctx["change_id"])
    out.setdefault("consumer", ctx.get("component") or "all")
    out.setdefault("evidence", [])
    return out
