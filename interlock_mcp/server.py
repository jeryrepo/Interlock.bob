"""
interlock_mcp/server.py
========================
Interlock as a set of MCP tools.

This is the mechanism behind "pull this repo into IBM Bob and it just works".
Bob reads MCP server configuration from `.bob/mcp.json` (project) or
`~/.bob/mcp.json` (global); Claude Code, Cursor and Copilot read `.mcp.json`.
Both files ship in this repository pointing at this module over stdio, so an
agent that clones the repo can call Interlock instead of reimplementing the
checks by hand.

Every tool is a thin wrapper over `interlock_cli.core`, which is also what the
CLI and the GitHub Action call. One implementation, three surfaces, no drift.

The design rule that matters here: **an agent can ask for the verdict but
cannot influence it.** There is no tool to override the gate, mark a consumer
verified, or approve legacy removal. `interlock_approve_coordination` exists
because coordination is a planning checkpoint; legacy removal is deliberately
absent, because that is the approval that authorises destroying the old field
and it stays with a human (AGENTS.md invariants 1 and 4).
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer  # MCP SDK 2.x (was FastMCP in 1.x)

from interlock_cli import core

mcp = MCPServer("interlock")

_DEFAULT_DB = os.environ.get("INTERLOCK_DB_PATH", "interlock.db")
_DEFAULT_ROOT = os.environ.get("INTERLOCK_COMPONENTS_ROOT", "fixtures")


def _render(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


@mcp.tool()
def interlock_check(
    old_symbol: str,
    new_symbol: str,
    provider: str,
    kind: str = "field_rename",
    components_root: str = _DEFAULT_ROOT,
    db_path: str = _DEFAULT_DB,
) -> str:
    """
    Run a breaking change through Interlock and return the deterministic verdict.

    Use this BEFORE opening a pull request that renames or removes a field other
    services may depend on. It discovers every consumer — including ones absent
    from any published contract — migrates them on an isolated copy of the
    component tree, runs their real test suites, and returns VERIFIED or
    NOT_PROVEN_SAFE with the specific consumers that are unproven.

    A NOT_PROVEN_SAFE result means the change is not safe to ship as-is. Report
    the unresolved consumers to the user rather than proceeding.

    Args:
        old_symbol: the field or symbol being replaced, e.g. "customer_id"
        new_symbol: its replacement, e.g. "account_id"
        provider: the component that owns it, e.g. "account-service"
        kind: field_rename | api_contract_change | transport_migration
        components_root: directory whose subdirectories are the components
        db_path: SQLite ledger path
    """
    spec = core.build_spec(kind, provider, old_symbol, new_symbol, components_root)
    conn = core.open_ledger(db_path)
    return _render(core.check(conn, f"{old_symbol} -> {new_symbol}", spec))


@mcp.tool()
def interlock_start(
    old_symbol: str,
    new_symbol: str,
    provider: str,
    kind: str = "field_rename",
    components_root: str = _DEFAULT_ROOT,
    db_path: str = _DEFAULT_DB,
) -> str:
    """
    Start a change and run discovery and planning only, stopping before any code
    is modified.

    Use this to see which consumers a change would affect, and in what order
    they would have to migrate, without touching anything. The change stops at
    the COORDINATE gate awaiting approval.
    """
    spec = core.build_spec(kind, provider, old_symbol, new_symbol, components_root)
    conn = core.open_ledger(db_path)
    return _render(core.start(conn, f"{old_symbol} -> {new_symbol}", spec))


@mcp.tool()
def interlock_approve_coordination(
    change_id: str,
    approved_by: str = "mcp-agent",
    db_path: str = _DEFAULT_DB,
) -> str:
    """
    Approve the migration plan for a change and run the implementation phases.

    This is the planning checkpoint only. It does NOT authorise removing the
    legacy field — that approval is reserved for a human and is not available
    through this server.
    """
    conn = core.open_ledger(db_path)
    return _render(core.approve(conn, change_id, "coordinate", approved_by))


@mcp.tool()
def interlock_gate(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """
    Return the deterministic safety verdict for a change.

    The verdict is computed by pure Python with no model involvement and cannot
    be overridden — not by this server, not by any agent. Read it; do not argue
    with it.
    """
    conn = core.open_ledger(db_path)
    return _render(core.gate_status(conn, change_id))


@mcp.tool()
def interlock_status(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """Return a change's workflow state, kind and current gate verdict."""
    conn = core.open_ledger(db_path)
    return _render(core.status(conn, change_id))


