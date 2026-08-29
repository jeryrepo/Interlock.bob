"""
tests/implementation/test_provider_patch.py

Proves that provider_patch.run():
  1. Actually modifies source files on disk.
  2. Introduces account_id.
  3. Retains customer_id during the compatibility window.
  4. Runs real pytest in the target repository.
  5. Prevents a successful commit when the target test suite fails.
  6. Creates a real Git commit on success.
  7. Returns a SHA that actually exists in the target repo.
  8. Remains scoped to the supplied target repository (never touches main repo).

All assertions use only isolated tmp_path repos — zero commits land on the
feature/planning branch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agents.implementation.provider_patch import run as patch_run


# ---------------------------------------------------------------------------
# SHA format helper
# ---------------------------------------------------------------------------

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _is_valid_sha(sha: str) -> bool:
    return bool(SHA_RE.fullmatch(sha))


def _sha_exists(sha: str, repo_path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "cat-file", "-e", sha],
        capture_output=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Test: source files are actually modified on disk
# ---------------------------------------------------------------------------

class TestSourceModification:
    def test_provider_patch_modifies_app_py(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """app.py on disk must differ from its pre-patch state after run()."""
        original = (tmp_provider_repo / "app.py").read_text()
        patch_run(patch_data, tmp_provider_repo)
        patched = (tmp_provider_repo / "app.py").read_text()
        assert patched != original, "app.py must be modified by provider-patch"

    def test_files_changed_list_is_non_empty(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """Returned files_changed must list at least one file."""
        result = patch_run(patch_data, tmp_provider_repo)
        assert len(result["files_changed"]) >= 1

    def test_files_changed_are_real_paths(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """Every path in files_changed must exist on disk."""
        result = patch_run(patch_data, tmp_provider_repo)
        for rel in result["files_changed"]:
            full = tmp_provider_repo / rel
            assert full.exists(), f"Listed changed file does not exist: {rel}"


# ---------------------------------------------------------------------------
# Test: account_id is introduced
# ---------------------------------------------------------------------------

class TestNewFieldIntroduced:
    def test_provider_patch_adds_account_id_to_app_py(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """account_id must appear in app.py after patching."""
        patch_run(patch_data, tmp_provider_repo)
        content = (tmp_provider_repo / "app.py").read_text()
        assert "account_id" in content, "account_id must be introduced in app.py"

    def test_provider_patch_adds_account_id_to_test_file(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """A test asserting account_id must exist somewhere in tests/ after patching."""
        patch_run(patch_data, tmp_provider_repo)
        test_files = list((tmp_provider_repo / "tests").rglob("test_*.py"))
        assert test_files, "At least one test file must exist after patching"
        combined = "\n".join(p.read_text() for p in test_files)
        assert "account_id" in combined, "account_id assertion must appear in test files"

    def test_provider_patch_status_is_success(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        result = patch_run(patch_data, tmp_provider_repo)
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Test: customer_id is retained (dual-field compatibility window)
# ---------------------------------------------------------------------------

class TestOldFieldRetained:
    def test_customer_id_remains_in_app_py(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """customer_id must still be present in app.py after patching."""
        patch_run(patch_data, tmp_provider_repo)
        content = (tmp_provider_repo / "app.py").read_text()
        assert "customer_id" in content, (
            "customer_id must be RETAINED in app.py during the compatibility window"
        )

    def test_customer_id_not_removed_from_tests(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """The original test asserting customer_id must not be deleted."""
        patch_run(patch_data, tmp_provider_repo)
        test_app = (tmp_provider_repo / "tests" / "test_app.py").read_text()
        assert "customer_id" in test_app, (
            "test_app.py must still reference customer_id after patching"
        )


# ---------------------------------------------------------------------------
# Test: real pytest executes and passes
# ---------------------------------------------------------------------------

class TestPytestExecution:
    def test_provider_patch_does_not_raise_on_passing_tests(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """run() must complete without raising when all tests pass."""
        # Should not raise — if it does, the test fails naturally.
        result = patch_run(patch_data, tmp_provider_repo)
        assert result["status"] == "success"

    def test_provider_patch_evidence_contains_pytest_output(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """Evidence content must carry the pytest_output_tail key."""
        result = patch_run(patch_data, tmp_provider_repo)
        assert len(result["evidence"]) >= 1
        ev = result["evidence"][0]
        assert "pytest_output_tail" in ev["content"]
        # Output tail must be a non-empty string
        assert isinstance(ev["content"]["pytest_output_tail"], str)
        assert len(ev["content"]["pytest_output_tail"]) > 0


# ---------------------------------------------------------------------------
# Test: failing target tests prevent successful completion
# ---------------------------------------------------------------------------

class TestFailureGate:
    def test_failing_test_raises_runtime_error(
        self, tmp_provider_repo_broken_tests: Path, patch_data: dict
    ):
        """
        When the target repository's test suite fails, provider_patch must raise
        RuntimeError and must NOT create a commit.
        """
        repo = tmp_provider_repo_broken_tests
        initial_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        with pytest.raises(RuntimeError) as exc_info:
            patch_run(patch_data, repo)

        # Error message must contain useful context
        err_msg = str(exc_info.value)
        assert "pytest failed" in err_msg.lower() or "exit" in err_msg.lower(), (
            f"RuntimeError should mention pytest failure, got: {err_msg[:200]}"
        )

        # HEAD must not have advanced — no commit was created
        post_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert initial_sha == post_sha, (
            "A commit must NOT be created when the test suite fails"
        )


# ---------------------------------------------------------------------------
# Test: real Git commit is produced
# ---------------------------------------------------------------------------

class TestGitCommit:
    def test_provider_patch_commit_sha_is_real(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """Returned commit_sha must be a valid 40-char lowercase hex string."""
        result = patch_run(patch_data, tmp_provider_repo)
        sha = result["commit_sha"]
        assert _is_valid_sha(sha), f"commit_sha '{sha}' is not a valid 40-char hex SHA"

    def test_commit_sha_actually_exists_in_repo(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """The returned SHA must be a real, reachable object in the target repo."""
        result = patch_run(patch_data, tmp_provider_repo)
        sha = result["commit_sha"]
        assert _sha_exists(sha, tmp_provider_repo), (
            f"SHA {sha} does not exist as a git object in {tmp_provider_repo}"
        )

    def test_commit_sha_is_head_in_target_repo(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """The returned SHA must be HEAD of the target repo after run()."""
        result = patch_run(patch_data, tmp_provider_repo)
        head = subprocess.run(
            ["git", "-C", str(tmp_provider_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert result["commit_sha"] == head

    def test_commit_message_mentions_both_fields(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """Commit message must reference both new_field and old_field."""
        result = patch_run(patch_data, tmp_provider_repo)
        sha = result["commit_sha"]
        msg = subprocess.run(
            ["git", "-C", str(tmp_provider_repo), "log", "--format=%s", "-1", sha],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "account_id" in msg, f"Commit message missing account_id: {msg}"
        assert "customer_id" in msg, f"Commit message missing customer_id: {msg}"


# ---------------------------------------------------------------------------
# Test: returned SHA really exists
# ---------------------------------------------------------------------------

class TestSHAValidity:
    def test_sha_format_is_40_char_hex(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        result = patch_run(patch_data, tmp_provider_repo)
        assert _is_valid_sha(result["commit_sha"])

    def test_evidence_contains_commit_sha(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """evidence[0].source_revision must equal the returned commit_sha."""
        result = patch_run(patch_data, tmp_provider_repo)
        sha = result["commit_sha"]
        assert len(result["evidence"]) >= 1
        ev = result["evidence"][0]
        assert ev["source_revision"] == sha, (
            f"evidence source_revision '{ev['source_revision']}' != commit_sha '{sha}'"
        )

    def test_evidence_claim_type_is_migration_status(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        result = patch_run(patch_data, tmp_provider_repo)
        ev = result["evidence"][0]
        assert ev["claim_type"] == "migration_status"

    def test_evidence_confidence_is_confirmed(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        result = patch_run(patch_data, tmp_provider_repo)
        ev = result["evidence"][0]
        assert ev["confidence"] == "confirmed"


# ---------------------------------------------------------------------------
# Test: modification remains scoped to the supplied target repository
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_patch_scoped_to_target_repo(self, tmp_path: Path, patch_data: dict):
        """
        Two isolated repos: patching repo A must not change repo B.
        """
        import textwrap as _tw

        def _make_repo(name: str) -> Path:
            repo = tmp_path / name
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "t@t.dev"],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "T"],
                capture_output=True,
            )
            (repo / "app.py").write_text(
                _tw.dedent("""\
                    class Resp:
                        customer_id: str = ""
                """),
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(repo), "add", "."], capture_output=True
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "initial"],
                capture_output=True,
            )
            return repo

        repo_a = _make_repo("repo-a")
        repo_b = _make_repo("repo-b")

        original_b = (repo_b / "app.py").read_text()

        patch_run(patch_data, repo_a)

        current_b = (repo_b / "app.py").read_text()
        assert current_b == original_b, (
            "repo-b must not be modified when patching repo-a"
        )

    def test_result_repository_field_matches_supplied_path(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """result['repository'] must equal str(repo_path) as supplied."""
        result = patch_run(patch_data, tmp_provider_repo)
        assert result["repository"] == str(tmp_provider_repo)

    def test_main_interlock_repo_head_unchanged(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """
        The Interlock project repo's HEAD must not change during provider-patch.
        Verifies git scoping via -C flag.
        """
        import os
        project_root = Path(__file__).parent.parent.parent
        if not (project_root / ".git").exists():
            pytest.skip("Not inside a git repository")

        before = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        patch_run(patch_data, tmp_provider_repo)

        after = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        assert before == after, (
            "Main Interlock repo HEAD must not change when provider-patch "
            "runs against a tmp repo"
        )


# ---------------------------------------------------------------------------
# Test: OpenAPI spec is updated
# ---------------------------------------------------------------------------

class TestOpenAPIUpdate:
    def test_openapi_yaml_updated(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """openapi.yaml must mention account_id after patching."""
        patch_run(patch_data, tmp_provider_repo)
        spec = (tmp_provider_repo / "openapi.yaml").read_text()
        assert "account_id" in spec, "openapi.yaml must be updated with account_id"

    def test_openapi_yaml_still_contains_customer_id(
        self, tmp_provider_repo: Path, patch_data: dict
    ):
        """customer_id must remain in openapi.yaml during the compatibility window."""
        patch_run(patch_data, tmp_provider_repo)
        spec = (tmp_provider_repo / "openapi.yaml").read_text()
        assert "customer_id" in spec, (
            "customer_id must be retained in openapi.yaml during compatibility period"
        )
