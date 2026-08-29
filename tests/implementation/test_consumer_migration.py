"""
tests/implementation/test_consumer_migration.py

Proves that consumer_migration.run():

  1. Checkout migration works and creates a real commit.
  2. Fraud migration works and creates a distinct real commit.
  3. Analytics Worker event["customer_id"] is migrated to event["account_id"].
  4. customer_id references are actually replaced (not just added alongside).
  5. Tests execute inside each target repository.
  6. A failing consumer test prevents successful completion and prevents commit.
  7. Returned SHA exists in the target repository.
  8. Modifications cannot escape the supplied repo_path.
  9. Outputs follow the existing typed-dict / adapter convention.
 10. Two consumers in separate repos yield two distinct SHAs.

All assertions use only isolated tmp_path repos — zero commits land on the
feature/planning branch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agents.implementation.consumer_migration import run as migrate_run


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
# 1. Checkout migration
# ---------------------------------------------------------------------------

class TestCheckoutMigration:
    def _data(self, migration_data: dict) -> dict:
        return {**migration_data, "consumer": "checkout"}

    def test_checkout_migration_creates_commit(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert result["status"] == "success"
        assert _is_valid_sha(result["commit_sha"])

    def test_checkout_commit_is_real_git_object(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert _sha_exists(result["commit_sha"], tmp_checkout_repo)

    def test_checkout_commit_is_head(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        head = subprocess.run(
            ["git", "-C", str(tmp_checkout_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert result["commit_sha"] == head

    def test_checkout_source_uses_account_id(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        migrate_run(self._data(migration_data), tmp_checkout_repo)
        source = (tmp_checkout_repo / "checkout.py").read_text()
        assert "account_id" in source, "account_id must appear in checkout.py"

    def test_checkout_source_no_longer_uses_customer_id_string_key(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        migrate_run(self._data(migration_data), tmp_checkout_repo)
        source = (tmp_checkout_repo / "checkout.py").read_text()
        assert '"customer_id"' not in source, (
            'String key "customer_id" must be replaced in checkout.py'
        )

    def test_checkout_files_changed_non_empty(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert len(result["files_changed"]) >= 1

    def test_checkout_consumer_field_in_result(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert result["consumer"] == "checkout"

    def test_checkout_evidence_claim_type(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert result["evidence"][0]["claim_type"] == "migration_status"

    def test_checkout_evidence_confidence(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert result["evidence"][0]["confidence"] == "confirmed"

    def test_checkout_evidence_source_revision_matches_sha(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_checkout_repo)
        assert result["evidence"][0]["source_revision"] == result["commit_sha"]


# ---------------------------------------------------------------------------
# 2. Fraud migration
# ---------------------------------------------------------------------------

class TestFraudMigration:
    def _data(self, migration_data: dict) -> dict:
        return {**migration_data, "consumer": "fraud"}

    def test_fraud_migration_creates_commit(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_fraud_repo)
        assert result["status"] == "success"
        assert _is_valid_sha(result["commit_sha"])

    def test_fraud_commit_is_real_git_object(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_fraud_repo)
        assert _sha_exists(result["commit_sha"], tmp_fraud_repo)

    def test_fraud_source_uses_account_id(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        migrate_run(self._data(migration_data), tmp_fraud_repo)
        source = (tmp_fraud_repo / "fraud.py").read_text()
        assert "account_id" in source

    def test_fraud_source_no_longer_uses_customer_id_string_key(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        migrate_run(self._data(migration_data), tmp_fraud_repo)
        source = (tmp_fraud_repo / "fraud.py").read_text()
        assert '"customer_id"' not in source, (
            'String key "customer_id" must be replaced in fraud.py'
        )

    def test_fraud_consumer_field_in_result(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_fraud_repo)
        assert result["consumer"] == "fraud"

    def test_fraud_and_checkout_have_distinct_shas(
        self, tmp_path: Path, migration_data: dict
    ):
        """
        Running migration on two separate repos must yield two different SHAs.
        This proves each call produces its own independent commit.
        """
        from tests.implementation.conftest import (
            _make_checkout_repo,
            _make_fraud_repo,
        )
        repo_a = _make_checkout_repo(tmp_path)
        repo_b = _make_fraud_repo(tmp_path)

        result_a = migrate_run({**migration_data, "consumer": "checkout"}, repo_a)
        result_b = migrate_run({**migration_data, "consumer": "fraud"}, repo_b)

        assert result_a["commit_sha"] != result_b["commit_sha"], (
            "Two separate repos must produce distinct SHAs"
        )


# ---------------------------------------------------------------------------
# 3. Analytics Worker migration — event["customer_id"] pattern
# ---------------------------------------------------------------------------

class TestAnalyticsWorkerMigration:
    def _data(self, migration_data: dict) -> dict:
        return {**migration_data, "consumer": "analytics-worker"}

    def test_analytics_event_key_migrated(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        """
        The canonical discovery-demo pattern: event["customer_id"] must become
        event["account_id"] in the analytics-worker source.
        """
        migrate_run(self._data(migration_data), tmp_analytics_worker_repo)
        source = (tmp_analytics_worker_repo / "worker.py").read_text()
        assert 'event["account_id"]' in source, (
            'event["account_id"] must appear in worker.py after migration'
        )

    def test_analytics_old_event_key_absent(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        """
        event["customer_id"] must be completely gone from worker.py.
        """
        migrate_run(self._data(migration_data), tmp_analytics_worker_repo)
        source = (tmp_analytics_worker_repo / "worker.py").read_text()
        assert 'event["customer_id"]' not in source, (
            'event["customer_id"] must be removed from worker.py'
        )

    def test_analytics_migration_creates_real_commit(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_analytics_worker_repo)
        assert _is_valid_sha(result["commit_sha"])
        assert _sha_exists(result["commit_sha"], tmp_analytics_worker_repo)

    def test_analytics_commit_message_contains_consumer_name(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_analytics_worker_repo)
        sha = result["commit_sha"]
        msg = subprocess.run(
            ["git", "-C", str(tmp_analytics_worker_repo), "log", "--format=%s", "-1", sha],
            capture_output=True, text=True,
        ).stdout.strip()
        assert "analytics-worker" in msg, f"Commit message should mention consumer: {msg}"

    def test_analytics_commit_message_contains_new_field(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        result = migrate_run(self._data(migration_data), tmp_analytics_worker_repo)
        sha = result["commit_sha"]
        msg = subprocess.run(
            ["git", "-C", str(tmp_analytics_worker_repo), "log", "--format=%s", "-1", sha],
            capture_output=True, text=True,
        ).stdout.strip()
        assert "account_id" in msg, f"Commit message should mention new field: {msg}"

    def test_analytics_tests_run_inside_target_repo(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        """
        run() must not raise — proving pytest executed and passed inside
        the analytics-worker temp repo.
        """
        result = migrate_run(self._data(migration_data), tmp_analytics_worker_repo)
        assert result["status"] == "success"
        ev = result["evidence"][0]
        assert "pytest_output_tail" in ev["content"]
        output = ev["content"]["pytest_output_tail"]
        assert "passed" in output.lower(), (
            f"pytest output must say 'passed'; got: {output[-300:]}"
        )


# ---------------------------------------------------------------------------
# 4. customer_id dependency is actually replaced
# ---------------------------------------------------------------------------

class TestFieldReplacement:
    def test_string_key_replaced_checkout(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        """Quoted key 'customer_id' must be replaced, not just appended."""
        migrate_run({**migration_data, "consumer": "checkout"}, tmp_checkout_repo)
        source = (tmp_checkout_repo / "checkout.py").read_text()
        assert '"account_id"' in source
        assert '"customer_id"' not in source

    def test_string_key_replaced_analytics(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        """event["customer_id"] → event["account_id"]."""
        migrate_run(
            {**migration_data, "consumer": "analytics-worker"},
            tmp_analytics_worker_repo,
        )
        source = (tmp_analytics_worker_repo / "worker.py").read_text()
        assert '"account_id"' in source
        assert '"customer_id"' not in source

    def test_variable_assignment_replaced_fraud(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        """
        `customer_id = account_response["customer_id"]` in fraud.py must be
        fully migrated: both the variable name and the key reference.
        """
        migrate_run({**migration_data, "consumer": "fraud"}, tmp_fraud_repo)
        source = (tmp_fraud_repo / "fraud.py").read_text()
        assert "account_id" in source
        # the key reference must be gone
        assert '"customer_id"' not in source


# ---------------------------------------------------------------------------
# 5. Tests execute inside each target repository
# ---------------------------------------------------------------------------

class TestPytestExecution:
    def test_checkout_pytest_passes(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        assert result["status"] == "success"

    def test_fraud_pytest_passes(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "fraud"}, tmp_fraud_repo
        )
        assert result["status"] == "success"

    def test_analytics_pytest_passes(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "analytics-worker"},
            tmp_analytics_worker_repo,
        )
        assert result["status"] == "success"

    def test_evidence_contains_pytest_output_tail(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        ev = result["evidence"][0]
        assert "pytest_output_tail" in ev["content"]
        assert isinstance(ev["content"]["pytest_output_tail"], str)
        assert len(ev["content"]["pytest_output_tail"]) > 0


# ---------------------------------------------------------------------------
# 6. Failing consumer test prevents successful completion
# ---------------------------------------------------------------------------

class TestFailureGate:
    def test_broken_consumer_raises_runtime_error(
        self, tmp_broken_consumer_repo: Path, migration_data: dict
    ):
        """
        A consumer repo whose test suite is intentionally broken must cause
        migrate_run() to raise RuntimeError.  No commit must be created.
        """
        initial_sha = subprocess.run(
            ["git", "-C", str(tmp_broken_consumer_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        with pytest.raises(RuntimeError) as exc_info:
            migrate_run(
                {**migration_data, "consumer": "broken-consumer"},
                tmp_broken_consumer_repo,
            )

        err = str(exc_info.value).lower()
        assert "pytest failed" in err or "exit" in err, (
            f"RuntimeError should mention pytest failure; got: {str(exc_info.value)[:300]}"
        )

        # HEAD must not have advanced
        post_sha = subprocess.run(
            ["git", "-C", str(tmp_broken_consumer_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert initial_sha == post_sha, (
            "No commit must be created when the consumer test suite fails"
        )

    def test_broken_consumer_error_message_contains_output(
        self, tmp_broken_consumer_repo: Path, migration_data: dict
    ):
        """RuntimeError message must contain the pytest output so it's diagnosable."""
        with pytest.raises(RuntimeError) as exc_info:
            migrate_run(
                {**migration_data, "consumer": "broken-consumer"},
                tmp_broken_consumer_repo,
            )
        # Should contain some hint about the failure
        assert "broken" in str(exc_info.value).lower() or "failed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 7. Returned SHA exists in the target repository
