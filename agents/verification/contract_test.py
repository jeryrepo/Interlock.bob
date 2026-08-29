"""
agents/verification/contract_test.py
=====================================
The contract-test agent: runs a component's *real* test suite and reports what
actually happened.

Why this exists
---------------
The deterministic gate (``orchestrator/gate.evaluate_gate``) refuses to
authorise legacy removal until every required consumer is ``verified``. Nothing
else in the system sets that status from evidence of a real execution — the
implementation agents prove that code *changed* (a real commit SHA), not that it
still *works*. This agent is what turns "migrated" into "proven safe".

It is also the state machine's unblocking step: ``can_advance("REHEARSE")``
requires at least one ``test_result`` evidence row, and ``can_advance("VERIFY")``
requires every migration to reach a terminal status. Both come from here.

What it must NOT do
-------------------
- **Never fabricate a result.** A non-zero pytest exit is ``status="failed"``,
  always. An unrunnable suite is ``status="failed"``, never "verified by
  default". Converting either into a pass would defeat the entire product claim
  (AGENTS.md invariant 4).
- **Never write to the ledger.** It returns a validated structure; the
  orchestrator persists it (invariant 2).
- **Never call another agent** (invariant 3).
- **Never decide the gate.** It reports one component's test outcome. The gate
  reads statuses; it does not ask this agent for a verdict (invariant 1).

Distinguishing "tests failed" from "tests could not run" matters: both block the
gate, but only the second one means the evidence is missing rather than damning.
``content["outcome"]`` carries that distinction.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How much pytest output to retain in evidence. Enough to diagnose a failure,
# bounded so a pathological suite cannot bloat the ledger.
_OUTPUT_TAIL_CHARS = 4000

_OUTCOME_PASSED = "tests_passed"
_OUTCOME_FAILED = "tests_failed"
_OUTCOME_NOT_RUN = "tests_could_not_run"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pytest(repo_path: Path) -> tuple[int, str]:
    """
    Run pytest inside *repo_path*; return ``(returncode, combined_output)``.

    Mirrors the helper in ``agents/implementation/provider_patch.py`` so the two
    agents execute tests identically — a consumer that passes during migration
    must not fail here for reasons of invocation.
    """
    cmd = [sys.executable, "-m", "pytest", str(repo_path), "-v", "--tb=short"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def _head_revision(repo_path: Path) -> str | None:
    """
    Return the commit SHA the tests ran against, or None if unavailable.

    Best-effort by design: a component directory is not required to be a git
    repository. Returning None is honest; inventing a SHA is not. The critic's
    stale-evidence check depends on this being real when it is present.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# Agent entry point
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """
    Execute *repo_path*'s test suite and return a VerificationResult-shaped dict.

    Parameters
    ----------
    data : dict with keys:
        - change_id : str (required)
        - consumer  : str, optional. Defaults to the directory name of
          ``repo_path``, so the caller need not restate it.
    repo_path : Path
        Component repository whose own tests are executed.

    Returns
    -------
    dict validating against ``orchestrator.schemas.VerificationResult``:
    ``{change_id, consumer, status, evidence}``.

    ``status`` is ``"verified"`` only when pytest exits 0. Every other
    outcome — failures, collection errors, a missing directory — is
    ``"failed"``, with ``content["outcome"]`` distinguishing which.
    """
    change_id = data["change_id"]
    repo_path = Path(repo_path)
    consumer = data.get("consumer") or repo_path.name

    if not repo_path.is_dir():
        return _result(
            change_id,
            consumer,
            passed=False,
            outcome=_OUTCOME_NOT_RUN,
            exit_code=None,
            output=f"repository path does not exist: {repo_path}",
            source_ref=str(repo_path),
            source_revision=None,
        )

    exit_code, output = _run_pytest(repo_path)
    passed = exit_code == 0

    # pytest exit code 5 means "no tests collected". That is not a pass — an
    # empty suite proves nothing, and treating it as success would let a
    # consumer with no tests sail through the gate.
    outcome = _OUTCOME_PASSED if passed else (
        _OUTCOME_NOT_RUN if exit_code == 5 else _OUTCOME_FAILED
    )

    return _result(
        change_id,
        consumer,
        passed=passed,
        outcome=outcome,
        exit_code=exit_code,
        output=output,
        source_ref=str(repo_path),
        source_revision=_head_revision(repo_path),
    )


def _result(
    change_id: str,
    consumer: str,
    *,
    passed: bool,
    outcome: str,
    exit_code: int | None,
    output: str,
    source_ref: str,
    source_revision: str | None,
) -> dict[str, Any]:
    """Build the VerificationResult payload. Single construction point."""
    return {
        "change_id": change_id,
        "consumer": consumer,
        "status": "verified" if passed else "failed",
        "evidence": [
            {
                "claim_type": "test_result",
                "subject": consumer,
                "content": {
                    "tests_passed": passed,
                    "outcome": outcome,
                    "exit_code": exit_code,
                    "output_tail": output[-_OUTPUT_TAIL_CHARS:],
                },
                "source_ref": source_ref,
                "confidence": "confirmed",
                "source_revision": source_revision,
            }
        ],
    }
