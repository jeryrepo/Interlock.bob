"""
tests/implementation/test_fixture_integration.py

Integration tests that copy fixture directories to a tmp_path worktree,
invoke the relevant agent, and then assert the post-migration state on the
tmp_path copy.  The real fixtures/ directory is NEVER modified.

Two categories:
  A. Agent-driven state assertions — copy a real fixture to tmp_path, run the
     agent against the copy, then assert the post-migration state.  These are
     the canonical tests that prove each agent produces the correct output.

  B. Worktree integration — copy each fixture into a tmp_path git worktree
     and re-run the agent from scratch to prove the full agent→fixture flow
     works end-to-end in a reproducible, non-destructive way.

Category B tests are marked with pytest.mark.integration so they can be
run separately with:
    pytest -m integration tests/implementation/test_fixture_integration.py

Category A run as normal tests (agent-invoked, tmp_path-isolated).
"""
from __future__ import annotations

import re
import shutil
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


def _sha_in_log(sha: str, fixture_subdir: str) -> bool:
    """Return True if sha appears in git log touching fixture_subdir."""
    result = subprocess.run(
        ["git", "log", "--format=%H", "--", fixture_subdir],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return sha in result.stdout


def _make_worktree(src: Path, dest: Path) -> None:
    """
    Copy src into dest, git-init it with local identity, and initial-commit.
    Used by both Category A and Category B tests.
    """
    shutil.copytree(str(src), str(dest))
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


# ===========================================================================
# A. Agent-driven state assertions
#
# Each test class:
#   1. Copies the real fixture to a tmp_path worktree (never touches fixtures/)
#   2. Invokes the relevant agent against the tmp_path copy
#   3. Asserts the post-migration state on the tmp_path copy
# ===========================================================================

def _prepare_account_service_worktree(src: Path, dest: Path) -> None:
    """
    Copy account-service into dest worktree and update the pre-migration
    test that asserts 'account_id' is absent (it was written for the
    pre-migration baseline; after provider-patch it would fail).
    The update mirrors what a real migration workflow would do — the test
    is fixed in the worktree, not in the real fixture.
    """
    _make_worktree(src, dest)
    test_app = dest / "tests" / "test_app.py"
    if test_app.exists():
        content = test_app.read_text(encoding="utf-8")
        # Remove the pre-migration-only test that asserts account_id is absent,
        # since provider-patch will add account_id and that test would fail.
        # We replace it with a post-migration assertion that both fields are present.
        updated = content.replace(
            'def test_get_account_no_account_id_pre_migration():\n'
            '    """Pre-migration: account_id is NOT yet in the response."""\n'
            '    result = get_account("cust-xyz")\n'
            '    assert "account_id" not in result',
            'def test_get_account_has_account_id_post_migration():\n'
            '    """Post-migration: account_id must be present in the response."""\n'
            '    result = get_account("cust-xyz")\n'
            '    assert "account_id" in result',
        )
        test_app.write_text(updated, encoding="utf-8")
        # Stage the updated test file so git is clean for patch_run
        subprocess.run(
            ["git", "-C", str(dest), "add", str(test_app)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(dest), "commit", "--amend", "--no-edit"],
            capture_output=True, cwd=str(dest),
        )


class TestAccountServiceState:
    """
    account-service must expose BOTH customer_id (retained) and account_id (added).
    Compatibility strategy: dual-field, NOT a full cutover.
    """

    def test_app_py_has_account_id(self, tmp_path: Path):
        dest = tmp_path / "account-service"
        _prepare_account_service_worktree(ACCOUNT_SERVICE, dest)
        patch_run(CR, dest)
        content = (dest / "app.py").read_text()
        assert "account_id" in content, "account_id must be present in app.py after provider-patch"

    def test_app_py_retains_customer_id(self, tmp_path: Path):
        dest = tmp_path / "account-service"
        _prepare_account_service_worktree(ACCOUNT_SERVICE, dest)
        patch_run(CR, dest)
        content = (dest / "app.py").read_text()
        assert "customer_id" in content, (
            "customer_id must be RETAINED in app.py (dual-field compatibility window)"
        )

    def test_openapi_has_account_id(self, tmp_path: Path):
        dest = tmp_path / "account-service"
        _prepare_account_service_worktree(ACCOUNT_SERVICE, dest)
        patch_run(CR, dest)
        content = (dest / "openapi.yaml").read_text()
        assert "account_id" in content, "openapi.yaml must document account_id after provider-patch"

    def test_openapi_retains_customer_id(self, tmp_path: Path):
        dest = tmp_path / "account-service"
        _prepare_account_service_worktree(ACCOUNT_SERVICE, dest)
        patch_run(CR, dest)
        content = (dest / "openapi.yaml").read_text()
        assert "customer_id" in content, "openapi.yaml must still document customer_id"

    def test_original_fixture_not_mutated(self, tmp_path: Path):
        """
        After running provider-patch against a tmp_path copy, the real
        fixtures/account-service/app.py must be unchanged (pre-migration).
        """
        original_content = (ACCOUNT_SERVICE / "app.py").read_text()
        dest = tmp_path / "account-service"
        _prepare_account_service_worktree(ACCOUNT_SERVICE, dest)
        patch_run(CR, dest)
        after_content = (ACCOUNT_SERVICE / "app.py").read_text()
        assert original_content == after_content, (
            "Real fixtures/account-service/app.py was mutated! "
            "The test must only operate on a tmp_path copy."
        )


class TestCheckoutState:
    """
    checkout must use account_id exclusively (full migration, customer_id fully absent).
    """

    def test_checkout_py_uses_account_id(self, tmp_path: Path):
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)
        migrate_run({**CR, "consumer": "checkout"}, dest)
        content = (dest / "checkout.py").read_text()
        assert '"account_id"' in content, "checkout.py must use account_id key after migration"

    def test_checkout_py_no_customer_id_key(self, tmp_path: Path):
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)
        migrate_run({**CR, "consumer": "checkout"}, dest)
        content = (dest / "checkout.py").read_text()
        assert '"customer_id"' not in content, (
            'checkout.py must not use "customer_id" key after migration'
        )

    def test_original_fixture_not_mutated(self, tmp_path: Path):
        """
        After running consumer-migration against a tmp_path copy, the real
        fixtures/checkout/checkout.py must be unchanged (pre-migration).
        """
        original_content = (CHECKOUT / "checkout.py").read_text()
        dest = tmp_path / "checkout"
        _make_worktree(CHECKOUT, dest)
        migrate_run({**CR, "consumer": "checkout"}, dest)
        after_content = (CHECKOUT / "checkout.py").read_text()
        assert original_content == after_content, (
            "Real fixtures/checkout/checkout.py was mutated! "
            "The test must only operate on a tmp_path copy."
        )


