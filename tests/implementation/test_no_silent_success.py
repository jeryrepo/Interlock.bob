"""
Regression tests for the invariant the whole gate rests on:

    an implementation agent that changed no source file MUST NOT report success.

Why this file exists
--------------------
Every one of the behaviours pinned here was, at one point, a silent lie that the
full test suite was completely green for. `status` was set to "success" whenever
pytest passed, entirely decoupled from whether the agent had changed anything.
Three separate false VERIFIED verdicts followed from that single decision:

1. A schema-only component (a README and a schema.sql) had no Python for
   consumer-migration to rewrite. The agent auto-generated a test asserting a
   dict literal it had just written, pytest passed, it committed, and the gate
   counted the component as migrated. The schema was never touched.

2. `deliver_via_webhook` is a function name, and provider-patch matches
   field-shaped symbols only. It matched nothing in the transport fixture,
   changed nothing, and still reported success — so the gate said a pub/sub
   migration was safe while the publisher had never gained `deliver_via_pubsub`.

3. Both of the above passed a gate that could not see the coexistence rehearsal
   at all, because the rehearsal wrote evidence and no work item.

These tests assert the honest outcome directly. They are cheap, and they are the
only thing standing between "proved safe" and "reassured".
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from agents.implementation import consumer_migration, provider_patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _repo(path: Path) -> Path:
    """A committed git repository at *path*."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["add", "."], path)
    _git(["commit", "-m", "baseline"], path)
    return path


CHANGE_REQUEST = {
    "id": "c1",
    "old_field": "customer_id",
    "new_field": "account_id",
    "provider": "account-service",
}


# ---------------------------------------------------------------------------
# consumer-migration
# ---------------------------------------------------------------------------

class TestConsumerMigrationRefusesEmptySuccess:
    def test_a_component_with_nothing_to_migrate_reports_failed(self, tmp_path):
        """
        A component this agent cannot read is unproven, not safe.

        The README mentions the field, so discovery would legitimately flag this
        component as a dependant — but there is no .py or .sql for the agent to
        change, which is exactly the case that used to be reported migrated.
        """
        repo = tmp_path / "docs-only"
        repo.mkdir()
        (repo / "README.md").write_text(
            "This service keys everything on customer_id.\n", encoding="utf-8"
        )
        _repo(repo)

        result = consumer_migration.run(
            {"consumer": "docs-only", "change_request": CHANGE_REQUEST}, repo
        )

        assert result["status"] == "failed"
        assert result["files_changed"] == []
        assert result["commit_sha"] is None
        assert result["evidence"][0]["claim_type"] == "risk"
        assert (
            result["evidence"][0]["content"]["risk"] == "migration_changed_nothing"
        )

    def test_it_does_not_write_a_self_satisfying_test(self, tmp_path):
        """
        The old failure mode was not just a wrong status — it was a test that
        asserted a dict the agent had written moments earlier, so it passed
        whether or not the component was migrated. Nothing should be created.
        """
        repo = tmp_path / "docs-only"
        repo.mkdir()
        (repo / "README.md").write_text("customer_id\n", encoding="utf-8")
        _repo(repo)

        consumer_migration.run(
            {"consumer": "docs-only", "change_request": CHANGE_REQUEST}, repo
        )

        assert not (repo / "tests").exists()