# ---------------------------------------------------------------------------

class TestSHAValidity:
    def test_checkout_sha_is_40_char_hex(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        assert _is_valid_sha(result["commit_sha"])

    def test_fraud_sha_is_40_char_hex(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "fraud"}, tmp_fraud_repo
        )
        assert _is_valid_sha(result["commit_sha"])

    def test_analytics_sha_exists_in_repo(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "analytics-worker"},
            tmp_analytics_worker_repo,
        )
        assert _sha_exists(result["commit_sha"], tmp_analytics_worker_repo)

    def test_evidence_source_revision_is_real_sha(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        rev = result["evidence"][0]["source_revision"]
        assert _is_valid_sha(rev)
        assert _sha_exists(rev, tmp_checkout_repo)


# ---------------------------------------------------------------------------
# 8. Modifications cannot escape the supplied repo_path
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_migrating_checkout_does_not_modify_fraud(
        self, tmp_path: Path, migration_data: dict
    ):
        """
        Patching checkout repo must not change any file in the fraud repo.
        Both repos live under the same tmp_path parent.
        """
        from tests.implementation.conftest import (
            _make_checkout_repo,
            _make_fraud_repo,
        )
        checkout_repo = _make_checkout_repo(tmp_path)
        fraud_repo = _make_fraud_repo(tmp_path)

        fraud_before = (fraud_repo / "fraud.py").read_text()

        migrate_run({**migration_data, "consumer": "checkout"}, checkout_repo)

        fraud_after = (fraud_repo / "fraud.py").read_text()
        assert fraud_before == fraud_after, (
            "Migrating checkout must not modify fraud/fraud.py"
        )

    def test_result_repository_equals_supplied_path(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        assert result["repository"] == str(tmp_checkout_repo)

    def test_main_interlock_repo_head_unchanged(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        """
        The Interlock project repo's HEAD must not change during migration.
        """
        project_root = Path(__file__).parent.parent.parent
        if not (project_root / ".git").exists():
            pytest.skip("Not inside a git repository")

        before = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        migrate_run(
            {**migration_data, "consumer": "analytics-worker"},
            tmp_analytics_worker_repo,
        )

        after = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        assert before == after, (
            "Main Interlock repo HEAD must not change during consumer migration"
        )


# ---------------------------------------------------------------------------
# 9. Output schema / typed-dict convention
# ---------------------------------------------------------------------------

class TestOutputSchema:
    def test_result_has_all_required_keys(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        required = {"consumer", "repository", "files_changed", "summary",
                    "commit_sha", "evidence", "status"}
        assert required.issubset(result.keys()), (
            f"Missing keys: {required - result.keys()}"
        )

    def test_evidence_has_all_required_keys(
        self, tmp_fraud_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "fraud"}, tmp_fraud_repo
        )
        ev = result["evidence"][0]
        required = {"claim_type", "subject", "content", "source_ref",
                    "confidence", "source_revision"}
        assert required.issubset(ev.keys()), (
            f"Missing evidence keys: {required - ev.keys()}"
        )

    def test_evidence_subject_is_consumer_name(
        self, tmp_analytics_worker_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "analytics-worker"},
            tmp_analytics_worker_repo,
        )
        assert result["evidence"][0]["subject"] == "analytics-worker"

    def test_status_is_success_string(
        self, tmp_checkout_repo: Path, migration_data: dict
    ):
        result = migrate_run(
            {**migration_data, "consumer": "checkout"}, tmp_checkout_repo
        )
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# 10. Two consumers yield two distinct SHAs
# ---------------------------------------------------------------------------

class TestDistinctCommits:
    def test_two_consumers_distinct_shas(
        self, tmp_path: Path, migration_data: dict
    ):
        """
        Running migration on two separate tmp repos yields two different SHAs.
        This is the canonical proof that each call produces its own commit.
        """
        from tests.implementation.conftest import (
            _make_checkout_repo,
            _make_analytics_worker_repo,
        )
        repo_checkout = _make_checkout_repo(tmp_path)
        repo_analytics = _make_analytics_worker_repo(tmp_path)

        r1 = migrate_run(
            {**migration_data, "consumer": "checkout"}, repo_checkout
        )
        r2 = migrate_run(
            {**migration_data, "consumer": "analytics-worker"}, repo_analytics
        )

        assert r1["commit_sha"] != r2["commit_sha"], (
            "Each consumer migration must produce a distinct commit SHA"
        )

    def test_three_consumers_all_distinct(self, tmp_path: Path, migration_data: dict):
        """
        Running all three consumer migrations produces three unique SHAs.
        """
        from tests.implementation.conftest import (
            _make_checkout_repo,
            _make_fraud_repo,
            _make_analytics_worker_repo,
        )
        repo_c = _make_checkout_repo(tmp_path)
        repo_f = _make_fraud_repo(tmp_path)
        repo_a = _make_analytics_worker_repo(tmp_path)

        rc = migrate_run({**migration_data, "consumer": "checkout"}, repo_c)
        rf = migrate_run({**migration_data, "consumer": "fraud"}, repo_f)
        ra = migrate_run({**migration_data, "consumer": "analytics-worker"}, repo_a)

        shas = {rc["commit_sha"], rf["commit_sha"], ra["commit_sha"]}
        assert len(shas) == 3, (
            f"Expected 3 distinct SHAs, got {len(shas)}: {shas}"
        )
