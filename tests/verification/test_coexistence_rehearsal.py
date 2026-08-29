"""
tests/verification/test_coexistence_rehearsal.py
=================================================
Tests for the coexistence-rehearsal agent.

The agent's whole value is that it refuses to claim a proof it did not obtain,
so most of these drive it down a failure path. ``_start_provider`` and
``_get_json`` are monkeypatched for the unit tests — the same seam pattern
``test_critic.py`` uses for ``_get_evidence`` — so they need no network and no
real server. The tests at the bottom start a genuine uvicorn process against the
real fixture; they are marked ``integration`` but need no Docker daemon, so they
run by default.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agents.verification import coexistence_rehearsal
from agents.verification.coexistence_rehearsal import run
from agents.verification.rehearsal.probe import check_payload
from orchestrator.schemas import VerificationResult

CHANGE_ID = "cr-001"
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class FakeProcess:
    """Stands in for the uvicorn Popen handle."""

    def __init__(self, exit_code: int | None = None, log: str = ""):
        self._exit_code = exit_code
        self.returncode = exit_code
        self.terminated = False
        self.killed = False
        self.stdout = _FakeStdout(log)

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self._exit_code = 0
        self.returncode = 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class _FakeStdout:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text


@pytest.fixture
def provider(tmp_path: Path) -> Path:
    """A project root containing a plausible provider directory."""
    (tmp_path / "fixtures" / "account-service").mkdir(parents=True)
    return tmp_path


def _install(monkeypatch, *, process=None, payload=None, fail_get=None):
    """Wire the seams. Returns the fake process so tests can inspect teardown."""
    proc = process if process is not None else FakeProcess()
    monkeypatch.setattr(coexistence_rehearsal, "_start_provider",
                        lambda provider_dir, port: proc)

    def fake_get(url, timeout=5.0):
        if fail_get:
            raise fail_get
        if url.endswith("/health"):
            return {"status": "ok"}
        return payload if payload is not None else {"customer_id": "probe-001"}

    monkeypatch.setattr(coexistence_rehearsal, "_get_json", fake_get)
    return proc


def _content(result: dict) -> dict:
    return result["evidence"][0]["content"]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

class TestSuccessfulRehearsal:
    def test_pre_migration_shape_is_verified(self, provider, monkeypatch):
        _install(monkeypatch, payload={"customer_id": "probe-001"})
        result = run({"change_id": CHANGE_ID}, provider)
        assert result["status"] == "verified"
        assert _content(result)["outcome"] == "coexistence_proven"

    def test_post_migration_both_fields_is_verified(self, provider, monkeypatch):
        _install(monkeypatch,
                 payload={"customer_id": "probe-001", "account_id": "probe-001"})
        result = run({"change_id": CHANGE_ID, "expect_new": True}, provider)
        assert result["status"] == "verified"

    def test_every_step_is_recorded(self, provider, monkeypatch):
        _install(monkeypatch)
        result = run({"change_id": CHANGE_ID}, provider)
        steps = {s["step"] for s in _content(result)["steps"]}
        assert steps == {"provider-start", "probe-request", "coexistence-assertions"}


# ---------------------------------------------------------------------------
# Failure detection — the reason this agent exists
# ---------------------------------------------------------------------------

class TestFailureDetection:
    def test_missing_new_field_after_migration_fails(self, provider, monkeypatch):
        """The headline case: the provider has not been patched but we expect it to be."""
        _install(monkeypatch, payload={"customer_id": "probe-001"})
        result = run({"change_id": CHANGE_ID, "expect_new": True}, provider)
        assert result["status"] == "failed"
        assert "account_id" in _content(result)["detail"]

    def test_missing_legacy_field_fails(self, provider, monkeypatch):
        """Dropping the old field breaks every un-migrated consumer."""
        _install(monkeypatch, payload={"account_id": "probe-001"})
        result = run({"change_id": CHANGE_ID, "expect_new": True}, provider)
        assert result["status"] == "failed"
        assert "customer_id" in _content(result)["detail"]

    def test_new_field_present_too_early_fails(self, provider, monkeypatch):
        """
        If the new field is already served when we expect pre-migration state,
        the rehearsal is not observing what it claims to observe.
        """
        _install(monkeypatch,
                 payload={"customer_id": "probe-001", "account_id": "probe-001"})
        result = run({"change_id": CHANGE_ID, "expect_new": False}, provider)
        assert result["status"] == "failed"
        assert "pre-migration state" in _content(result)["detail"]

    def test_provider_that_never_starts_fails(self, provider, monkeypatch):
        _install(monkeypatch, process=FakeProcess(exit_code=3, log="ImportError: no app"))
        result = run({"change_id": CHANGE_ID}, provider)
        assert result["status"] == "failed"
        assert "did not start" in _content(result)["detail"]

    def test_provider_startup_log_is_captured(self, provider, monkeypatch):
        """A startup failure must not vanish — the log is the only diagnosis."""
        _install(monkeypatch,
                 process=FakeProcess(exit_code=3, log="ModuleNotFoundError: fastapi"))
        result = run({"change_id": CHANGE_ID}, provider)
        start_step = _content(result)["steps"][0]
        assert "fastapi" in start_step["output_tail"]

    def test_unanswerable_probe_request_fails(self, provider, monkeypatch):
        proc = FakeProcess()
        monkeypatch.setattr(coexistence_rehearsal, "_start_provider",
                            lambda provider_dir, port: proc)

        def flaky(url, timeout=5.0):
            if url.endswith("/health"):
                return {"status": "ok"}
            raise OSError("connection reset")

        monkeypatch.setattr(coexistence_rehearsal, "_get_json", flaky)
        result = run({"change_id": CHANGE_ID}, provider)
        assert result["status"] == "failed"
        assert "did not answer" in _content(result)["detail"]


class TestUnrunnableRehearsal:
    """A rehearsal that did not run must never look like one that passed."""

    def test_missing_provider_directory_is_failed(self, tmp_path):
        result = run({"change_id": CHANGE_ID}, tmp_path)
        assert result["status"] == "failed"
        assert _content(result)["outcome"] == "rehearsal_could_not_run"

    def test_missing_provider_directory_starts_nothing(self, tmp_path, monkeypatch):
        started: list[int] = []
        monkeypatch.setattr(coexistence_rehearsal, "_start_provider",
                            lambda d, p: started.append(p))
        run({"change_id": CHANGE_ID}, tmp_path)
        assert started == []

    def test_unlaunchable_provider_is_failed(self, provider, monkeypatch):
        def boom(provider_dir, port):
            raise OSError("no python")

        monkeypatch.setattr(coexistence_rehearsal, "_start_provider", boom)
        result = run({"change_id": CHANGE_ID}, provider)
        assert result["status"] == "failed"
        assert _content(result)["outcome"] == "rehearsal_could_not_run"


# ---------------------------------------------------------------------------
# Cleanup — a leaked uvicorn holds its port and breaks the next run
# ---------------------------------------------------------------------------

class TestTeardown:
    def test_provider_is_stopped_on_success(self, provider, monkeypatch):
        proc = _install(monkeypatch)
        run({"change_id": CHANGE_ID}, provider)
        assert proc.terminated

    def test_provider_is_stopped_after_assertion_failure(self, provider, monkeypatch):
        proc = _install(monkeypatch, payload={"account_id": "x"})
        run({"change_id": CHANGE_ID, "expect_new": True}, provider)
        assert proc.terminated

    def test_provider_is_stopped_if_a_step_raises(self, provider, monkeypatch):
        proc = FakeProcess()
        monkeypatch.setattr(coexistence_rehearsal, "_start_provider",
                            lambda d, p: proc)

        def exploding(url, timeout=5.0):
            if url.endswith("/health"):
                return {"status": "ok"}
            raise RuntimeError("unexpected")

        monkeypatch.setattr(coexistence_rehearsal, "_get_json", exploding)
        with pytest.raises(RuntimeError):
            run({"change_id": CHANGE_ID}, provider)
        assert proc.terminated


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def test_field_names_are_not_hardcoded(self, provider, monkeypatch):
        """Invariant 6: the change's fields arrive from data, not constants."""
        _install(monkeypatch, payload={"user_ref": "probe-001"})
        result = run(
            {"change_id": CHANGE_ID, "old_field": "user_ref", "new_field": "party_ref"},
            provider,
        )
        assert result["status"] == "verified"

    def test_provider_path_is_configurable(self, tmp_path, monkeypatch):
        (tmp_path / "services" / "billing").mkdir(parents=True)
        _install(monkeypatch)
        result = run(
            {"change_id": CHANGE_ID, "provider_path": "services/billing"}, tmp_path
        )
        assert result["status"] == "verified"

    def test_each_run_binds_a_fresh_port(self):
        """Parallel rehearsals must not collide on a fixed port."""
        assert coexistence_rehearsal._free_port() != 0


# ---------------------------------------------------------------------------
# The shared assertion function
# ---------------------------------------------------------------------------

class TestCheckPayload:
    """One implementation of 'what coexistence means', used by both run modes."""

    def test_pre_migration_ok(self):
        assert check_payload({"customer_id": "x"}, "customer_id", "account_id", False) == []

    def test_post_migration_ok(self):
        payload = {"customer_id": "x", "account_id": "x"}
        assert check_payload(payload, "customer_id", "account_id", True) == []

    def test_dropping_legacy_field_is_a_failure(self):
        failures = check_payload({"account_id": "x"}, "customer_id", "account_id", True)
        assert len(failures) == 1
        assert "customer_id" in failures[0]

    def test_both_conditions_can_fail_together(self):
        failures = check_payload({}, "customer_id", "account_id", True)
        assert len(failures) == 2


# ---------------------------------------------------------------------------
# Contract compliance
# ---------------------------------------------------------------------------

class TestSchemaCompliance:
    def test_success_validates_against_shared_schema(self, provider, monkeypatch):
        _install(monkeypatch)
        assert VerificationResult(**run({"change_id": CHANGE_ID}, provider)).status == "verified"

    def test_failure_validates_against_shared_schema(self, provider, monkeypatch):
        _install(monkeypatch, payload={"account_id": "x"})
        model = VerificationResult(**run({"change_id": CHANGE_ID, "expect_new": True}, provider))
        assert model.status == "failed"

    def test_only_test_result_evidence_is_emitted(self, provider, monkeypatch):
        _install(monkeypatch)
        result = run({"change_id": CHANGE_ID}, provider)
        assert {e["claim_type"] for e in result["evidence"]} == {"test_result"}


class TestSafetyConstraints:
    """AGENTS.md invariants 1, 2 and 3, checked via imports rather than grep."""

    @staticmethod
    def _imported_names() -> set[str]:
        tree = ast.parse(
            Path(coexistence_rehearsal.__file__).read_text(encoding="utf-8")
        )
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def test_does_not_import_sqlite(self):
        assert not any(n.startswith("sqlite3") for n in self._imported_names())

    def test_does_not_import_the_ledger(self):
        assert not any("ledger" in n for n in self._imported_names())

    def test_does_not_import_the_gate(self):
        assert not any("gate" in n for n in self._imported_names())

    def test_only_agent_import_is_its_own_probe(self):
        """
        Invariant 3 forbids agent-to-agent calls. Importing a pure assertion
        helper from this agent's own package is not that.
        """
        agent_imports = {n for n in self._imported_names() if n.startswith("agents")}
        assert agent_imports == {"agents.verification.rehearsal.probe"}

    def test_never_emits_a_gate_verdict(self, provider, monkeypatch):
        _install(monkeypatch)
        rendered = str(run({"change_id": CHANGE_ID}, provider))
        assert "NOT_PROVEN_SAFE" not in rendered
        assert "VERIFIED" not in rendered


# ---------------------------------------------------------------------------
# The real thing — a genuine uvicorn process. No Docker required.
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestAgainstRealProvider:
    def test_pre_migration_coexistence_holds(self):
        """Start the real fixture provider and prove it serves the legacy shape."""
        result = run({"change_id": CHANGE_ID, "expect_new": False}, REPO_ROOT)
        assert result["status"] == "verified", _content(result)["detail"]
        assert _content(result)["outcome"] == "coexistence_proven"

    def test_unpatched_provider_fails_post_migration_expectations(self):
        """
        The negative case against a real server: the shipped fixture has not
        been patched, so requiring the new field must fail.
        """
        result = run({"change_id": CHANGE_ID, "expect_new": True}, REPO_ROOT)
        assert result["status"] == "failed"
        assert "account_id" in _content(result)["detail"]

    def test_a_directory_without_an_asgi_app_fails(self):
        """A consumer fixture has no service:app, so the provider cannot start."""
        result = run(
            {"change_id": CHANGE_ID, "provider_path": "fixtures/checkout"}, REPO_ROOT
        )
        assert result["status"] == "failed"
        assert "did not start" in _content(result)["detail"]