class TestFraudState:
    """fraud must use account_id exclusively (full migration)."""

    def test_fraud_py_uses_account_id(self, tmp_path: Path):
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)
        migrate_run({**CR, "consumer": "fraud"}, dest)
        content = (dest / "fraud.py").read_text()
        assert "account_id" in content, "fraud.py must use account_id after migration"

    def test_fraud_py_no_customer_id_key(self, tmp_path: Path):
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)
        migrate_run({**CR, "consumer": "fraud"}, dest)
        content = (dest / "fraud.py").read_text()
        assert '"customer_id"' not in content, (
            'fraud.py must not use "customer_id" key after migration'
        )

    def test_original_fixture_not_mutated(self, tmp_path: Path):
        """
        After running consumer-migration against a tmp_path copy, the real
        fixtures/fraud/fraud.py must be unchanged (pre-migration).
        """
        original_content = (FRAUD / "fraud.py").read_text()
        dest = tmp_path / "fraud"
        _make_worktree(FRAUD, dest)
        migrate_run({**CR, "consumer": "fraud"}, dest)
        after_content = (FRAUD / "fraud.py").read_text()
        assert original_content == after_content, (
            "Real fixtures/fraud/fraud.py was mutated! "
            "The test must only operate on a tmp_path copy."
        )


class TestAnalyticsWorkerState:
    """
    analytics-worker must use event["account_id"] — the canonical undocumented pattern.
    Full cutover: event["customer_id"] must be absent.
    """

    def test_worker_py_uses_event_account_id(self, tmp_path: Path):
        dest = tmp_path / "analytics-worker"
        _make_worktree(ANALYTICS, dest)
        migrate_run({**CR, "consumer": "analytics-worker"}, dest)
        content = (dest / "worker.py").read_text()
        assert 'event["account_id"]' in content, (
            'worker.py must use event["account_id"] after migration'
        )

    def test_worker_py_no_event_customer_id(self, tmp_path: Path):
        dest = tmp_path / "analytics-worker"
        _make_worktree(ANALYTICS, dest)
        migrate_run({**CR, "consumer": "analytics-worker"}, dest)
        content = (dest / "worker.py").read_text()
        assert 'event["customer_id"]' not in content, (
            'event["customer_id"] must be gone from worker.py after migration'
        )

    def test_original_fixture_not_mutated(self, tmp_path: Path):
        """
        After running consumer-migration against a tmp_path copy, the real
        fixtures/analytics-worker/worker.py must be unchanged (pre-migration,
        containing event["customer_id"]).
        """
        original_content = (ANALYTICS / "worker.py").read_text()
        dest = tmp_path / "analytics-worker"
        _make_worktree(ANALYTICS, dest)
        migrate_run({**CR, "consumer": "analytics-worker"}, dest)
        after_content = (ANALYTICS / "worker.py").read_text()
        assert original_content == after_content, (
            "Real fixtures/analytics-worker/worker.py was mutated! "
            "The test must only operate on a tmp_path copy."
        )
        # Also confirm the real fixture still has the pre-migration pattern
        assert 'event["customer_id"]' in after_content, (
            "Real worker.py must still contain event[\"customer_id\"] (pre-migration fixture)"
        )


