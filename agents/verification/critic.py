"""
agents/verification/critic.py
==============================
critic — Verification agent for Interlock.

Reads the orchestrator's evidence snapshot via a read-only HTTP call to
``GET /change-requests/{id}/evidence`` and flags evidence-quality issues.

Contract:
- ONLY emits ``claim_type="risk"`` Evidence items.
- MUST NOT decide the safety gate (VERIFIED / NOT_PROVEN_SAFE).
  That decision belongs exclusively to ``orchestrator/gate.py::evaluate_gate()``.
- MUST NOT write to the ledger (no SQLite imports, no ledger calls).
- MUST NOT call other agents.
- Evidence-quality status returned:
    ``status="verified"``  → no quality problems found in the evidence snapshot.
    ``status="failed"``    → one or more quality problems detected.

Checks performed:
  1. Missing migration evidence for a required consumer.
  2. Migration evidence with no ``source_revision`` (no commit SHA).
  3. Stale test_result evidence: ``created_at`` older than the latest
     known migration commit timestamp.

The base URL for the orchestrator API is read from:
  1. The ``base_url`` parameter passed to ``run()``.
  2. The ``INTERLOCK_API_URL`` environment variable (fallback).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

try:
    import httpx as _http_lib
    _USE_HTTPX = True
except ImportError:
    import urllib.request as _urllib_request  # type: ignore
    import json as _json
    _USE_HTTPX = False

from orchestrator.schemas.common import Evidence
from orchestrator.schemas.verification import VerificationResult


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_evidence(change_id: str, base_url: str) -> dict[str, Any]:
    """
    Call ``GET {base_url}/change-requests/{change_id}/evidence`` and return
    the parsed JSON body as a plain dict.

    Raises ``RuntimeError`` if the HTTP call fails or returns a non-200 status.
    """
    url = f"{base_url.rstrip('/')}/change-requests/{change_id}/evidence"

    if _USE_HTTPX:
        response = _http_lib.get(url, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(
                f"GET {url} returned HTTP {response.status_code}: {response.text}"
            )
        return response.json()
    else:
        # Fallback: stdlib urllib
        req = _urllib_request.Request(url)
        try:
            with _urllib_request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"GET {url} failed: {exc}") from exc
        return _json.loads(body)


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime, or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _check_missing_consumers(
    evidence_items: list[dict],
    required_consumers: list[str],
    change_id: str,
) -> list[Evidence]:
    """
    Flag a risk for each required consumer that has no ``migration_status``
    evidence entry in the snapshot.
    """
    risks: list[Evidence] = []
    migrated_consumers = {
        item["subject"]
        for item in evidence_items
        if item.get("claim_type") == "migration_status"
    }
    for consumer in required_consumers:
        if consumer not in migrated_consumers:
            risks.append(
                Evidence(
                    claim_type="risk",
                    subject=consumer,
                    content={
                        "risk": "missing_migration_evidence",
                        "detail": (
                            f"No migration_status evidence found for required consumer "
                            f"'{consumer}' in change '{change_id}'."
                        ),
                    },
                    source_ref=f"change:{change_id}",
                    confidence="confirmed",
                    source_revision=None,
                )
            )
    return risks


def _check_missing_commit_refs(
    evidence_items: list[dict],
    change_id: str,
) -> list[Evidence]:
    """
    Flag a risk for any ``migration_status`` evidence item that lacks a
    ``source_revision`` (i.e. no real commit SHA was recorded).

    Planning and orchestration metadata entries are skipped. Component names
    are learned from evidence instead of being hardcoded, so a newly discovered
    consumer receives the same scrutiny automatically.
    """
    risks: list[Evidence] = []
    for item in evidence_items:
        if item.get("claim_type") != "migration_status":
            continue
        subject = item.get("subject", "")
        if subject == "migration-plan" or subject.startswith("_"):
            continue
        if not item.get("source_revision"):
            risks.append(
                Evidence(
                    claim_type="risk",
                    subject=item.get("subject", "unknown"),
                    content={
                        "risk": "no_commit_ref",
                        "detail": (
                            f"Migration evidence for '{item.get('subject', 'unknown')}' "
                            f"has no source_revision (commit SHA). "
                            f"Migration may not represent a real commit."
                        ),
                        "evidence_id": item.get("id"),
                    },
                    source_ref=f"change:{change_id}",
                    confidence="confirmed",
                    source_revision=None,
                )
            )
    return risks


def _check_stale_test_evidence(
    evidence_items: list[dict],
    latest_migration_commit_ts: str | None,
    change_id: str,
) -> list[Evidence]:
    """
    Flag a risk for any ``test_result`` evidence item whose ``created_at``
    timestamp is older than ``latest_migration_commit_ts``.

    If ``latest_migration_commit_ts`` is not provided, this check is skipped.
    """
    risks: list[Evidence] = []
    if not latest_migration_commit_ts:
        return risks

    cutoff = _parse_iso(latest_migration_commit_ts)
    if cutoff is None:
        return risks

    for item in evidence_items:
        if item.get("claim_type") != "test_result":
            continue
        created_at = _parse_iso(item.get("created_at"))
        if created_at is None:
            continue
        if created_at < cutoff:
            risks.append(
                Evidence(
                    claim_type="risk",
                    subject=item.get("subject", "unknown"),
                    content={
                        "risk": "stale_test_evidence",
                        "detail": (
                            f"test_result evidence for '{item.get('subject', 'unknown')}' "
                            f"was recorded at {item.get('created_at')} which is older than "
                            f"the latest migration commit at {latest_migration_commit_ts}. "
                            f"Re-run contract-test against the migrated code."
                        ),
                        "evidence_created_at": item.get("created_at"),
                        "latest_migration_commit_ts": latest_migration_commit_ts,
                        "evidence_id": item.get("id"),
                    },
                    source_ref=f"change:{change_id}",
                    confidence="confirmed",
                    source_revision=None,
                )
            )
    return risks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    data: dict[str, Any],
    base_url: str | None = None,
) -> VerificationResult:
    """
    Inspect the orchestrator's evidence snapshot and flag quality issues.

    Parameters
    ----------
    data : dict with keys:
        - ``change_id``                  (str, required)
        - ``consumer``                   (str, optional, default ``"critic"``)
        - ``required_consumers``         (list[str], optional)  — consumers that
          MUST have a ``migration_status`` evidence entry.
        - ``latest_migration_commit_ts`` (str, optional)  — ISO-8601 timestamp
          of the most recent implementation commit; used to detect stale
          ``test_result`` evidence.
    base_url : str | None
        Base URL of the orchestrator API, e.g. ``"http://localhost:8000"``.
        Falls back to the ``INTERLOCK_API_URL`` environment variable.

    Returns
    -------
    VerificationResult
        ``status="verified"``  if no evidence-quality problems were found.
        ``status="failed"``    if one or more risk items were flagged.
        ``evidence``           contains only ``claim_type="risk"`` items.

        The status here reflects EVIDENCE QUALITY, not migration safety.
        The safety gate is decided exclusively by
        ``orchestrator/gate.py::evaluate_gate()``.

    Raises
    ------
    ValueError
        If ``change_id`` is missing from ``data`` or no base URL is available.
    RuntimeError
        If the HTTP call to the orchestrator fails.
    """
    change_id: str = data.get("change_id", "")
    consumer: str = data.get("consumer", "critic")
    required_consumers: list[str] = data.get("required_consumers", [])
    latest_migration_commit_ts: str | None = data.get("latest_migration_commit_ts")

    if not change_id:
        raise ValueError("data must contain a non-empty 'change_id'.")

    resolved_url = base_url or os.environ.get("INTERLOCK_API_URL", "")
    if not resolved_url:
        raise ValueError(
            "base_url must be provided as a parameter or via the "
            "INTERLOCK_API_URL environment variable."
        )

    # -----------------------------------------------------------------------
    # Fetch evidence from the orchestrator (read-only HTTP call).
    # -----------------------------------------------------------------------
    payload = _get_evidence(change_id, resolved_url)
    evidence_items: list[dict] = payload.get("evidence", [])

    # -----------------------------------------------------------------------
    # Run checks — build risk evidence items.
    # -----------------------------------------------------------------------
    risks: list[Evidence] = []

    risks.extend(
        _check_missing_consumers(evidence_items, required_consumers, change_id)
    )
    risks.extend(
        _check_missing_commit_refs(evidence_items, change_id)
    )
    risks.extend(
        _check_stale_test_evidence(evidence_items, latest_migration_commit_ts, change_id)
    )

    # All emitted evidence must be claim_type="risk" — enforced at construction
    # above, but assert here for belt-and-suspenders.
    assert all(e.claim_type == "risk" for e in risks), (
        "Critic emitted non-risk evidence — programming error."
    )

    status = "failed" if risks else "verified"

    return VerificationResult(
        change_id=change_id,
        consumer=consumer,
        status=status,
        evidence=risks,
    )
