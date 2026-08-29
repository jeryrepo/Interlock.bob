"""
tests/verification/test_coexistence_rehearsal.py
=================================================
Tests for agents/verification/coexistence_rehearsal.py.

Tests are divided into two groups:

  NON-DOCKER: Test the internal module structure, schema of return types, and
  input validation — without invoking docker compose.  These pass even when
  Docker Desktop is not running.

  DOCKER (@pytest.mark.docker): Tests that actually call docker compose as a
  subprocess.  Automatically skipped when Docker is unavailable (see conftest.py).
  Run them explicitly with:

    pytest -m docker tests/verification/test_coexistence_rehearsal.py

Proven properties:
  - run() returns a schema-valid VerificationResult (all VerificationResult
    fields and Evidence fields are present and correctly typed).
  - run() raises ValueError for missing change_id (no Docker needed).
  - Docker compose is actually invoked as a subprocess (real output, not mocked).
  - A broken service (bad CMD override) causes status="failed", not "verified".
  - evidence[0].content["docker_output"] contains real docker output.
  - evidence[0].claim_type == "test_result".
  - No fixture mutation from any test.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.verification import coexistence_rehearsal as rehearsal_module
from agents.verification.coexistence_rehearsal import run as rehearsal_run
from orchestrator.schemas.verification import VerificationResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
FIXTURES = REPO_ROOT / "fixtures"


# ---------------------------------------------------------------------------
# Non-Docker tests: input validation and schema structure
# (These never call docker compose)
# ---------------------------------------------------------------------------

class TestCoexistenceRehearsalInputValidation:
    """Input validation — no Docker required."""

    def test_missing_change_id_raises_value_error(self, tmp_path: Path):
        """run() must raise ValueError if change_id is absent."""
        fake_compose = tmp_path / "any.yml"
        fake_compose.write_text("services: {}\n")

        with pytest.raises(ValueError, match="change_id"):
            rehearsal_run({}, fake_compose)

    def test_change_id_required_not_empty(self, tmp_path: Path):
        """An empty change_id must also raise ValueError."""
        fake_compose = tmp_path / "any.yml"
        fake_compose.write_text("services: {}\n")

        with pytest.raises(ValueError, match="change_id"):
            rehearsal_run({"change_id": ""}, fake_compose)


# Module-level mock functions (plain callables, not bound methods — safe to use
# with patch.object which replaces the module attribute).
def _mock_run_compose_success(compose_file, project_name="interlock-rehearsal", extra_args=None, timeout=300):
    return 0, "Successfully ran all tests\npassed\n"


def _mock_run_compose_failure(compose_file, project_name="interlock-rehearsal", extra_args=None, timeout=300):
    return 1, "FAILED\ncontainer exited with code 1\n"


class TestCoexistenceRehearsalSchemaMocked:
    """
    Schema and return-type validation using a mocked _run_compose.
    No Docker process is spawned — the mock returns a fixed (returncode, output).
    """

    def test_successful_run_returns_verified(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            result = rehearsal_run(
                {"change_id": "cr-mock-ok", "consumer": "coexistence"},
                fake_compose,
                project_name="test-ok",
            )

        assert isinstance(result, VerificationResult)
        assert result.status == "verified"
        assert result.change_id == "cr-mock-ok"
        assert result.consumer == "coexistence"

    def test_failed_run_returns_failed(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_failure):
            result = rehearsal_run(
                {"change_id": "cr-mock-fail", "consumer": "coexistence"},
                fake_compose,
                project_name="test-fail",
            )

        assert result.status == "failed"

    def test_result_has_docker_output_in_content(self, tmp_path: Path):
        """evidence content must have docker_output key."""
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            result = rehearsal_run(
                {"change_id": "cr-mock-output", "consumer": "coexistence"},
                fake_compose,
                project_name="test-output",
            )

        assert "docker_output" in result.evidence[0].content
        assert "passed" in result.evidence[0].content["docker_output"]

    def test_evidence_claim_type_is_test_result(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            result = rehearsal_run(
                {"change_id": "cr-claim-type", "consumer": "coexistence"},
                fake_compose,
                project_name="test-claim",
            )

        assert len(result.evidence) == 1
        assert result.evidence[0].claim_type == "test_result"

    def test_result_serialisable_to_dict(self, tmp_path: Path):
        """VerificationResult must be serialisable via Pydantic model_dump."""
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            result = rehearsal_run(
                {"change_id": "cr-serial", "consumer": "coexistence"},
                fake_compose,
                project_name="test-serial",
            )

        d = result.model_dump()
        assert d["change_id"] == "cr-serial"
        assert d["evidence"][0]["claim_type"] == "test_result"

    def test_commit_ref_stored_in_source_revision(self, tmp_path: Path):
        """A provided commit_ref must appear in evidence source_revision."""
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            result = rehearsal_run(
                {
                    "change_id": "cr-rev-test",
                    "consumer": "coexistence",
                    "commit_ref": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                },
                fake_compose,
                project_name="test-rev",
            )

        assert result.evidence[0].source_revision == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    def test_consumer_defaults_to_coexistence(self, tmp_path: Path):
        """consumer defaults to 'coexistence' when not supplied."""
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            result = rehearsal_run(
                {"change_id": "cr-default-consumer"},
                fake_compose,
                project_name="test-consumer",
            )

        assert result.consumer == "coexistence"


class TestDockerUnavailable:
    """
    Fail-closed path: FileNotFoundError when Docker binary is missing.
    No Docker process is spawned — _run_compose raises FileNotFoundError.
    """

    def _mock_run_compose_no_docker(self, *args, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    def test_returns_failed_status(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", self._mock_run_compose_no_docker):
            result = rehearsal_run(
                {"change_id": "cr-no-docker", "consumer": "coexistence"},
                fake_compose,
            )

        assert result.status == "failed"

    def test_evidence_claim_type_is_test_result(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", self._mock_run_compose_no_docker):
            result = rehearsal_run(
                {"change_id": "cr-no-docker-risk", "consumer": "coexistence"},
                fake_compose,
            )

        assert result.evidence[0].claim_type == "test_result"
        assert result.evidence[0].content["returncode"] == 127

    def test_evidence_note_mentions_docker_unavailable(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", self._mock_run_compose_no_docker):
            result = rehearsal_run(
                {"change_id": "cr-no-docker-note", "consumer": "coexistence"},
                fake_compose,
            )

        note = result.evidence[0].content.get("note", "")
        assert "Docker unavailable" in note

    def test_evidence_confidence_is_refuted(self, tmp_path: Path):
        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", self._mock_run_compose_no_docker):
            result = rehearsal_run(
                {"change_id": "cr-no-docker-conf", "consumer": "coexistence"},
                fake_compose,
            )

        assert result.evidence[0].confidence == "refuted"


class TestNoFixtureMutationMocked:
    """
    No fixture files are changed by running the rehearsal agent.
    Uses mocked _run_compose so no Docker process is spawned.
    """

    def test_fixtures_not_mutated_by_rehearsal(self, tmp_path: Path):
        checkout_before = (FIXTURES / "checkout" / "checkout.py").read_text()
        fraud_before = (FIXTURES / "fraud" / "fraud.py").read_text()
        analytics_before = (FIXTURES / "analytics-worker" / "worker.py").read_text()
        account_before = (FIXTURES / "account-service" / "app.py").read_text()

        fake_compose = tmp_path / "compose.yml"
        fake_compose.write_text("services: {}\n")

        with patch.object(rehearsal_module, "_run_compose", _mock_run_compose_success):
            rehearsal_run(
                {"change_id": "cr-guard", "consumer": "coexistence"},
                fake_compose,
                project_name="test-guard",
            )

        assert (FIXTURES / "checkout" / "checkout.py").read_text() == checkout_before, (
            "fixtures/checkout/checkout.py was mutated by rehearsal."
        )
        assert (FIXTURES / "fraud" / "fraud.py").read_text() == fraud_before, (
            "fixtures/fraud/fraud.py was mutated by rehearsal."
        )
        assert (FIXTURES / "analytics-worker" / "worker.py").read_text() == analytics_before, (
            "fixtures/analytics-worker/worker.py was mutated by rehearsal."
        )
        assert (FIXTURES / "account-service" / "app.py").read_text() == account_before, (
            "fixtures/account-service/app.py was mutated by rehearsal."
        )


# ---------------------------------------------------------------------------
# Docker-dependent tests — require Docker Desktop to be running
# ---------------------------------------------------------------------------

@pytest.mark.docker
class TestCoexistenceRehearsalDocker:
    """
    Real Docker Compose tests.  These tests actually call docker compose,
    build images from the fixture Dockerfiles, and run the test suites.

    Skip condition: Docker is not available (see conftest.py).

    Run with:
        pytest -m docker tests/verification/test_coexistence_rehearsal.py
    """

    def test_rehearsal_succeeds_with_real_compose_file(self):
        """
        Run docker compose against the real docker-compose.yml.
        All four fixture services run their pytest suites and must exit 0.
        """
        result = rehearsal_run(
            {"change_id": "cr-docker-001", "consumer": "coexistence"},
            COMPOSE_FILE,
            project_name="interlock-rehearsal-main",
            timeout=300,
        )

        assert result.status == "verified", (
            "docker compose rehearsal failed. Output:\n"
            + result.evidence[0].content.get("docker_output", "")[-2000:]
        )

    def test_rehearsal_output_contains_real_docker_output(self):
        """evidence[0].content['docker_output'] must be a non-empty real string."""
        result = rehearsal_run(
            {"change_id": "cr-docker-002", "consumer": "coexistence"},
            COMPOSE_FILE,
            project_name="interlock-rehearsal-output",
            timeout=300,
        )

        docker_output = result.evidence[0].content.get("docker_output", "")
        assert isinstance(docker_output, str)
        assert len(docker_output) > 0, "docker_output must be non-empty real output"

    def test_rehearsal_repeated_run_is_idempotent(self):
        """
        Running the rehearsal twice must produce the same status.
        Proves Docker Compose is reliable and not order-dependent.
        """
        result_1 = rehearsal_run(
            {"change_id": "cr-docker-003a", "consumer": "coexistence"},
            COMPOSE_FILE,
            project_name="interlock-rehearsal-repeat-1",
            timeout=300,
        )
        result_2 = rehearsal_run(
            {"change_id": "cr-docker-003b", "consumer": "coexistence"},
            COMPOSE_FILE,
            project_name="interlock-rehearsal-repeat-2",
            timeout=300,
        )

        assert result_1.status == result_2.status, (
            "Repeated rehearsal runs returned different statuses — "
            f"run 1: {result_1.status}, run 2: {result_2.status}"
        )

    def test_broken_service_causes_failed_status(self, tmp_path: Path):
        """
        NEGATIVE PATH: A compose file with a service that runs a deliberately
        broken command must cause status="failed", not silently "verified".
        """
        broken_compose = tmp_path / "docker-compose-broken.yml"
        broken_compose.write_text(textwrap.dedent("""\
            services:
              checkout:
                build:
                  context: ./fixtures/checkout
                  dockerfile: Dockerfile
                command: ["python", "-c", "import sys; sys.exit(1)"]
        """))

        result = rehearsal_run(
            {"change_id": "cr-docker-broken", "consumer": "coexistence"},
            broken_compose,
            project_name="interlock-rehearsal-broken",
            timeout=120,
        )

        assert result.status == "failed", (
            "A broken service must cause status='failed'. "
            f"Actual: {result.status!r}. Output:\n"
            + result.evidence[0].content.get("docker_output", "")[-1000:]
        )