# ===========================================================================
# B. Worktree integration tests — reproducible end-to-end agent invocations
# Uses tmp_path copies of the PRE-MIGRATION fixture baseline so the tests
# are idempotent.
# ===========================================================================

def _revert_to_baseline(src_file_content: str, dest_file: Path) -> None:
    """Write pre-migration content back into a file in the worktree."""
    dest_file.write_text(src_file_content, encoding="utf-8")


@pytest.mark.integration
class TestProviderPatchWorktreeIntegration:
    """Run provider-patch against a fresh copy of the account-service fixture."""

    def test_provider_patch_on_fixture_structure(self, tmp_path: Path):
        """
        provider-patch must succeed when run against a directory that matches
        the real fixtures/account-service/ structure.
        """
        dest = tmp_path / "account-service"

        # Build a fresh pre-migration baseline matching the fixture structure
        import textwrap
        dest.mkdir()
        subprocess.run(["git", "init"], capture_output=True, cwd=str(dest))
        subprocess.run(
            ["git", "config", "user.email", "test@interlock.dev"],
            capture_output=True, cwd=str(dest),
        )
        subprocess.run(
            ["git", "config", "user.name", "Interlock Test"],
            capture_output=True, cwd=str(dest),
        )

        (dest / "conftest.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        )
        (dest / "app.py").write_text(textwrap.dedent("""\
            from typing import Optional

            class AccountResponse:
                customer_id: Optional[str] = None

                def __init__(self, customer_id: str):
                    self.customer_id = customer_id

                def to_dict(self) -> dict:
                    return {
                        "customer_id": self.customer_id,
                    }

            def get_account(customer_id: str) -> dict:
                return AccountResponse(customer_id=customer_id).to_dict()
        """))
        (dest / "openapi.yaml").write_text(textwrap.dedent("""\
            openapi: "3.0.0"
            info:
              title: Account Service
              version: "1.0"
            paths:
              /accounts/{id}:
                get:
                  responses:
                    "200":
                      content:
                        application/json:
                          schema:
                            type: object
                            properties:
                              customer_id:
                                type: string
        """))
        tests_dir = dest / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_app.py").write_text(textwrap.dedent("""\
            from app import get_account
            def test_get_account():
                r = get_account("c-1")
                assert "customer_id" in r
        """))

        subprocess.run(["git", "add", "."], capture_output=True, cwd=str(dest))
        subprocess.run(
            ["git", "commit", "-m", "baseline"],
            capture_output=True, cwd=str(dest),
        )

        result = patch_run(CR, dest)

        assert result["status"] == "success"
        assert _is_sha(result["commit_sha"])
        assert "account_id" in (dest / "app.py").read_text()
        assert "customer_id" in (dest / "app.py").read_text()  # retained
        assert "account_id" in (dest / "openapi.yaml").read_text()

    def test_provider_patch_commit_sha_verified_in_worktree(self, tmp_path: Path):
        """SHA returned by provider-patch must exist as a git object in the worktree."""
        import textwrap
        dest = tmp_path / "acct-svc"
        dest.mkdir()
        subprocess.run(["git", "init"], capture_output=True, cwd=str(dest))
        subprocess.run(
            ["git", "config", "user.email", "t@t.dev"], capture_output=True, cwd=str(dest)
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], capture_output=True, cwd=str(dest)
        )
        (dest / "conftest.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        )
        (dest / "app.py").write_text(
            "class M:\n    customer_id: str = ''\ndef get(c): return {'customer_id': c}\n"
        )
        tests = dest / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("")
        (tests / "test_m.py").write_text(
            "from app import get\ndef test_m(): assert 'customer_id' in get('x')\n"
        )
        subprocess.run(["git", "add", "."], capture_output=True, cwd=str(dest))
        subprocess.run(["git", "commit", "-m", "b"], capture_output=True, cwd=str(dest))

        result = patch_run(CR, dest)
        sha = result["commit_sha"]
        verify = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", sha], capture_output=True
        )
        assert verify.returncode == 0, f"SHA {sha} does not exist in worktree"


@pytest.mark.integration
class TestConsumerMigrationWorktreeIntegration:
    """Run consumer-migration against fresh copies matching the real fixture structure."""

    def _checkout_baseline(self, dest: Path) -> None:
        import textwrap
        dest.mkdir()
        subprocess.run(["git", "init"], capture_output=True, cwd=str(dest))
        subprocess.run(
            ["git", "config", "user.email", "t@t.dev"], capture_output=True, cwd=str(dest)
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], capture_output=True, cwd=str(dest)
        )
        (dest / "conftest.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        )
        (dest / "checkout.py").write_text(textwrap.dedent("""\
            def process_order(resp: dict, item: str) -> dict:
                cid = resp["customer_id"]
                return {"order_customer": cid, "item": item, "status": "pending"}
        """))
        tests = dest / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("")
        (tests / "test_checkout.py").write_text(textwrap.dedent("""\
            from checkout import process_order
            def test_order():
                r = process_order({"customer_id": "c-1"}, "widget")
                assert r["order_customer"] == "c-1"
        """))
        subprocess.run(["git", "add", "."], capture_output=True, cwd=str(dest))
        subprocess.run(["git", "commit", "-m", "baseline"], capture_output=True, cwd=str(dest))

    def _analytics_baseline(self, dest: Path) -> None:
        import textwrap
        dest.mkdir()
        subprocess.run(["git", "init"], capture_output=True, cwd=str(dest))
        subprocess.run(
            ["git", "config", "user.email", "t@t.dev"], capture_output=True, cwd=str(dest)
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], capture_output=True, cwd=str(dest)
        )
        (dest / "conftest.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        )
        (dest / "worker.py").write_text(textwrap.dedent("""\
            def process_event(event: dict) -> dict:
                cid = event["customer_id"]
                return {"processed_for": cid, "metadata": {"customer_id": cid}}
        """))
        tests = dest / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("")
        (tests / "test_worker.py").write_text(textwrap.dedent("""\
            from worker import process_event
            def test_event():
                r = process_event({"customer_id": "c-1", "type": "buy"})
                assert r["processed_for"] == "c-1"
        """))
        subprocess.run(["git", "add", "."], capture_output=True, cwd=str(dest))
        subprocess.run(["git", "commit", "-m", "baseline"], capture_output=True, cwd=str(dest))

    def test_checkout_migration_on_fixture_structure(self, tmp_path: Path):
        dest = tmp_path / "checkout"
        self._checkout_baseline(dest)

        result = migrate_run({**CR, "consumer": "checkout"}, dest)

        assert result["status"] == "success"
        assert _is_sha(result["commit_sha"])
        src = (dest / "checkout.py").read_text()
        assert '"account_id"' in src
        assert '"customer_id"' not in src

    def test_analytics_event_key_replaced_in_fixture_structure(self, tmp_path: Path):
        """
        The canonical discovery-demo migration: event['customer_id']
        must become event['account_id'] in a real fixture-structured repo.
        """
        dest = tmp_path / "analytics-worker"
        self._analytics_baseline(dest)

        result = migrate_run({**CR, "consumer": "analytics-worker"}, dest)

        assert result["status"] == "success"
        src = (dest / "worker.py").read_text()
        assert 'event["account_id"]' in src
        assert 'event["customer_id"]' not in src

    def test_checkout_and_analytics_distinct_shas(self, tmp_path: Path):
        dest_c = tmp_path / "checkout"
        dest_a = tmp_path / "analytics-worker"
        self._checkout_baseline(dest_c)
        self._analytics_baseline(dest_a)

        r_c = migrate_run({**CR, "consumer": "checkout"}, dest_c)
        r_a = migrate_run({**CR, "consumer": "analytics-worker"}, dest_a)

        assert r_c["commit_sha"] != r_a["commit_sha"]

    def test_migration_scoped_to_fixture_path(self, tmp_path: Path):
        """Migrating checkout must not touch analytics-worker, even if both are under tmp_path."""
        dest_c = tmp_path / "checkout"
        dest_a = tmp_path / "analytics-worker"
        self._checkout_baseline(dest_c)
        self._analytics_baseline(dest_a)

        analytics_before = (dest_a / "worker.py").read_text()
        migrate_run({**CR, "consumer": "checkout"}, dest_c)
        analytics_after = (dest_a / "worker.py").read_text()

        assert analytics_before == analytics_after, (
            "Migrating checkout must not modify analytics-worker/worker.py"
        )
