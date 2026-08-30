"""
tests/cli/test_cli_and_mcp.py
==============================
Tests for the two agent-facing surfaces: the `interlock` CLI and the MCP server.

Both are thin shells over `interlock_cli.core`, so most of these assert the
things a shell can still get wrong — exit codes, argument validation, and above
all that neither surface can talk its way past the deterministic gate.

The end-to-end cases are marked `integration`: they copy the fixture tree, run
real pytest and make real git commits inside a throwaway workspace. No network,
no Docker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import orchestrator.gate as gate
import orchestrator.ledger as ledger
from interlock_cli import core
from interlock_cli.cli import app

runner = CliRunner()

RENAME_ARGS = [
    "--old", "customer_id",
    "--new", "account_id",
    "--provider", "account-service",
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolate the agent workspace and the ledger for every test."""
    monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    return tmp_path


@pytest.fixture
def db(workspace) -> str:
    return str(workspace / "ledger.db")


# ---------------------------------------------------------------------------
# Spec construction
# ---------------------------------------------------------------------------

class TestBuildSpec:
    def test_field_rename(self):
        spec = core.build_spec("field_rename", "svc", "a", "b", "fixtures")
        assert spec["kind"] == "field_rename"
        assert spec["old_field"] == "a" and spec["new_field"] == "b"

    def test_transport_migration_maps_symbols(self):
        spec = core.build_spec(
            "transport_migration", "svc", "send_webhook", "publish",
            "fixtures", topic="acct.events", webhook_path="/hooks",
        )
        assert spec["old_symbol"] == "send_webhook"
        assert spec["topic"] == "acct.events"

    def test_unknown_kind_is_rejected_immediately(self):
        """A typo must fail here, not as a mysteriously empty registry later."""
        with pytest.raises(Exception):
            core.build_spec("nonsense", "svc", "a", "b", "fixtures")


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestCliSurface:
    def test_help_lists_every_command(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in ("check", "start", "approve", "gate", "status", "list", "evidence"):
            assert command in result.stdout

    def test_bad_kind_exits_with_the_error_code(self, db):
        result = runner.invoke(app, ["check", *RENAME_ARGS, "--kind", "nope", "--db", db])
        assert result.exit_code == core.EXIT_ERROR

    def test_status_of_unknown_change_exits_with_error(self, db):
        result = runner.invoke(app, ["status", "no-such-change", "--db", db])
        assert result.exit_code == core.EXIT_ERROR

    def test_list_on_an_empty_ledger_succeeds(self, db):
        assert runner.invoke(app, ["list", "--db", db]).exit_code == 0


class TestGateExitCodes:
    """
    The exit code is the product. Without it `interlock check` is a report
    nobody reads instead of a blocking CI check.
    """

    def test_unverified_change_exits_one(self, db):
        conn = core.open_ledger(db)
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", {
            "kind": "field_rename", "provider": "account-service",
            "components_root": "fixtures",
            "old_field": "customer_id", "new_field": "account_id",
        })
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        conn.close()

        result = runner.invoke(app, ["gate", "c1", "--db", db])
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE

    def test_verified_change_exits_zero(self, db):
        conn = core.open_ledger(db)
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", {
            "kind": "field_rename", "provider": "account-service",
            "components_root": "fixtures",
            "old_field": "customer_id", "new_field": "account_id",
        })
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        ledger.upsert_work_item(conn, "c1", "checkout", "verified", "migrate")
        ledger.upsert_work_item(conn, "c1", "account-service", "verified", "provider_patch")
        ledger.upsert_work_item(
            conn, "c1", "account-service", "verified", gate.REHEARSAL_STEP_KIND
        )
        conn.close()

        assert runner.invoke(app, ["gate", "c1", "--db", db]).exit_code == core.EXIT_OK


