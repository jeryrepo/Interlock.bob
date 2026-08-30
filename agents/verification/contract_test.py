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

from orchestrator.manifest import ComponentManifest
from orchestrator.manifest import environment_for, missing_program, resolve_program
from orchestrator.manifest import load as manifest_for

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How much pytest output to retain in evidence. Enough to diagnose a failure,
# bounded so a pathological suite cannot bloat the ledger.
_OUTPUT_TAIL_CHARS = 4000

# A component declares its own test command, so this process cannot know that
# the command terminates. Bounded, and expiry is never a pass.
_TEST_TIMEOUT_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 60

_OUTCOME_PASSED = "tests_passed"
_OUTCOME_FAILED = "tests_failed"
_OUTCOME_NOT_RUN = "tests_could_not_run"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_tests(repo_path: Path) -> tuple[int, str, ComponentManifest]:
    """
    Run the component's own test suite; return ``(returncode, output, manifest)``.

    The command comes from the component's ``interlock.toml`` when it declares
    one, and falls back to pytest otherwise. That fallback is what keeps the
    existing Python components working with no manifest at all.

    Reading the command from the component is what makes this agent
    language-agnostic: it never needs to understand C, Go or Rust, only whether
    that component's suite passed. Hardcoding ``python -m pytest`` here was the
    single line that confined Interlock to Python codebases.

    The command is executed WITHOUT a shell, so a manifest cannot smuggle in a
    pipe or a second command.
    """
    manifest = manifest_for(repo_path)
    # The default pytest command is hermetic (private basetemp, no cache) —
    # see ComponentManifest.command() for why that matters.
    cmd = manifest.command()
    missing = missing_program(cmd)
    if missing:
        # Named explicitly. "tests failed (exit 127)" reads as "your code is
        # broken" when the truth is that the runner is not installed or not on
        # PATH - a distinction the author of the component cannot act on
        # unless it is stated.
        return 127, (
            f"cannot run this component's tests: {missing!r} was not found on "
            f"PATH. Install it, or correct test_command in interlock.toml. "
            f"Command was {cmd!r}."
        ), manifest
    try:
        result = subprocess.run(
            resolve_program(cmd), cwd=str(repo_path), capture_output=True,
            text=True, timeout=_TEST_TIMEOUT_SECONDS, env=environment_for(cmd),
        )
    except subprocess.TimeoutExpired:
        # 124 is the conventional timeout exit code, and run() maps a non-zero
        # exit that is not 5 to "tests_failed" — so a hung suite blocks the gate.
        return 124, (
            f"test command {cmd!r} timed out after {_TEST_TIMEOUT_SECONDS}s"
        ), manifest
    except OSError as exc:
        # A declared command that cannot be executed at all — a missing `make`,
        # a bad path. Not a test failure; a test run that never happened.
        return 127, f"could not execute {cmd!r}: {exc}", manifest
    return result.returncode, result.stdout + result.stderr, manifest


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
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
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

    exit_code, output, manifest = _run_tests(repo_path)
    passed = exit_code == 0

    # Exit 5 means "no tests collected" to pytest, and nothing in particular to
    # `make test`, so the special case only applies to the built-in default. An
    # empty suite proves nothing either way and must never read as a pass.
    if passed:
        outcome = _OUTCOME_PASSED
    elif exit_code in (124, 127):
        # 127: the command could not be executed. 124: it never finished.
        outcome = _OUTCOME_NOT_RUN
    elif exit_code == 5 and manifest.uses_default_pytest:
        outcome = _OUTCOME_NOT_RUN          # no tests collected
    else:
        outcome = _OUTCOME_FAILED

    return _result(
        change_id,
        consumer,
        passed=passed,
        outcome=outcome,
        exit_code=exit_code,
        output=output,
        source_ref=str(repo_path),
        source_revision=_head_revision(repo_path),
        language=manifest.language,
        test_command=manifest.command(),
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
    language: str = "python",
    test_command: list[str] | None = None,
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
                    "language": language,
                    "test_command": test_command,
                    "output_tail": output[-_OUTPUT_TAIL_CHARS:],
                },
                "source_ref": source_ref,
                "confidence": "confirmed",
                "source_revision": source_revision,
            }
        ],
    }