class TestConsumerMigrationHandlesSql:
    """A column rename that never reaches the schema is not a column rename."""

    SCHEMA = textwrap.dedent(
        """\
        CREATE TABLE IF NOT EXISTS accounts (
            customer_id  TEXT PRIMARY KEY,
            email        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id     TEXT PRIMARY KEY,
            customer_id  TEXT NOT NULL REFERENCES accounts(customer_id),
            item         TEXT NOT NULL
        );
        """
    )

    def _schema_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "platform-config"
        repo.mkdir()
        (repo / "schema.sql").write_text(self.SCHEMA, encoding="utf-8")
        return _repo(repo)

    def test_the_schema_is_actually_migrated(self, tmp_path):
        repo = self._schema_repo(tmp_path)

        result = consumer_migration.run(
            {"consumer": "platform-config", "change_request": CHANGE_REQUEST}, repo
        )

        assert result["status"] == "success"
        assert "schema.sql" in result["files_changed"]

        schema = (repo / "schema.sql").read_text(encoding="utf-8")
        for table in ("accounts", "orders"):
            assert f"ALTER TABLE {table} ADD COLUMN account_id TEXT;" in schema
            assert f"UPDATE {table} SET account_id = customer_id;" in schema

    def test_the_old_column_survives_the_coexistence_window(self, tmp_path):
        """
        Additive, not a rewrite. Replacing the column in place would break every
        existing reader the instant the schema was applied — the coordinated
        big-bang deploy this project exists to avoid.
        """
        repo = self._schema_repo(tmp_path)
        consumer_migration.run(
            {"consumer": "platform-config", "change_request": CHANGE_REQUEST}, repo
        )

        schema = (repo / "schema.sql").read_text(encoding="utf-8")
        assert "customer_id  TEXT PRIMARY KEY" in schema

    def test_rerunning_does_not_duplicate_the_migration(self, tmp_path):
        """Idempotent: the workflow calls agents again after an approval."""
        repo = self._schema_repo(tmp_path)
        consumer_migration.run(
            {"consumer": "platform-config", "change_request": CHANGE_REQUEST}, repo
        )
        first = (repo / "schema.sql").read_text(encoding="utf-8")

        consumer_migration.run(
            {"consumer": "platform-config", "change_request": CHANGE_REQUEST}, repo
        )
        assert (repo / "schema.sql").read_text(encoding="utf-8") == first
        assert first.count("ALTER TABLE accounts ADD COLUMN account_id") == 1

    def test_a_foreign_key_reference_does_not_trigger_a_spurious_alter(self, tmp_path):
        """
        `REFERENCES accounts(customer_id)` names a column on another table.
        Only tables that DECLARE the column should gain the new one.
        """
        repo = tmp_path / "fk-only"
        repo.mkdir()
        (repo / "schema.sql").write_text(
            textwrap.dedent(
                """\
                CREATE TABLE IF NOT EXISTS notes (
                    note_id  TEXT PRIMARY KEY,
                    owner    TEXT NOT NULL REFERENCES accounts(customer_id)
                );
                """
            ),
            encoding="utf-8",
        )
        _repo(repo)

        result = consumer_migration.run(
            {"consumer": "fk-only", "change_request": CHANGE_REQUEST}, repo
        )

        assert result["status"] == "failed"
        assert "ALTER TABLE notes" not in (repo / "schema.sql").read_text(
            encoding="utf-8"
        )

    def test_the_generated_test_fails_if_the_schema_is_reverted(self, tmp_path):
        """
        The strongest claim in this file: the test the agent generates actually
        constrains the artifact. Revert the schema and the generated test must
        go red — the old generated stub would have stayed green.
        """
        repo = self._schema_repo(tmp_path)
        consumer_migration.run(
            {"consumer": "platform-config", "change_request": CHANGE_REQUEST}, repo
        )

        (repo / "schema.sql").write_text(self.SCHEMA, encoding="utf-8")

        completed = subprocess.run(
            ["python", "-m", "pytest", str(repo / "tests"), "-q"],
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0, completed.stdout


# ---------------------------------------------------------------------------
# provider-patch
# ---------------------------------------------------------------------------

class TestProviderPatchRefusesEmptySuccess:
    def test_a_function_shaped_symbol_reports_failed(self, tmp_path):
        """
        The transport bug, pinned. provider-patch matches field-shaped symbols;
        `deliver_via_webhook` is a function name and matches none of them. The
        agent must say so rather than commit and report success.
        """
        repo = tmp_path / "event-publisher"
        repo.mkdir()
        (repo / "publisher.py").write_text(
            textwrap.dedent(
                """\
                def deliver_via_webhook(event):
                    return {"transport": "webhook", "event": event}
                """
            ),
            encoding="utf-8",
        )
        _repo(repo)

        result = provider_patch.run(
            {
                "change_request": {
                    "id": "c1",
                    "old_field": "deliver_via_webhook",
                    "new_field": "deliver_via_pubsub",
                    "provider": "event-publisher",
                }
            },
            repo,
        )

        assert result["status"] == "failed"
        assert result["files_changed"] == []
        assert result["commit_sha"] is None
        assert (
            result["evidence"][0]["content"]["risk"]
            == "provider_patch_changed_nothing"
        )

    def test_the_provider_source_is_left_untouched(self, tmp_path):
        repo = tmp_path / "event-publisher"
        repo.mkdir()
        source = "def deliver_via_webhook(event):\n    return event\n"
        (repo / "publisher.py").write_text(source, encoding="utf-8")
        _repo(repo)

        provider_patch.run(
            {
                "change_request": {
                    "id": "c1",
                    "old_field": "deliver_via_webhook",
                    "new_field": "deliver_via_pubsub",
                    "provider": "event-publisher",
                }
            },
            repo,
        )

        assert (repo / "publisher.py").read_text(encoding="utf-8") == source
