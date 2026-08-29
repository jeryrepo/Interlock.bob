"""
tests/verification/test_contract_test.py
=========================================
Tests for agents/verification/contract_test.py.

Every test that touches a fixture copy:
  1. Copies the real fixture to a tmp_path worktree (never touches fixtures/).
  2. Optionally runs the relevant implementation agent against the copy.
  3. Runs contract_test.run() against the copy.
  4. Asserts the real fixtures/ file was NOT mutated.

Proven properties:
  - Real pytest subprocess is invoked (output contains actual pytest markers).
  - A deliberately broken test causes status="failed" (negative path).
  - A passing test suite causes status="verified".
  - VerificationResult validates against the schema.
  - evidence[0].claim_type == "test_result".
  - evidence[0].content["output"] is real subprocess output (never fabricated).
  - No fixture mutation from any test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agents.verification.contract_test import run as ct_run
from orchestrator.schemas.verification import VerificationResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
FIXTURES = REPO_ROOT / "fixtures"

CHECKOUT = FIXTURES / "checkout"
FRAUD = FIXTURES / "fraud"
ANALYTICS = FIXTURES / "analytics-worker"
ACCOUNT_SERVICE = FIXTURES / "account-service"


# ---------------------------------------------------------------------------
# Worktree helper (same pattern as tests/implementation/test_fixture_integration.py)
# ---------------------------------------------------------------------------

def _make_worktree(src: Path, dest: Path) -> None:
    """
    Copy src into dest, git-init it with local identity, and initial-commit.
    This is the canonical non-mutating pattern — real fixtures/ is never touched.
    """
    shutil.copytree(str(src), str(dest), ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init"], capture_output=True, cwd=str(dest))
    subprocess.run(
        ["git", "config", "user.email", "test@interlock.dev"],
        capture_output=True, cwd=str(dest),
    )
    subprocess.run(
        ["git", "config", "user.name", "Interlock Test"],
        capture_output=True, cwd=str(dest),
    )
    subprocess.run(["git", "add", "."], capture_output=True, cwd=str(dest))
    subprocess.run(
        ["git", "commit", "-m", "baseline-copy"],
        capture_output=True, cwd=str(dest),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestContractTestRunsPytest:
    """
    Prove that contract_test.run() invokes real pytest as a subprocess and
    returns genuine output — not fabricated results.
    """

    def test_runs_real_pytest_against_checkout(self, tmp_path: Path):
        """
        Real pytest is invoked: output contains actual pytest markers.
        The pre-migration checkout test suite passes with customer_id.
        """
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)

        result = ct_run(
            {"change_id": "cr-001", "consumer": "checkout"},
            dest,
        )

        assert result.status == "verified"
        output = result.evidence[0].content["output"]
        # Real pytest always emits these markers in verbose mode
        assert "passed" in output or "PASSED" in output, (
            "output must contain real pytest pass markers; got:\n" + output[:500]
        )

    def test_runs_real_pytest_against_account_service(self, tmp_path: Path):
        """The account-service pre-migration test suite also passes."""
        dest = tmp_path / "account-service"
        _make_worktree(ACCOUNT_SERVICE, dest)

        result = ct_run(
            {"change_id": "cr-001", "consumer": "account-service"},
            dest,
        )

        assert result.status == "verified"
        output = result.evidence[0].content["output"]
        assert "passed" in output or "PASSED" in output, (
            "account-service pytest output did not contain pass markers:\n" + output[:500]
        )

    def test_output_contains_subprocess_returncode(self, tmp_path: Path):
        """evidence content must include the real returncode, not a fabricated value."""
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)

        result = ct_run(
            {"change_id": "cr-002", "consumer": "fraud"},
            dest,
        )

        assert "returncode" in result.evidence[0].content
        assert result.evidence[0].content["returncode"] == 0

    def test_commit_ref_stored_in_source_revision(self, tmp_path: Path):
        """A provided commit_ref is stored in source_revision."""
        dest = tmp_path / "analytics"
        _make_worktree(ANALYTICS, dest)

        result = ct_run(
            {
                "change_id": "cr-003",
                "consumer": "analytics-worker",
                "commit_ref": "abc123def456abc123def456abc123def456abc1",
            },
            dest,
        )

        assert result.evidence[0].source_revision == "abc123def456abc123def456abc123def456abc1"


class TestContractTestDetectsFailure:
    """
    NEGATIVE PATH: Prove that a genuinely broken test causes status="failed".
    The agent must NOT silently report success for a failing suite.
    """

    def test_broken_test_causes_failed_status(self, tmp_path: Path):
        """
        Inject assert False into a tmp_path copy's test file.
        contract_test.run() must return status="failed", never "verified".
        """
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)

        # Inject a failing assertion into the existing test file
        test_file = dest / "tests" / "test_checkout.py"
        original = test_file.read_text()
        broken = original + "\n\ndef test_injected_failure():\n    assert False, 'deliberate failure'\n"
        test_file.write_text(broken)

        result = ct_run(
            {"change_id": "cr-001", "consumer": "checkout"},
            dest,
        )

        assert result.status == "failed", (
            "A broken test suite must produce status='failed', not 'verified'. "
            f"Actual status: {result.status!r}. Output:\n"
            + result.evidence[0].content.get("output", "")[:800]
        )

    def test_broken_test_output_contains_failure_markers(self, tmp_path: Path):
        """The output from a failing run must contain real pytest failure markers."""
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)

        test_file = dest / "tests" / "test_fraud.py"
        original = test_file.read_text()
        test_file.write_text(original + "\n\ndef test_must_fail():\n    assert False\n")

        result = ct_run(
            {"change_id": "cr-002", "consumer": "fraud"},
            dest,
        )

        output = result.evidence[0].content["output"]
        assert result.status == "failed"
        # Real pytest output always includes these on failure
        assert "failed" in output or "FAILED" in output or "error" in output.lower(), (
            "pytest failure output must contain failure markers; got:\n" + output[:500]
        )

    def test_nonexistent_tests_dir_causes_failed_status(self, tmp_path: Path):
        """
        A repo with no tests/ directory should report failed (nothing collected).
        """
        dest = tmp_path / "empty-service"
        dest.mkdir()
        # Minimal Python file — no tests
        (dest / "app.py").write_text("x = 1\n")

        result = ct_run(
            {"change_id": "cr-004", "consumer": "empty-service"},
            dest,
        )

        # pytest exits non-zero (exit code 4 = no tests collected)
        assert result.status == "failed"


class TestContractTestSchemaValidation:
    """Prove that all results validate against the VerificationResult schema."""

    def test_result_is_verification_result_instance(self, tmp_path: Path):
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)

        result = ct_run(
            {"change_id": "cr-001", "consumer": "checkout"},
            dest,
        )

        assert isinstance(result, VerificationResult)

    def test_evidence_claim_type_is_test_result(self, tmp_path: Path):
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)

        result = ct_run(
            {"change_id": "cr-001", "consumer": "fraud"},
            dest,
        )

        assert len(result.evidence) == 1
        assert result.evidence[0].claim_type == "test_result"

    def test_result_fields_match_schema(self, tmp_path: Path):
        dest = tmp_path / "analytics"
        _make_worktree(ANALYTICS, dest)

        result = ct_run(
            {"change_id": "cr-test", "consumer": "analytics-worker"},
            dest,
        )

        assert result.change_id == "cr-test"
        assert result.consumer == "analytics-worker"
        assert result.status in ("verified", "failed")
        assert isinstance(result.evidence, list)

    def test_schema_serialisable_to_dict(self, tmp_path: Path):
        """VerificationResult must be serialisable (Pydantic model_dump)."""
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)

        result = ct_run(
            {"change_id": "cr-001", "consumer": "checkout"},
            dest,
        )

        d = result.model_dump()
        assert d["change_id"] == "cr-001"
        assert d["evidence"][0]["claim_type"] == "test_result"


class TestNoFixtureMutation:
    """
    Every test that copies a fixture must leave the real fixtures/ tree unchanged.
    Explicitly asserted — same safety guarantee as test_fixture_integration.py.
    """

    def test_checkout_fixture_not_mutated(self, tmp_path: Path):
        original = (CHECKOUT / "checkout.py").read_text()
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)
        ct_run({"change_id": "cr-001", "consumer": "checkout"}, dest)
        after = (CHECKOUT / "checkout.py").read_text()
        assert original == after, (
            "Real fixtures/checkout/checkout.py was mutated! "
            "contract_test must operate only on the tmp_path copy."
        )

    def test_fraud_fixture_not_mutated(self, tmp_path: Path):
        original = (FRAUD / "fraud.py").read_text()
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)
        ct_run({"change_id": "cr-002", "consumer": "fraud"}, dest)
        after = (FRAUD / "fraud.py").read_text()
        assert original == after, (
            "Real fixtures/fraud/fraud.py was mutated! "
            "contract_test must operate only on the tmp_path copy."
        )

    def test_analytics_fixture_not_mutated(self, tmp_path: Path):
        original = (ANALYTICS / "worker.py").read_text()
        dest = tmp_path / "analytics-worker"
        _make_worktree(ANALYTICS, dest)
        ct_run({"change_id": "cr-003", "consumer": "analytics-worker"}, dest)
        after = (ANALYTICS / "worker.py").read_text()
        assert original == after, (
            "Real fixtures/analytics-worker/worker.py was mutated! "
            "contract_test must operate only on the tmp_path copy."
        )

    def test_account_service_fixture_not_mutated(self, tmp_path: Path):
        original = (ACCOUNT_SERVICE / "app.py").read_text()
        dest = tmp_path / "account-service"
        _make_worktree(ACCOUNT_SERVICE, dest)
        ct_run({"change_id": "cr-004", "consumer": "account-service"}, dest)
        after = (ACCOUNT_SERVICE / "app.py").read_text()
        assert original == after, (
            "Real fixtures/account-service/app.py was mutated! "
            "contract_test must operate only on the tmp_path copy."
        )