@mcp.tool()
def interlock_evidence(
    change_id: str,
    claim_type: str | None = None,
    db_path: str = _DEFAULT_DB,
) -> str:
    """
    Return the evidence ledger for a change.

    Every claim carries a source reference and, where the claim is about code
    that changed, a real git commit SHA. Use this to explain *why* a verdict is
    what it is.

    Args:
        claim_type: optionally filter to dependency | migration_status |
            test_result | risk. "risk" is the useful one for diagnosing a
            NOT_PROVEN_SAFE verdict.
    """
    conn = core.open_ledger(db_path)
    items = core.evidence(conn, change_id)
    if claim_type:
        items = [e for e in items if e["claim_type"] == claim_type]
    return _render(items)


@mcp.tool()
def interlock_dependency_graph(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """
    Return the discovered dependency graph as nodes and typed edges.

    Edge types are api, event, db, or undocumented. An `undocumented` edge is a
    consumer found only by reading source — the kind that breaks production
    because nobody knew it existed.
    """
    conn = core.open_ledger(db_path)
    return _render(core.graph(conn, change_id))


@mcp.tool()
def interlock_review(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """
    Render a pull-request review for a change, as markdown.

    Use this when the user is about to open a PR for a breaking change, or asks
    what reviewers will see. The output is the same body the Interlock GitHub
    Action posts, so what you show them locally is what will appear on the PR.

    It names the blocking components and lists consumers that appear in no
    published contract — the ones found only by reading source.
    """
    from interlock_cli import review as review_mod

    conn = core.open_ledger(db_path)
    status = core.status(conn, change_id)
    graph = core.graph(conn, change_id)
    risks = [e for e in core.evidence(conn, change_id) if e["claim_type"] == "risk"]
    return review_mod.render_markdown(status, graph, risks)


@mcp.tool()
def interlock_orchestration_map(db_path: str = _DEFAULT_DB) -> str:
    """
    Describe how Interlock is wired: which agents run for which change kind, in
    which phase, and what each proves.

    Use this to answer questions about what Interlock will actually do for a
    given kind of change before running it.
    """
    from orchestrator.agent_registry import AGENT_REGISTRY
    from orchestrator.gate import _DEFAULT_STEP_KINDS, _REQUIRED_PROVIDER_STEPS, _REQUIRED_STEP_KINDS
    from orchestrator.schemas import CHANGE_KINDS

    phases = ["DISCOVERY", "PLANNING", "MODIFY", "REHEARSE", "VERIFY"]
    return _render({
        kind: {
            "gate_requires": {
                "provider": list(_REQUIRED_PROVIDER_STEPS.get(kind, ())),
                "per_consumer": list(_REQUIRED_STEP_KINDS.get(kind, _DEFAULT_STEP_KINDS)),
            },
            "phases": {
                phase: [a.role for a in AGENT_REGISTRY.get((kind, phase), ())]
                for phase in phases
            },
        }
        for kind in CHANGE_KINDS
    })


@mcp.tool()
def interlock_list_changes(db_path: str = _DEFAULT_DB) -> str:
    """List known changes in this ledger, newest first."""
    conn = core.open_ledger(db_path)
    return _render(core.changes(conn))


def main() -> None:
    """Entry point for `interlock-mcp`, declared in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
