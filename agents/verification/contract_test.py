"""
agents/verification/contract_test.py
=====================================
contract-test — Verification agent for Interlock.

Runs the real pytest suite of a fixture repository (in a pre-prepared copy
at ``repo_path``) as a subprocess.  Returns real pass/fail evidence via
VerificationResult.

Contract:
- The caller is responsible for supplying a ``repo_path`` that is already in
  the desired state (e.g. post-migration copy in a tmp_path worktree).
- This agent NEVER modifies the repository; it only reads and runs tests.
- Output is never fabricated — test results come exclusively from the real
  pytest subprocess exit code and stdout/stderr.
- Never writes SQLite.  Never calls other agents.  Returns a structured result.
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from typing import Any

from orchestrator.schemas.common import Evidence
from orchestrator.schemas.verification import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pytest(repo_path: Path) -> tuple[int, str]:
    """
    Invoke pytest against ``repo_path`` as a subprocess.

    Returns (returncode, combined_output).  The combined output is the raw
    stdout+stderr from the pytest process — never synthesised.
    """
    cmd = [sys.executable, "-m", "pytest", str(repo_path), "-v", "--tb=short"]
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    combined = result.stdout + result.stderr
    return result.returncode, combined


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> VerificationResult:
    """
    Run the pytest suite inside ``repo_path`` and return a schema-valid
    VerificationResult.

    Parameters
    ----------
    data : dict with keys:
        - ``change_id``  (str, required)  — the change-request ID.
        - ``consumer``   (str, required)  — service name being tested,
          e.g. ``"checkout"``, ``"account-service"``.
        - ``commit_ref`` (str, optional)  — the migration commit SHA recorded
          for this consumer; stored in ``source_revision`` for staleness checks.

    repo_path : Path
        Path to the fixture's working directory (should be a tmp_path copy,
        NOT the real fixtures/ tree).

    Returns
    -------
    VerificationResult
        ``status="verified"``  if pytest exits 0.
        ``status="failed"``    if pytest exits non-zero (or cannot run).

    Raises
    ------
    ValueError
        If ``change_id`` or ``consumer`` are missing from ``data``.
    """
    repo_path = Path(repo_path)

    change_id: str = data.get("change_id", "")
    consumer: str = data.get("consumer", "")
    commit_ref: str | None = data.get("commit_ref")

    if not change_id:
        raise ValueError("data must contain a non-empty 'change_id'.")
    if not consumer:
        raise ValueError("data must contain a non-empty 'consumer'.")

    # -----------------------------------------------------------------------
    # Run pytest — real subprocess, output is never fabricated.
    # -----------------------------------------------------------------------
    returncode, output = _run_pytest(repo_path)

    status: str = "verified" if returncode == 0 else "failed"
    confidence: str = "confirmed" if returncode == 0 else "refuted"

    # -----------------------------------------------------------------------
    # Build evidence from the real subprocess result.
    # -----------------------------------------------------------------------
    evidence = Evidence(
        claim_type="test_result",
        subject=consumer,
        content={
            "returncode": returncode,
            "output": output,
            "repo_path": str(repo_path),
            "command": [sys.executable, "-m", "pytest", str(repo_path), "-v", "--tb=short"],
        },
        source_ref=str(repo_path),
        confidence=confidence,
        source_revision=commit_ref,
    )

    return VerificationResult(
        change_id=change_id,
        consumer=consumer,
        status=status,
        evidence=[evidence],
    )
