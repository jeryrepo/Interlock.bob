"""
agents/verification/webhook_quiet.py
=====================================
The webhook-quiet agent: proves a subscriber has actually stopped using the
retired webhook transport.

Why this exists
---------------
A transport cut-over is not finished when the subscriber *can* consume from
pub/sub. It is finished when it no longer uses the webhook — otherwise
retiring the webhook path still breaks it. That is why
`gate._REQUIRED_STEP_KINDS["transport_migration"]` demands two verified work
items per subscriber: `subscribe` (it moved) and `webhook_quiet` (it drained).

The gate never learns what quiescence means. It counts verified work items; this
agent decides what "drained" is. That separation is ADR-0002.

Two independent checks, both required
-------------------------------------
1. **Source**: the subscriber no longer references the retired symbol. A service
   whose code still calls the webhook is not drained, whatever its traffic
   happens to look like right now.
2. **Traffic**: its recorded activity shows zero calls to the webhook path
   within the quiet window.

Requiring both matters. Source alone would pass a service that was redeployed
but still has in-flight callers; traffic alone would pass a service that is
merely idle and would resume calling the webhook on its next event.

What it must NOT do
-------------------
- **Never treat missing evidence as quiet.** No activity record means not
  proven, which is `status="failed"` — an absent log is not a zero (AGENTS.md
  invariant 4).
- Never write to the ledger (invariant 2), call another agent (invariant 3), or
  decide the gate (invariant 1).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ACTIVITY_FILENAME = "webhook_activity.json"
_SOURCE_SUFFIXES = {".py"}
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "tests"}

_OUTCOME_DRAINED = "webhook_drained"
_OUTCOME_ACTIVE = "webhook_still_active"
_OUTCOME_NOT_RUN = "quiescence_not_proven"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _source_references(repo_path: Path, symbol: str) -> list[str]:
    """Files whose source still mentions *symbol* as a standalone identifier."""
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    hits: list[str] = []
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(repo_path).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            hits.append(str(path.relative_to(repo_path)))
    return hits


def _read_activity(repo_path: Path) -> dict[str, Any] | None:
    """Parse the subscriber's webhook activity record, or None if unusable."""
    path = repo_path / _ACTIVITY_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Agent entry point
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """
    Decide whether this subscriber has drained off the retired webhook.

    Parameters
    ----------
    data : dict with keys:
        - change_id  : str (required)
        - consumer   : str, optional. Defaults to the directory name.
        - old_field  : str, the retired transport symbol. The orchestrator maps
          `TransportMigrationSpec.old_symbol` onto this key for every agent.
    repo_path : Path
        The subscriber's component directory.

    Returns
    -------
    dict validating against `orchestrator.schemas.VerificationResult`.
    `status` is "verified" only when the source is clean AND the recorded
    traffic in the quiet window is zero.
    """
    change_id = data["change_id"]
    repo_path = Path(repo_path)
    consumer = data.get("consumer") or repo_path.name
    retired_symbol = data.get("old_field") or "deliver_via_webhook"

    if not repo_path.is_dir():
        return _result(change_id, consumer, _OUTCOME_NOT_RUN, {
            "detail": f"component directory not found: {repo_path}",
        })

    still_referenced = _source_references(repo_path, retired_symbol)
    activity = _read_activity(repo_path)

    if activity is None:
        # Absence of a record is not evidence of quiet.
        return _result(change_id, consumer, _OUTCOME_NOT_RUN, {
            "detail": (
                f"no readable {_ACTIVITY_FILENAME}; webhook quiescence cannot be "
                f"proven for '{consumer}'"
            ),
            "source_references": still_referenced,
        })

    calls = activity.get("calls_in_window")
    window = activity.get("window_seconds")

    if not isinstance(calls, int):
        return _result(change_id, consumer, _OUTCOME_NOT_RUN, {
            "detail": f"{_ACTIVITY_FILENAME} has no numeric 'calls_in_window'",
            "activity": activity,
        })

    reasons: list[str] = []
    if still_referenced:
        reasons.append(
            f"source still references {retired_symbol!r} in: "
            f"{', '.join(still_referenced)}"
        )
    if calls > 0:
        reasons.append(
            f"{calls} webhook call(s) recorded in the last {window}s window"
        )

    if reasons:
        return _result(change_id, consumer, _OUTCOME_ACTIVE, {
            "detail": "; ".join(reasons),
            "calls_in_window": calls,
            "window_seconds": window,
            "source_references": still_referenced,
        })

    return _result(change_id, consumer, _OUTCOME_DRAINED, {
        "detail": (
            f"'{consumer}' no longer references {retired_symbol!r} and recorded "
            f"0 webhook calls in the last {window}s"
        ),
        "calls_in_window": 0,
        "window_seconds": window,
        "source_references": [],
    })


def _result(
    change_id: str, consumer: str, outcome: str, content: dict[str, Any]
) -> dict[str, Any]:
    """Build the VerificationResult payload. Single construction point."""
    drained = outcome == _OUTCOME_DRAINED
    return {
        "change_id": change_id,
        "consumer": consumer,
        "status": "verified" if drained else "failed",
        "evidence": [
            {
                "claim_type": "test_result",
                "subject": consumer,
                "content": {"tests_passed": drained, "outcome": outcome, **content},
                "source_ref": str(Path(consumer) / _ACTIVITY_FILENAME),
                "confidence": "confirmed",
                "source_revision": None,
            }
        ],
    }
