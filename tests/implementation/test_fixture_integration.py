"""
tests/implementation/test_fixture_integration.py

Integration tests that run against the REAL fixtures/ directories.

Two categories:

  A. State assertions — verify the real fixture files are in the correct
     pre-migration state (customer_id baseline; demo starts before migration).

  B. Worktree integration — obtain actual committed pre-migration fixture
     content from commit c4774c2, place that content into an isolated
     temporary Git repo, run provider-patch/consumer-migration there, run
     real pytest, and verify the resulting Git SHA and source changes.
     These tests are non-destructive: the checked-out fixtures are never
     touched.

Category B tests are marked with pytest.mark.integration so they can be
run separately with:
    pytest -m integration tests/implementation/test_fixture_integration.py

Category A run as normal tests (fast, no agent invocation).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agents.implementation.provider_patch import run as patch_run
from agents.implementation.consumer_migration import run as migrate_run

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
FIXTURES = REPO_ROOT / "fixtures"

ACCOUNT_SERVICE = FIXTURES / "account-service"
CHECKOUT        = FIXTURES / "checkout"
FRAUD           = FIXTURES / "fraud"
ANALYTICS       = FIXTURES / "analytics-worker"

# Commit that holds the real pre-migration fixture baseline
BASELINE_COMMIT = "c4774c2"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

CR = {
    "change_request": {
        "id": "cr-001",
        "old_field": "customer_id",
        "new_field": "account_id",
        "provider": "account-service",
    },
    "strategy_result": {},
}


def _is_sha(s: str) -> bool:
    return bool(SHA_RE.fullmatch(s))


def _git_show(commit: str, path: str) -> str:
    """Return the content of a file at a given commit from the main repo."""
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git show {commit}:{path} failed:\n{result.stderr}"
        )
    return result.stdout


def _sha_in_log(fixture_subdir: str, pattern: str) -> bool:
    """Return True if a commit with subject matching pattern exists for fixture_subdir."""
    result = subprocess.run(
        ["git", "log", "--format=%s", "--", fixture_subdir],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return pattern in result.stdout


# ===========================================================================
# A. State assertions — real checked-out fixtures are in PRE-MIGRATION state
# (customer_id baseline; demo starts before migration)
# ===========================================================================

class TestAccountServicePreMigrationState:
    """account-service must be in customer_id-only baseline (pre-migration)."""

    def test_app_py_has_customer_id(self):
        content = (ACCOUNT_SERVICE / "app.py").read_text()
        assert "customer_id" in content, "customer_id must be present in app.py (pre-migration)"

    def test_app_py_no_account_id_in_code(self):
        content = (ACCOUNT_SERVICE / "app.py").read_text(encoding="utf-8")
        # The docstring may mention "account_id" as a migration target; check
        # that it does not appear as a Python identifier or dict key in code.
        import ast
        try:
            tree = ast.parse(content)
        except SyntaxError:
            pytest.fail("app.py is not valid Python at pre-migration baseline")
        # Walk the AST: no attribute/name node should be 'account_id'
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "account_id":
                pytest.fail("account_id appears as a code Name in app.py — must be pre-migration baseline")
            if isinstance(node, ast.Attribute) and node.attr == "account_id":
                pytest.fail("account_id appears as an Attribute in app.py — must be pre-migration baseline")
            if isinstance(node, ast.Constant) and node.value == "account_id":
                pytest.fail("account_id appears as a string constant in app.py — must be pre-migration baseline")

    def test_openapi_has_customer_id(self):
        content = (ACCOUNT_SERVICE / "openapi.yaml").read_text()
        assert "customer_id" in content, "openapi.yaml must document customer_id (pre-migration)"

    def test_openapi_no_account_id(self):
        content = (ACCOUNT_SERVICE / "openapi.yaml").read_text()
        assert "account_id" not in content, (
            "openapi.yaml must not document account_id — fixture must be pre-migration baseline"
        )

    def test_provider_patch_commit_exists_in_history(self):
        """The provider-patch migration commit must remain visible in git log history."""
        assert _sha_in_log("fixtures/account-service/", "provider-patch"), (
            "A 'provider-patch' commit must appear in git log for account-service history"
        )

    def test_pre_migration_tests_pass(self):
        """The real account-service test suite must pass at the pre-migration baseline."""
        result = subprocess.run(
            ["python", "-m", "pytest", str(ACCOUNT_SERVICE), "-v", "--tb=short"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"account-service tests must pass at pre-migration baseline:\n{result.stdout[-2000:]}"
        )


class TestCheckoutPreMigrationState:
    """checkout must be in customer_id baseline (pre-migration)."""

    def test_checkout_py_uses_customer_id(self):
        content = (CHECKOUT / "checkout.py").read_text()
        assert '"customer_id"' in content, "checkout.py must use customer_id key (pre-migration)"

    def test_checkout_py_no_account_id_key(self):
        content = (CHECKOUT / "checkout.py").read_text()
        assert '"account_id"' not in content, (
            'checkout.py must not use "account_id" key — fixture must be pre-migration baseline'
        )

    def test_consumer_migration_commit_exists_in_history(self):
        assert _sha_in_log("fixtures/checkout/", "consumer-migration(checkout)"), (
            "A consumer-migration(checkout) commit must appear in git log history"
        )

    def test_pre_migration_tests_pass(self):
        result = subprocess.run(
            ["python", "-m", "pytest", str(CHECKOUT), "-v", "--tb=short"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"checkout tests must pass at pre-migration baseline:\n{result.stdout[-2000:]}"
        )


class TestFraudPreMigrationState:
    """fraud must be in customer_id baseline (pre-migration)."""

    def test_fraud_py_uses_customer_id(self):
        content = (FRAUD / "fraud.py").read_text()
        assert '"customer_id"' in content, "fraud.py must use customer_id (pre-migration)"

    def test_fraud_py_no_account_id_key(self):
        content = (FRAUD / "fraud.py").read_text()
        assert '"account_id"' not in content, (
            'fraud.py must not use "account_id" key — fixture must be pre-migration baseline'
        )

    def test_consumer_migration_commit_exists_in_history(self):
        assert _sha_in_log("fixtures/fraud/", "consumer-migration(fraud)"), (
            "A consumer-migration(fraud) commit must appear in git log history"
        )

    def test_pre_migration_tests_pass(self):
        result = subprocess.run(
            ["python", "-m", "pytest", str(FRAUD), "-v", "--tb=short"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"fraud tests must pass at pre-migration baseline:\n{result.stdout[-2000:]}"
        )


class TestAnalyticsWorkerPreMigrationState:
    """analytics-worker must use event["customer_id"] — pre-migration baseline."""

    def test_worker_py_uses_event_customer_id(self):
        content = (ANALYTICS / "worker.py").read_text()
        assert 'event["customer_id"]' in content, (
            'worker.py must use event["customer_id"] (pre-migration baseline)'
        )

    def test_worker_py_no_event_account_id(self):
        content = (ANALYTICS / "worker.py").read_text()
        assert 'event["account_id"]' not in content, (
            'event["account_id"] must not be in worker.py — fixture must be pre-migration baseline'
        )

    def test_worker_py_no_discovery_giveaway_text(self):
        """Analytics worker must not contain comments that hint at hidden/undocumented dependency."""
        content = (ANALYTICS / "worker.py").read_text()
        assert "undocumented" not in content, (
            "worker.py must not contain 'undocumented' discovery-giveaway text"
        )
        assert "Discovery agents must find" not in content, (
            "worker.py must not contain instructions directing a Discovery agent"
        )
        assert "never listed in an API contract" not in content, (
            "worker.py must not contain discovery-hint text"
        )

    def test_consumer_migration_commit_exists_in_history(self):
        assert _sha_in_log("fixtures/analytics-worker/", "consumer-migration(analytics-worker)"), (
            "A consumer-migration(analytics-worker) commit must appear in git log history"
        )

    def test_pre_migration_tests_pass(self):
        result = subprocess.run(
            ["python", "-m", "pytest", str(ANALYTICS), "-v", "--tb=short"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"analytics-worker tests must pass at pre-migration baseline:\n{result.stdout[-2000:]}"
        )


# ===========================================================================
# B. Worktree integration tests — reproducible end-to-end agent invocations
#
# Obtains actual committed pre-migration fixture content from commit c4774c2,
# places that content into an isolated temporary Git repo, runs the agent,
# runs real pytest, and verifies the resulting Git SHA and source changes.
# Never modifies the checked-out fixtures.
# ===========================================================================

def _init_worktree(dest: Path) -> None:
    """Initialise a fresh git repo with local identity at dest."""
    subprocess.run(["git", "init"], capture_output=True, cwd=str(dest), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@interlock.dev"],
        capture_output=True, cwd=str(dest), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Interlock Test"],
        capture_output=True, cwd=str(dest), check=True,
    )


def _write_and_commit(dest: Path, files: dict[str, str], message: str) -> str:
    """Write files into dest, stage, and commit. Returns the commit SHA."""
    for rel_path, content in files.items():
        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], capture_output=True, cwd=str(dest), check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True, cwd=str(dest), check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=str(dest), check=True,
    )
    return result.stdout.strip()


def _build_account_service_worktree(dest: Path) -> str:
    """
    Populate dest with real pre-migration account-service content from c4774c2.
    Returns the baseline commit SHA.
    """
    _init_worktree(dest)
    files = {
        "conftest.py":           _git_show(BASELINE_COMMIT, "fixtures/account-service/conftest.py"),
        "app.py":                _git_show(BASELINE_COMMIT, "fixtures/account-service/app.py"),
        "openapi.yaml":          _git_show(BASELINE_COMMIT, "fixtures/account-service/openapi.yaml"),
        "tests/__init__.py":     _git_show(BASELINE_COMMIT, "fixtures/account-service/tests/__init__.py"),
        "tests/test_app.py":     _git_show(BASELINE_COMMIT, "fixtures/account-service/tests/test_app.py"),
    }
    return _write_and_commit(dest, files, "baseline: real pre-migration account-service from c4774c2")


def _build_checkout_worktree(dest: Path) -> str:
    """
    Populate dest with real pre-migration checkout content from c4774c2.
    Returns the baseline commit SHA.
    """
    _init_worktree(dest)
    files = {
        "conftest.py":              _git_show(BASELINE_COMMIT, "fixtures/checkout/conftest.py"),
        "checkout.py":              _git_show(BASELINE_COMMIT, "fixtures/checkout/checkout.py"),
        "tests/__init__.py":        _git_show(BASELINE_COMMIT, "fixtures/checkout/tests/__init__.py"),
        "tests/test_checkout.py":   _git_show(BASELINE_COMMIT, "fixtures/checkout/tests/test_checkout.py"),
    }
    return _write_and_commit(dest, files, "baseline: real pre-migration checkout from c4774c2")


def _build_analytics_worktree(dest: Path) -> str:
    """
    Populate dest with real pre-migration analytics-worker content from c4774c2.
    Returns the baseline commit SHA.
    """
    _init_worktree(dest)
    files = {
        "conftest.py":               _git_show(BASELINE_COMMIT, "fixtures/analytics-worker/conftest.py"),
        "worker.py":                 _git_show(BASELINE_COMMIT, "fixtures/analytics-worker/worker.py"),
        "tests/__init__.py":         _git_show(BASELINE_COMMIT, "fixtures/analytics-worker/tests/__init__.py"),
        "tests/test_worker.py":      _git_show(BASELINE_COMMIT, "fixtures/analytics-worker/tests/test_worker.py"),
    }
    return _write_and_commit(dest, files, "baseline: real pre-migration analytics-worker from c4774c2")


@pytest.mark.integration
class TestProviderPatchWorktreeIntegration:
    """
    Run provider-patch against a worktree populated with the REAL committed
    pre-migration account-service content from c4774c2.
    """

    def test_provider_patch_on_real_fixture_content(self, tmp_path: Path):
        """
        provider-patch must succeed on the exact content committed at c4774c2.
        """
        dest = tmp_path / "account-service"
        dest.mkdir()
        baseline_sha = _build_account_service_worktree(dest)

        result = patch_run(CR, dest)

        assert result["status"] == "success", f"provider-patch failed: {result}"
        assert _is_sha(result["commit_sha"]), f"Expected 40-char hex SHA, got: {result['commit_sha']}"
        # New field must be introduced
        app_content = (dest / "app.py").read_text(encoding="utf-8")
        assert "account_id" in app_content, "account_id must be present after provider-patch"
        # Old field must be retained (dual-field window)
        assert "customer_id" in app_content, "customer_id must be retained after provider-patch"
        # OpenAPI must document the new field
        assert "account_id" in (dest / "openapi.yaml").read_text(encoding="utf-8")
        # Resulting SHA must be different from (i.e., newer than) baseline
        assert result["commit_sha"] != baseline_sha, (
            "provider-patch commit SHA must differ from pre-migration baseline SHA"
        )

    def test_provider_patch_commit_sha_verified_in_worktree(self, tmp_path: Path):
        """SHA returned by provider-patch must exist as a git object in the worktree."""
        dest = tmp_path / "account-service"
        dest.mkdir()
        _build_account_service_worktree(dest)

        result = patch_run(CR, dest)
        sha = result["commit_sha"]

        verify = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", sha], capture_output=True
        )
        assert verify.returncode == 0, f"SHA {sha} does not exist as a git object in worktree"

    def test_provider_patch_git_log_shows_patch_commit(self, tmp_path: Path):
        """git log in the worktree must show a provider-patch commit after the agent runs."""
        dest = tmp_path / "account-service"
        dest.mkdir()
        _build_account_service_worktree(dest)

        patch_run(CR, dest)

        log = subprocess.run(
            ["git", "-C", str(dest), "log", "--format=%s"],
            capture_output=True, text=True,
        )
        assert "provider-patch" in log.stdout, (
            "git log in worktree must contain a provider-patch commit"
        )


@pytest.mark.integration
class TestConsumerMigrationWorktreeIntegration:
    """
    Run consumer-migration against worktrees populated with the REAL committed
    pre-migration fixture content from c4774c2.
    """

    def test_checkout_migration_on_real_fixture_content(self, tmp_path: Path):
        """
        consumer-migration(checkout) must succeed on the exact content committed at c4774c2.
        """
        dest = tmp_path / "checkout"
        dest.mkdir()
        baseline_sha = _build_checkout_worktree(dest)

        result = migrate_run({**CR, "consumer": "checkout"}, dest)

        assert result["status"] == "success", f"migrate_run failed: {result}"
        assert _is_sha(result["commit_sha"]), f"Expected 40-char hex SHA, got: {result['commit_sha']}"
        src = (dest / "checkout.py").read_text(encoding="utf-8")
        assert '"account_id"' in src, "checkout.py must use account_id after migration"
        assert '"customer_id"' not in src, "checkout.py must not retain customer_id key after migration"
        assert result["commit_sha"] != baseline_sha

    def test_analytics_event_key_replaced_on_real_fixture_content(self, tmp_path: Path):
        """
        The canonical discovery-demo migration: event["customer_id"]
        must become event["account_id"] when run against the real c4774c2 content.
        """
        dest = tmp_path / "analytics-worker"
        dest.mkdir()
        baseline_sha = _build_analytics_worktree(dest)

        result = migrate_run({**CR, "consumer": "analytics-worker"}, dest)

        assert result["status"] == "success", f"migrate_run failed: {result}"
        src = (dest / "worker.py").read_text(encoding="utf-8")
        assert 'event["account_id"]' in src, "worker.py must use event[account_id] after migration"
        assert 'event["customer_id"]' not in src, "worker.py must not retain event[customer_id] after migration"
        assert result["commit_sha"] != baseline_sha

    def test_checkout_and_analytics_produce_distinct_shas(self, tmp_path: Path):
        """Running migration on two separate worktrees yields two different SHAs."""
        dest_c = tmp_path / "checkout"
        dest_a = tmp_path / "analytics-worker"
        dest_c.mkdir()
        dest_a.mkdir()
        _build_checkout_worktree(dest_c)
        _build_analytics_worktree(dest_a)

        r_c = migrate_run({**CR, "consumer": "checkout"}, dest_c)
        r_a = migrate_run({**CR, "consumer": "analytics-worker"}, dest_a)

        assert r_c["commit_sha"] != r_a["commit_sha"], (
            "Two independent migration runs must produce distinct commit SHAs"
        )

    def test_migration_scoped_to_worktree_path(self, tmp_path: Path):
        """Migrating checkout worktree must not touch the analytics-worker worktree."""
        dest_c = tmp_path / "checkout"
        dest_a = tmp_path / "analytics-worker"
        dest_c.mkdir()
        dest_a.mkdir()
        _build_checkout_worktree(dest_c)
        _build_analytics_worktree(dest_a)

        analytics_before = (dest_a / "worker.py").read_text(encoding="utf-8")
        migrate_run({**CR, "consumer": "checkout"}, dest_c)
        analytics_after = (dest_a / "worker.py").read_text(encoding="utf-8")

        assert analytics_before == analytics_after, (
            "Migrating checkout worktree must not modify analytics-worker/worker.py"
        )

    def test_checkout_migration_git_log_shows_migration_commit(self, tmp_path: Path):
        """git log in the worktree must show a consumer-migration commit after the agent runs."""
        dest = tmp_path / "checkout"
        dest.mkdir()
        _build_checkout_worktree(dest)

        migrate_run({**CR, "consumer": "checkout"}, dest)

        log = subprocess.run(
            ["git", "-C", str(dest), "log", "--format=%s"],
            capture_output=True, text=True,
        )
        assert "consumer-migration(checkout)" in log.stdout, (
            "git log in worktree must contain a consumer-migration(checkout) commit"
        )