class TestHumanGateCannotBeBypassed:
    """AGENTS.md invariant 1: no surface may approve past an unverified gate."""

    def test_legacy_removal_is_refused_while_unverified(self, db):
        conn = core.open_ledger(db)
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", {
            "kind": "field_rename", "provider": "account-service",
            "components_root": "fixtures",
            "old_field": "customer_id", "new_field": "account_id",
        })
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)

        with pytest.raises(PermissionError):
            core.approve(conn, "c1", "legacy_removal", "someone")

    def test_cli_reports_the_refusal_with_a_nonzero_exit(self, db):
        conn = core.open_ledger(db)
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", {
            "kind": "field_rename", "provider": "account-service",
            "components_root": "fixtures",
            "old_field": "customer_id", "new_field": "account_id",
        })
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        conn.close()

        result = runner.invoke(
            app, ["approve", "c1", "--gate", "legacy_removal", "--db", db]
        )
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------

def _unwrap(result) -> object:
    return json.loads(result.content[0].text)


class TestMcpServer:
    def test_expected_tools_are_registered(self):
        from interlock_mcp.server import mcp

        names = {t.name for t in asyncio.run(mcp.list_tools())}
        assert {
            "interlock_check", "interlock_start", "interlock_gate",
            "interlock_status", "interlock_evidence",
            "interlock_dependency_graph", "interlock_list_changes",
        } <= names

    def test_no_tool_can_influence_the_verdict(self):
        """
        An agent may ask for the verdict but never change it. In particular
        there is no legacy-removal approval and no override.
        """
        from interlock_mcp.server import mcp

        names = {t.name for t in asyncio.run(mcp.list_tools())}
        forbidden = ("override", "legacy_removal", "set_verified", "force", "bypass")
        offenders = [n for n in names for k in forbidden if k in n]
        assert offenders == [], f"gate-influencing tools exposed: {offenders}"

    def test_every_tool_documents_itself(self):
        """Descriptions are the whole interface an agent sees."""
        from interlock_mcp.server import mcp

        for tool in asyncio.run(mcp.list_tools()):
            assert tool.description and len(tool.description.strip()) > 40, tool.name

    def test_gate_tool_reads_the_ledger(self, db):
        from interlock_mcp.server import mcp

        conn = core.open_ledger(db)
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", {
            "kind": "field_rename", "provider": "account-service",
            "components_root": "fixtures",
            "old_field": "customer_id", "new_field": "account_id",
        })
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        conn.close()

        out = _unwrap(asyncio.run(
            mcp.call_tool("interlock_gate", {"change_id": "c1", "db_path": db})
        ))
        assert out["result"] == "NOT_PROVEN_SAFE"
        assert "checkout" in out["unresolved"]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestEndToEndSurfaces:
    def test_cli_check_drives_real_agents_to_verified(self, db):
        result = runner.invoke(app, ["check", *RENAME_ARGS, "--db", db, "--json"])
        assert result.exit_code == core.EXIT_OK, result.stdout
        payload = json.loads(result.stdout)
        assert payload["gate"]["result"] == "VERIFIED"
        assert payload["kind"] == "field_rename"

    def test_cli_check_discovers_the_undocumented_consumer(self, db):
        result = runner.invoke(app, ["check", *RENAME_ARGS, "--db", db, "--json"])
        components = {w["component"] for w in json.loads(result.stdout)["gate"]["work_items"]}
        assert "analytics-worker" in components

    def test_mcp_check_returns_a_real_verdict(self, db):
        from interlock_mcp.server import mcp

        out = _unwrap(asyncio.run(mcp.call_tool("interlock_check", {
            "old_symbol": "customer_id", "new_symbol": "account_id",
            "provider": "account-service", "db_path": db,
        })))
        assert out["gate"]["result"] == "VERIFIED"

    def test_a_missing_provider_is_not_proven_safe(self, db):
        """The provider patch cannot succeed, so the gate must block."""
        result = runner.invoke(app, [
            "check", "--old", "customer_id", "--new", "account_id",
            "--provider", "no-such-service", "--db", db, "--json",
        ])
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE
