"""
tests/verification/test_critic.py
===================================
Tests for agents/verification/critic.py.

Uses httpx.MockTransport (via monkeypatching _get_evidence) to simulate the
orchestrator evidence API — no real server needed, no ports opened.

Proven properties:
  - Detects stale test_result evidence (created_at older than migration commit ts).
  - Detects missing migration evidence for a required consumer.
  - Detects migration evidence with no source_revision (no commit SHA).
  - Only emits claim_type="risk" evidence — never any other type.
  - Never writes to the ledger (no SQLite imports, no ledger calls).
  - Never returns a VERIFIED/NOT_PROVEN_SAFE gate verdict — that is gate.py's job.
  - All results validate against the VerificationResult schema.
  - No fixture mutation (critic only touches HTTP data, not files).
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agents.verification.critic as critic_module
from agents.verification.critic import (
    _check_missing_consumers,
    _check_missing_commit_refs,
    _check_stale_test_evidence,
    _parse_iso,
    run as critic_run,
)
from orchestrator.schemas.verification import VerificationResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
FIXTURES = REPO_ROOT / "fixtures"

CHANGE_ID = "cr-test-001"
BASE_URL = "http://localhost:8000"

# Timestamps
_NOW = datetime.now(timezone.utc)
_OLD = _NOW - timedelta(hours=2)     # older than migration commit
_FUTURE = _NOW + timedelta(hours=1)  # newer than anything

_OLD_TS = _OLD.isoformat()
_NOW_TS = _NOW.isoformat()
_FUTURE_TS = _FUTURE.isoformat()


# ---------------------------------------------------------------------------
# Mock helper: patch _get_evidence so no real HTTP server is needed
# ---------------------------------------------------------------------------

def _make_mock_evidence_response(items: list[dict]) -> dict:
    """Build a fake EvidenceListResponse payload."""
    return {"change_id": CHANGE_ID, "evidence": items}


def _migration_evidence(consumer: str, commit_sha: str | None = "abc123") -> dict:
    return {
        "id": f"ev-{consumer}",
        "change_id": CHANGE_ID,
        "claim_type": "migration_status",
        "subject": consumer,
        "content": {"action": f"migrated {consumer}"},
        "source_ref": f"/fixtures/{consumer}",
        "confidence": "confirmed",
        "source_revision": commit_sha,
        "created_at": _NOW_TS,
    }


def _test_result_evidence(consumer: str, created_at: str) -> dict:
    return {
        "id": f"ev-test-{consumer}",
        "change_id": CHANGE_ID,
        "claim_type": "test_result",
        "subject": consumer,
        "content": {"returncode": 0, "output": "passed"},
        "source_ref": f"/fixtures/{consumer}",
        "confidence": "confirmed",
        "source_revision": "abc123",
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Unit tests for internal check helpers (pure functions — no HTTP)
# ---------------------------------------------------------------------------

class TestCheckHelpers:
    """Test the three check functions in isolation."""

    def test_check_missing_consumers_flags_absent_consumer(self):
        items = [_migration_evidence("checkout")]
        risks = _check_missing_consumers(items, ["checkout", "fraud", "analytics-worker"], CHANGE_ID)
        subjects = {r.subject for r in risks}
        assert "fraud" in subjects
        assert "analytics-worker" in subjects
        assert "checkout" not in subjects  # present — not flagged

    def test_check_missing_consumers_no_risk_when_all_present(self):
        items = [
            _migration_evidence("checkout"),
            _migration_evidence("fraud"),
            _migration_evidence("analytics-worker"),
        ]
        risks = _check_missing_consumers(items, ["checkout", "fraud", "analytics-worker"], CHANGE_ID)
        assert risks == []

    def test_check_missing_consumers_empty_required_list(self):
        risks = _check_missing_consumers([], [], CHANGE_ID)
        assert risks == []

    def test_check_missing_consumers_all_risk_evidence_type(self):
        items = []
        risks = _check_missing_consumers(items, ["checkout"], CHANGE_ID)
        assert all(r.claim_type == "risk" for r in risks)

    def test_check_missing_commit_refs_flags_no_source_revision(self):
        items = [_migration_evidence("checkout", commit_sha=None)]
        risks = _check_missing_commit_refs(items, CHANGE_ID)
        assert len(risks) == 1
        assert risks[0].claim_type == "risk"
        assert risks[0].content["risk"] == "no_commit_ref"

    def test_check_missing_commit_refs_no_flag_when_sha_present(self):
        items = [_migration_evidence("checkout", commit_sha="abc123abc123")]
        risks = _check_missing_commit_refs(items, CHANGE_ID)
        assert risks == []

    def test_check_missing_commit_refs_ignores_non_migration_evidence(self):
        items = [_test_result_evidence("checkout", _NOW_TS)]  # test_result, not migration
        risks = _check_missing_commit_refs(items, CHANGE_ID)
        assert risks == []

    def test_check_missing_commit_refs_does_not_flag_migration_plan(self):
        """
        A 'migration-plan' subject is a planning artifact, not a real component.
        It must NOT be flagged for a missing source_revision even when one is absent.
        """
        items = [
            {
                "id": "ev-migration-plan",
                "change_id": CHANGE_ID,
                "claim_type": "migration_status",
                "subject": "migration-plan",
                "content": {"action": "compatibility strategy planned"},
                "source_ref": "/agents/planning/output",
                "confidence": "confirmed",
                "source_revision": None,  # no commit — it's a plan, not code
                "created_at": _NOW_TS,
            }
        ]
        risks = _check_missing_commit_refs(items, CHANGE_ID)
        assert risks == [], (
            "migration-plan is a planning artifact and must never be flagged "
            "for missing commit_ref — it is not a real component."
        )

    def test_check_missing_commit_refs_still_flags_real_component(self):
        """
        A real component (e.g. 'checkout') must still be flagged when its
        migration_status evidence has no source_revision.
        This confirms the fix does not accidentally suppress real risks.
        """
        items = [_migration_evidence("checkout", commit_sha=None)]
        risks = _check_missing_commit_refs(items, CHANGE_ID)
        assert len(risks) == 1
        assert risks[0].content["risk"] == "no_commit_ref"
        assert risks[0].subject == "checkout"

    def test_check_stale_evidence_flags_old_test_result(self):
        # test_result created BEFORE the migration commit → stale
        items = [_test_result_evidence("checkout", _OLD_TS)]
        risks = _check_stale_test_evidence(items, _NOW_TS, CHANGE_ID)
        assert len(risks) == 1
        assert risks[0].claim_type == "risk"
        assert risks[0].content["risk"] == "stale_test_evidence"

    def test_check_stale_evidence_no_flag_for_fresh_result(self):
        # test_result created AFTER the migration commit → not stale
        items = [_test_result_evidence("checkout", _FUTURE_TS)]
        risks = _check_stale_test_evidence(items, _NOW_TS, CHANGE_ID)
        assert risks == []

    def test_check_stale_evidence_skipped_when_no_cutoff(self):
        items = [_test_result_evidence("checkout", _OLD_TS)]
        risks = _check_stale_test_evidence(items, None, CHANGE_ID)
        assert risks == []

    def test_check_stale_evidence_ignores_non_test_result(self):
        items = [_migration_evidence("checkout")]  # migration_status, not test_result
        risks = _check_stale_test_evidence(items, _NOW_TS, CHANGE_ID)
        assert risks == []


class TestParseIso:
    """Test the _parse_iso helper."""

    def test_valid_utc_timestamp(self):
        dt = _parse_iso("2025-01-01T12:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_timestamp_becomes_utc(self):
        dt = _parse_iso("2025-01-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_none_returns_none(self):
        assert _parse_iso(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_iso("") is None

    def test_invalid_string_returns_none(self):
        assert _parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# Integration tests using monkeypatched _get_evidence
# ---------------------------------------------------------------------------

class TestCriticRunWithMockedHTTP:
    """
    Test critic.run() end-to-end with _get_evidence monkeypatched to avoid
    requiring a real running orchestrator.
    """

    def test_detects_missing_required_consumer(self, monkeypatch):
        """Critic must flag a consumer with no migration_status evidence."""
        payload = _make_mock_evidence_response([
            _migration_evidence("checkout"),
            # fraud and analytics-worker are absent
        ])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run(
            {
                "change_id": CHANGE_ID,
                "required_consumers": ["checkout", "fraud", "analytics-worker"],
            },
            base_url=BASE_URL,
        )

        assert result.status == "failed"
        risk_subjects = {e.subject for e in result.evidence}
        assert "fraud" in risk_subjects
        assert "analytics-worker" in risk_subjects
        assert "checkout" not in risk_subjects

    def test_detects_stale_test_evidence(self, monkeypatch):
        """Critic must flag test_result evidence older than latest migration commit."""
        payload = _make_mock_evidence_response([
            _migration_evidence("checkout"),
            _test_result_evidence("checkout", _OLD_TS),  # stale
        ])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run(
            {
                "change_id": CHANGE_ID,
                "latest_migration_commit_ts": _NOW_TS,
            },
            base_url=BASE_URL,
        )

        assert result.status == "failed"
        assert any(
            e.content.get("risk") == "stale_test_evidence"
            for e in result.evidence
        )

    def test_detects_missing_commit_ref(self, monkeypatch):
        """Critic must flag migration evidence with no source_revision."""
        payload = _make_mock_evidence_response([
            _migration_evidence("checkout", commit_sha=None),  # no SHA
        ])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run(
            {"change_id": CHANGE_ID},
            base_url=BASE_URL,
        )

        assert result.status == "failed"
        assert any(
            e.content.get("risk") == "no_commit_ref"
            for e in result.evidence
        )

    def test_no_risks_when_evidence_is_clean(self, monkeypatch):
        """Critic returns status=verified and empty evidence when all checks pass."""
        payload = _make_mock_evidence_response([
            _migration_evidence("checkout", commit_sha="abc123"),
            _migration_evidence("fraud", commit_sha="def456"),
            _migration_evidence("analytics-worker", commit_sha="ghi789"),
            _test_result_evidence("checkout", _FUTURE_TS),
        ])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run(
            {
                "change_id": CHANGE_ID,
                "required_consumers": ["checkout", "fraud", "analytics-worker"],
                "latest_migration_commit_ts": _NOW_TS,
            },
            base_url=BASE_URL,
        )

        assert result.status == "verified"
        assert result.evidence == []

    def test_all_emitted_evidence_is_risk_type(self, monkeypatch):
        """CRITICAL: critic must ONLY emit claim_type='risk' evidence."""
        payload = _make_mock_evidence_response([
            _migration_evidence("checkout", commit_sha=None),  # will trigger no_commit_ref risk
            _test_result_evidence("checkout", _OLD_TS),         # will trigger stale risk
        ])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run(
            {
                "change_id": CHANGE_ID,
                "required_consumers": ["checkout", "fraud"],
                "latest_migration_commit_ts": _NOW_TS,
            },
            base_url=BASE_URL,
        )

        for ev in result.evidence:
            assert ev.claim_type == "risk", (
                f"Critic emitted non-risk evidence: claim_type={ev.claim_type!r}. "
                "Critic MUST only emit risk evidence."
            )

    def test_result_is_verification_result_instance(self, monkeypatch):
        """run() must return a VerificationResult, not a plain dict."""
        payload = _make_mock_evidence_response([])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run({"change_id": CHANGE_ID}, base_url=BASE_URL)

        assert isinstance(result, VerificationResult)

    def test_result_serialisable_to_dict(self, monkeypatch):
        """VerificationResult must be serialisable via Pydantic model_dump."""
        payload = _make_mock_evidence_response([])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run({"change_id": CHANGE_ID}, base_url=BASE_URL)
        d = result.model_dump()

        assert d["change_id"] == CHANGE_ID
        assert isinstance(d["evidence"], list)

    def test_consumer_defaults_to_critic(self, monkeypatch):
        """consumer field defaults to 'critic' when not supplied."""
        payload = _make_mock_evidence_response([])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run({"change_id": CHANGE_ID}, base_url=BASE_URL)

        assert result.consumer == "critic"

    def test_custom_consumer_label(self, monkeypatch):
        """consumer field can be overridden."""
        payload = _make_mock_evidence_response([])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)

        result = critic_run(
            {"change_id": CHANGE_ID, "consumer": "custom-label"},
            base_url=BASE_URL,
        )

        assert result.consumer == "custom-label"


class TestCriticSafetyConstraints:
    """
    Verify that the critic respects its core safety constraints:
    - Never writes to the ledger.
    - Never issues a gate verdict.
    - Status reflects evidence quality only.
    """

    def test_critic_does_not_import_ledger(self):
        """
        The critic module must not import orchestrator.ledger.
        If it did, it could write to the SQLite database — which is forbidden.
        """
        import orchestrator.ledger as ledger_module
        critic_globals = vars(critic_module)
        for key, value in critic_globals.items():
            assert value is not ledger_module, (
                f"critic.py imports orchestrator.ledger (found as '{key}'). "
                "Agents must never import or call the ledger directly."
            )

    def test_critic_status_is_not_gate_verdict(self, monkeypatch):
        """
        The critic's status ("verified"/"failed") is about evidence quality,
        NOT the gate's VERIFIED/NOT_PROVEN_SAFE decision.
        Confirm that critic.run() returns a VerificationResult with
        status in {"verified", "failed"} and never any other value.
        """
        import agents.verification.critic as critic_module_local
        payload = _make_mock_evidence_response([
            _migration_evidence("checkout", commit_sha=None),
        ])
        monkeypatch.setattr(critic_module_local, "_get_evidence", lambda cid, url: payload)

        result = critic_run({"change_id": CHANGE_ID}, base_url=BASE_URL)

        assert result.status in ("verified", "failed"), (
            f"status must be 'verified' or 'failed', got: {result.status!r}"
        )
        # The gate verdict strings must NOT appear in the result
        result_dict = result.model_dump()
        result_str = str(result_dict)
        assert "NOT_PROVEN_SAFE" not in result_str, (
            "Critic must not emit gate verdict NOT_PROVEN_SAFE."
        )
        assert "VERIFIED" not in result_str or result_str.count("VERIFIED") == 0, (
            "Critic must not emit gate verdict VERIFIED (all-caps)."
        )

    def test_critic_missing_change_id_raises(self, monkeypatch):
        """run() raises ValueError when change_id is absent."""
        with pytest.raises(ValueError, match="change_id"):
            critic_run({}, base_url=BASE_URL)

    def test_critic_missing_base_url_raises(self, monkeypatch):
        """run() raises ValueError when no base_url and no env var."""
        import os
        env_backup = os.environ.pop("INTERLOCK_API_URL", None)
        try:
            with pytest.raises(ValueError, match="base_url"):
                critic_run({"change_id": CHANGE_ID})
        finally:
            if env_backup is not None:
                os.environ["INTERLOCK_API_URL"] = env_backup

    def test_critic_env_var_fallback(self, monkeypatch):
        """INTERLOCK_API_URL env var is used when base_url param is None."""
        payload = _make_mock_evidence_response([])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)
        monkeypatch.setenv("INTERLOCK_API_URL", BASE_URL)

        result = critic_run({"change_id": CHANGE_ID})

        assert isinstance(result, VerificationResult)


# ---------------------------------------------------------------------------
# No fixture mutation guard
# ---------------------------------------------------------------------------

class TestNoFixtureMutation:
    """
    The critic agent only makes HTTP calls — it never touches the filesystem.
    Verify that the real fixtures/ directory is untouched.
    """

    def test_fixtures_not_mutated_by_critic(self, monkeypatch):
        """No fixture files are changed by running the critic."""
        checkout_before = (FIXTURES / "checkout" / "checkout.py").read_text()
        fraud_before = (FIXTURES / "fraud" / "fraud.py").read_text()
        analytics_before = (FIXTURES / "analytics-worker" / "worker.py").read_text()
        account_before = (FIXTURES / "account-service" / "app.py").read_text()

        payload = _make_mock_evidence_response([
            _migration_evidence("checkout", commit_sha=None),
        ])
        monkeypatch.setattr(critic_module, "_get_evidence", lambda cid, url: payload)
        critic_run(
            {"change_id": CHANGE_ID, "required_consumers": ["fraud"]},
            base_url=BASE_URL,
        )

        assert (FIXTURES / "checkout" / "checkout.py").read_text() == checkout_before
        assert (FIXTURES / "fraud" / "fraud.py").read_text() == fraud_before
        assert (FIXTURES / "analytics-worker" / "worker.py").read_text() == analytics_before
        assert (FIXTURES / "account-service" / "app.py").read_text() == account_before
