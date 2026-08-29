"""
tests/verification/test_contract_test.py
=========================================
Tests for the contract-test agent.

The point of this agent is that it cannot lie about test outcomes, so most of
these tests are about what it does when things go *wrong*. A contract-test agent
that only proves the happy path is worthless — the gate depends on failures
being reported as failures.
"""

from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

from agents.verification import contract_test
from agents.verification.contract_test import run
from orchestrator.schemas import VerificationResult


# ---------------------------------------------------------------------------
# Fixtures — throwaway component repos, never the real fixtures/ tree
# ---------------------------------------------------------------------------

def _write_component(root: Path, test_body: str) -> Path:
    """Create a minimal component directory containing one test module."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_component.py").write_text(
        textwrap.dedent(test_body), encoding="utf-8"
    )
    return root


@pytest.fixture
def passing_repo(tmp_path: Path) -> Path:
    return _write_component(
        tmp_path / "checkout",
        """
        def test_ok():
            assert True
        """,
    )


@pytest.fixture
def failing_repo(tmp_path: Path) -> Path:
    return _write_component(
        tmp_path / "fraud",
        """
        def test_broken():
            assert False, "the migration broke this consumer"
        """,
    )


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """A component with no tests at all — proves nothing."""
    root = tmp_path / "no-tests"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Real execution
# ---------------------------------------------------------------------------

class TestRealPytestExecution:
    """pytest must actually run — not be simulated."""

    def test_passing_suite_is_verified(self, passing_repo: Path):
        result = run({"change_id": "cr-1"}, passing_repo)
        assert result["status"] == "verified"
        content = result["evidence"][0]["content"]
        assert content["tests_passed"] is True
        assert content["outcome"] == "tests_passed"
        assert content["exit_code"] == 0

    def test_output_is_captured_from_a_real_run(self, passing_repo: Path):
        """Evidence must contain genuine pytest output, not a canned string."""
        result = run({"change_id": "cr-1"}, passing_repo)
        output = result["evidence"][0]["content"]["output_tail"]
        assert "test_component.py" in output or "1 passed" in output

    def test_consumer_defaults_to_directory_name(self, passing_repo: Path):
        result = run({"change_id": "cr-1"}, passing_repo)
        assert result["consumer"] == "checkout"

    def test_explicit_consumer_overrides_directory_name(self, passing_repo: Path):
        result = run({"change_id": "cr-1", "consumer": "analytics-worker"}, passing_repo)
        assert result["consumer"] == "analytics-worker"


# ---------------------------------------------------------------------------
# Failure detection — the part that protects the gate
# ---------------------------------------------------------------------------

class TestFailureDetection:
    """A failing suite must never be reported as verified."""

    def test_failing_suite_is_failed(self, failing_repo: Path):
        result = run({"change_id": "cr-1"}, failing_repo)
        assert result["status"] == "failed"
        assert result["evidence"][0]["content"]["tests_passed"] is False

    def test_failing_suite_is_distinguished_from_unrunnable(self, failing_repo: Path):
        result = run({"change_id": "cr-1"}, failing_repo)
        assert result["evidence"][0]["content"]["outcome"] == "tests_failed"

    def test_failure_output_is_retained_for_diagnosis(self, failing_repo: Path):
        result = run({"change_id": "cr-1"}, failing_repo)
        output = result["evidence"][0]["content"]["output_tail"]
        assert "the migration broke this consumer" in output

    def test_nonzero_exit_code_is_recorded(self, failing_repo: Path):
        result = run({"change_id": "cr-1"}, failing_repo)
        assert result["evidence"][0]["content"]["exit_code"] not in (0, None)


class TestUnrunnableSuites:
    """Absence of evidence is not evidence of safety."""

    def test_empty_suite_is_not_a_pass(self, empty_repo: Path):
        """
        pytest exits 5 on 'no tests collected'. A consumer with no tests has
        proven nothing, so it must not reach the gate as verified.
        """
        result = run({"change_id": "cr-1"}, empty_repo)
        assert result["status"] == "failed"
        assert result["evidence"][0]["content"]["outcome"] == "tests_could_not_run"

    def test_missing_directory_is_failed_not_an_exception(self, tmp_path: Path):
        result = run({"change_id": "cr-1"}, tmp_path / "nope")
        assert result["status"] == "failed"
        assert result["evidence"][0]["content"]["outcome"] == "tests_could_not_run"

    def test_missing_directory_records_no_revision(self, tmp_path: Path):
        result = run({"change_id": "cr-1"}, tmp_path / "nope")
        assert result["evidence"][0]["source_revision"] is None


# ---------------------------------------------------------------------------
# Source revision — what makes stale-evidence detection possible
# ---------------------------------------------------------------------------

class TestSourceRevision:
    def test_real_sha_recorded_for_a_git_repo(self, passing_repo: Path):
        """The recorded SHA must be the repo's actual HEAD, not invented."""
        subprocess.run(["git", "init"], cwd=passing_repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=passing_repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=passing_repo, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=passing_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=passing_repo, capture_output=True)

        expected = subprocess.run(
            ["git", "-C", str(passing_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        result = run({"change_id": "cr-1"}, passing_repo)
        recorded = result["evidence"][0]["source_revision"]
        assert recorded == expected
        assert len(recorded) == 40


# ---------------------------------------------------------------------------
# Contract compliance
# ---------------------------------------------------------------------------

class TestSchemaCompliance:
    @pytest.mark.parametrize("repo_fixture", ["passing_repo", "failing_repo", "empty_repo"])
    def test_output_validates_against_shared_schema(self, repo_fixture, request):
        repo = request.getfixturevalue(repo_fixture)
        result = run({"change_id": "cr-1"}, repo)
        model = VerificationResult(**result)
        assert model.change_id == "cr-1"
        assert model.status in ("verified", "failed")

    def test_only_test_result_evidence_is_emitted(self, passing_repo: Path):
        result = run({"change_id": "cr-1"}, passing_repo)
        assert {e["claim_type"] for e in result["evidence"]} == {"test_result"}


class TestSafetyConstraints:
    """
    AGENTS.md invariants 1, 2 and 3.

    These inspect the module's *imports* via AST rather than grepping the
    source, because the module docstring legitimately names the things the
    agent must not do. Prose about the ledger is fine; importing it is not.
    """

    @staticmethod
    def _imported_names() -> set[str]:
        """Every module name this agent imports, including inside functions."""
        tree = ast.parse(Path(contract_test.__file__).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
        return names

    def test_agent_does_not_import_sqlite(self):
        """Agents never open the database (invariant 2)."""
        assert not any(n.startswith("sqlite3") for n in self._imported_names())

    def test_agent_does_not_import_the_ledger(self):
        """
        Importing the ledger would let the agent write the database directly.
        Checked at runtime as well, mirroring tests/verification/test_critic.py.
        """
        assert not any("ledger" in n for n in self._imported_names())

        import orchestrator.ledger as ledger_module
        for key, value in vars(contract_test).items():
            assert value is not ledger_module, (
                f"contract_test.py imports orchestrator.ledger (as '{key}')."
            )

    def test_agent_does_not_import_other_agents(self):
        """Agents never call other agents (invariant 3)."""
        assert not any(n.startswith("agents") for n in self._imported_names())

    def test_agent_does_not_import_the_gate(self):
        """The gate is computed in exactly one place, and it is not here."""
        assert not any("gate" in n for n in self._imported_names())

    def test_agent_never_emits_a_gate_verdict(self, passing_repo: Path, failing_repo: Path):
        """
        The gate's vocabulary must not appear in any result this agent returns,
        for either outcome (invariant 1).
        """
        for repo in (passing_repo, failing_repo):
            rendered = str(run({"change_id": "cr-1"}, repo))
            assert "NOT_PROVEN_SAFE" not in rendered
            assert "VERIFIED" not in rendered

    def test_real_fixtures_are_not_mutated(self):
        """
        Running against the shipped fixtures must leave them untouched — the
        agent only reads and executes.
        """
        repo_root = Path(__file__).resolve().parents[2]
        checkout = repo_root / "fixtures" / "checkout"
        before = {
            p.relative_to(checkout): p.read_bytes()
            for p in checkout.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        run({"change_id": "cr-1"}, checkout)
        after = {
            p.relative_to(checkout): p.read_bytes()
            for p in checkout.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert before == after
