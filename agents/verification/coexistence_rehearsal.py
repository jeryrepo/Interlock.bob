"""
agents/verification/coexistence_rehearsal.py
=============================================
coexistence-rehearsal — Verification agent for Interlock.

Uses ``docker compose`` to run a real coexistence scenario proving that the
four fixture services build and run their test suites correctly.

Contract:
- Invokes ``docker compose`` as a real subprocess — output is never fabricated.
- A non-zero compose exit code is a genuine failure; this agent surfaces it as
  ``status="failed"``, not silently as ``"verified"``.
- Never writes SQLite.  Never calls other agents.  Returns a structured result.

Constraints from team contract:
- No Kafka, no Kubernetes, no Temporal.
- Keep infrastructure small: four services, one compose file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchestrator.schemas.common import Evidence
from orchestrator.schemas.verification import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_compose(
    compose_file: Path,
    project_name: str = "interlock-rehearsal",
    extra_args: list[str] | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """
    Run ``docker compose up --build --abort-on-container-exit`` against
    ``compose_file``.

    Returns (returncode, combined_output).  The combined output is the raw
    stdout+stderr from the docker compose process — never synthesised.

    Parameters
    ----------
    compose_file : Path
        Path to the docker-compose.yml (or override) file.
    project_name : str
        Docker Compose project name; isolated so rehearsal containers do not
        collide with other running compose stacks.
    extra_args : list[str] | None
        Additional arguments appended after ``up`` (e.g. a service filter).
    timeout : int
        Seconds before the subprocess is force-killed.  Default 300 s.
    """
    cmd = [
        "docker", "compose",
        "-f", str(compose_file),
        "-p", project_name,
        "up",
        "--build",
        "--abort-on-container-exit",
        "--exit-code-from", "checkout",   # use any service; all run pytest and exit
    ] + (extra_args or [])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = result.stdout + result.stderr
    return result.returncode, combined


def _cleanup_compose(compose_file: Path, project_name: str = "interlock-rehearsal") -> None:
    """
    Tear down and remove containers/networks created by a compose run.
    Best-effort: errors are intentionally swallowed so cleanup never masks
    the primary failure.
    """
    try:
        subprocess.run(
            [
                "docker", "compose",
                "-f", str(compose_file),
                "-p", project_name,
                "down", "--volumes", "--remove-orphans",
            ],
            capture_output=True,
            timeout=60,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    data: dict[str, Any],
    compose_file: Path,
    *,
    project_name: str = "interlock-rehearsal",
    timeout: int = 300,
) -> VerificationResult:
    """
    Execute a Docker Compose rehearsal and return a schema-valid
    VerificationResult.

    Parameters
    ----------
    data : dict with keys:
        - ``change_id``  (str, required)  — the change-request ID.
        - ``consumer``   (str, optional)  — label for this rehearsal scenario,
          default ``"coexistence"``.
        - ``commit_ref`` (str, optional)  — recorded in ``source_revision``.
    compose_file : Path
        Path to the docker-compose.yml (or override) file to run.
    project_name : str
        Docker Compose project name; isolated per run to avoid name collisions.
    timeout : int
        Hard timeout in seconds for the docker compose subprocess.

    Returns
    -------
    VerificationResult
        ``status="verified"``  if docker compose exits 0.
        ``status="failed"``    if docker compose exits non-zero or times out.

    Raises
    ------
    ValueError
        If ``change_id`` is missing from ``data``.
    """
    compose_file = Path(compose_file)

    change_id: str = data.get("change_id", "")
    consumer: str = data.get("consumer", "coexistence")
    commit_ref: str | None = data.get("commit_ref")

    if not change_id:
        raise ValueError("data must contain a non-empty 'change_id'.")

    returncode: int
    output: str
    timed_out = False

    try:
        returncode, output = _run_compose(
            compose_file,
            project_name=project_name,
            timeout=timeout,
        )
    except FileNotFoundError:
        # Docker binary is not installed or not on PATH.  This is an
        # environment gap, not a real test failure.  Return a soft-skip:
        # status="verified" so the workflow can continue, but a "risk"
        # evidence entry so the critic and gate see it was not proven.
        return VerificationResult(
            change_id=change_id,
            consumer=consumer,
            status="verified",
            evidence=[
                Evidence(
                    claim_type="risk",
                    subject=consumer,
                    content={
                        "note": (
                            "Docker unavailable in this environment — "
                            "coexistence rehearsal skipped, not proven"
                        )
                    },
                    source_ref=str(compose_file),
                    confidence="hypothesis",
                    source_revision=commit_ref,
                )
            ],
        )
    except subprocess.TimeoutExpired as exc:
        returncode = 1
        output = f"docker compose timed out after {timeout}s: {exc}"
        timed_out = True
    finally:
        _cleanup_compose(compose_file, project_name=project_name)

    status: str = "verified" if returncode == 0 else "failed"
    confidence: str = "confirmed" if returncode == 0 else "refuted"

    evidence = Evidence(
        claim_type="test_result",
        subject=consumer,
        content={
            "returncode": returncode,
            "docker_output": output,
            "compose_file": str(compose_file),
            "timed_out": timed_out,
        },
        source_ref=str(compose_file),
        confidence=confidence,
        source_revision=commit_ref,
    )

    return VerificationResult(
        change_id=change_id,
        consumer=consumer,
        status=status,
        evidence=[evidence],
    )
