"""
agents/verification/coexistence_rehearsal.py
=============================================
The coexistence-rehearsal agent: starts the provider for real and proves it
serves the legacy and new field shapes at the same time.

Why this exists
---------------
Contract tests prove each component passes in isolation. They cannot prove the
property the migration actually depends on: that a *single running provider*
serves the old contract and the new one simultaneously, so consumers can be cut
over one at a time instead of in a coordinated big-bang deploy. That is what the
coexistence window means, and the only honest way to demonstrate it is to run
the thing and ask it.

Why a subprocess and not Docker
-------------------------------
This agent originally drove ``docker compose``, because the Person 4 brief asked
for it. That was reconsidered: the property worth proving is *a separate process
answering over a real socket*, and a uvicorn subprocess delivers exactly that.
Docker added a daemon dependency — which meant the rehearsal could not run at
all in most environments, and a rehearsal that cannot run proves nothing.

The containerised consumer services were dropped outright. The consumer fixtures
are pure functions taking a dict (``process_order(account_response: dict, ...)``);
none of them makes an HTTP call. Running their pytest suites in containers
re-ran exactly what ``contract_test.py`` already covers, only slower. It proved
nothing about coexistence.

``docker-compose.yml`` is retained as an optional demo path and shares this
module's assertions via ``rehearsal/probe.py``.

What it must NOT do
-------------------
- **Never report a rehearsal that did not run as a pass.** A provider that never
  becomes healthy, a missing directory, or a missing dependency all mean "not
  proven" — ``status="failed"``, never a default success (AGENTS.md invariant 4).
- **Never write to the ledger** (invariant 2) or **call another agent**
  (invariant 3).
- **Never decide the gate** (invariant 1). It reports what the provider did.

Testability
-----------
``_start_provider`` and ``_get_json`` are module-level seams that tests
monkeypatch, mirroring how ``critic.py`` exposes ``_get_evidence``. The test that
starts a real server is marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agents.verification.rehearsal.probe import (
    PROBE_ACCOUNT_KEY,
    check_payload,
    describe,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDER_PATH = "fixtures/account-service"

# The ASGI application to serve. `service:app` deliberately, not `app:app`:
# app.py holds the payload logic the provider-patch agent rewrites and defines
# no ASGI application.
_ASGI_TARGET = "service:app"

_HEALTH_ATTEMPTS = 40
_HEALTH_DELAY_SECONDS = 0.25
_SHUTDOWN_GRACE_SECONDS = 5

_OUTPUT_TAIL_CHARS = 3000

_OUTCOME_PASSED = "coexistence_proven"
_OUTCOME_FAILED = "coexistence_failed"
_OUTCOME_NOT_RUN = "rehearsal_could_not_run"


# ---------------------------------------------------------------------------
# Seams — monkeypatched in unit tests
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Ask the OS for an unused port so parallel runs cannot collide."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_provider(provider_dir: Path, port: int) -> subprocess.Popen:
    """
    Launch the provider as a real uvicorn process bound to a local port.

    stdout and stderr are captured so a startup failure (a missing dependency,
    an import error) can be reported as evidence instead of vanishing.
    """
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", _ASGI_TARGET,
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(provider_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _await_health(base_url: str, process: subprocess.Popen) -> str | None:
    """
    Poll /health until the provider answers.

    Returns None on success, or a human-readable reason on failure. Gives up
    immediately if the process has already exited — waiting the full timeout for
    a server that died on import wastes time and buries the real error.
    """
    for _ in range(_HEALTH_ATTEMPTS):
        if process.poll() is not None:
            return f"provider process exited early with code {process.returncode}"
        try:
            _get_json(f"{base_url}/health", timeout=2.0)
            return None
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(_HEALTH_DELAY_SECONDS)
    return (
        f"provider never became healthy after "
        f"{_HEALTH_ATTEMPTS * _HEALTH_DELAY_SECONDS:.0f}s"
    )


def _stop_provider(process: subprocess.Popen) -> str:
    """Terminate the provider and return whatever it logged."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
    try:
        return process.stdout.read() or "" if process.stdout else ""
    except (ValueError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Agent entry point
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """
    Run the coexistence rehearsal and return a VerificationResult-shaped dict.

    Parameters
    ----------
    data : dict with keys:
        - change_id     : str (required)
        - provider_path : str, optional. Provider directory relative to
          *repo_path*. Defaults to ``fixtures/account-service``.
        - old_field     : str, optional (default ``customer_id``)
        - new_field     : str, optional (default ``account_id``)
        - expect_new    : bool, optional. True once the provider patch has
          landed, meaning both fields must be served.
    repo_path : Path
        Project root.

    Returns
    -------
    dict validating against ``orchestrator.schemas.VerificationResult``.
    ``status`` is ``"verified"`` only when the provider started, answered, and
    served exactly the expected field shape.
    """
    change_id = data["change_id"]
    old_field = data.get("old_field", "customer_id")
    new_field = data.get("new_field", "account_id")
    expect_new = bool(data.get("expect_new"))

    provider_dir = (
        Path(repo_path) / data.get("provider_path", _DEFAULT_PROVIDER_PATH)
    )

    if not provider_dir.is_dir():
        return _result(change_id, _OUTCOME_NOT_RUN, steps=[], detail=(
            f"provider directory not found: {provider_dir}"
        ))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    steps: list[dict[str, Any]] = []

    try:
        process = _start_provider(provider_dir, port)
    except OSError as exc:
        return _result(change_id, _OUTCOME_NOT_RUN, steps=[], detail=(
            f"could not launch the provider: {exc}"
        ))

    try:
        # 1. The provider must actually come up.
        reason = _await_health(base_url, process)
        steps.append(_step("provider-start", reason is None, reason or "healthy"))
        if reason is not None:
            log = _stop_provider(process)
            steps[-1]["output_tail"] = log[-_OUTPUT_TAIL_CHARS:]
            return _result(change_id, _OUTCOME_FAILED, steps,
                           detail=f"provider did not start: {reason}")

        # 2. Ask it what it serves.
        try:
            payload = _get_json(f"{base_url}/accounts/{PROBE_ACCOUNT_KEY}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            steps.append(_step("probe-request", False, f"request failed: {exc}"))
            return _result(change_id, _OUTCOME_FAILED, steps,
                           detail="provider did not answer the probe request")

        steps.append(_step("probe-request", True, f"response: {payload}"))

        # 3. The proof itself.
        failures = check_payload(payload, old_field, new_field, expect_new)
        steps.append(_step(
            "coexistence-assertions",
            not failures,
            "; ".join(failures) if failures else describe(old_field, new_field, expect_new),
        ))
        if failures:
            return _result(change_id, _OUTCOME_FAILED, steps,
                           detail="; ".join(failures))

        return _result(change_id, _OUTCOME_PASSED, steps,
                       detail=describe(old_field, new_field, expect_new))
    finally:
        # Always stop the provider. A leaked uvicorn holds its port and breaks
        # the next rehearsal, and a rehearsal that cannot rerun is worse than
        # one that failed.
        _stop_provider(process)


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------

def _step(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"step": name, "passed": passed, "detail": detail}


def _result(
    change_id: str,
    outcome: str,
    steps: list[dict[str, Any]],
    detail: str,
) -> dict[str, Any]:
    """Build the VerificationResult payload. Single construction point."""
    passed = outcome == _OUTCOME_PASSED
    return {
        "change_id": change_id,
        "consumer": "coexistence-rehearsal",
        "status": "verified" if passed else "failed",
        "evidence": [
            {
                "claim_type": "test_result",
                "subject": "coexistence-rehearsal",
                "content": {
                    "tests_passed": passed,
                    "outcome": outcome,
                    "detail": detail,
                    "steps": steps,
                },
                "source_ref": _DEFAULT_PROVIDER_PATH,
                "confidence": "confirmed",
                "source_revision": None,
            }
        ],
    }
