"""
interlock_mcp/server.py
========================
Interlock as a set of MCP tools.

This is the mechanism behind "pull this repo into IBM Bob and it just works".
Bob reads MCP server configuration from `.bob/mcp.json` (project) or
`~/.bob/settings/mcp.json` (global — note the `settings/`; a `~/.bob/mcp.json`
is silently ignored, verified against the Bob 2.0 application bundle);
Claude Code, Cursor and Copilot read `.mcp.json`. `interlock init` writes
either scope for another repository.
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
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer  # MCP SDK 2.x (was FastMCP in 1.x)

from interlock_cli import core

mcp = MCPServer("interlock")

_DEFAULT_DB = os.environ.get("INTERLOCK_DB_PATH", "interlock.db")
_DEFAULT_ROOT = os.environ.get("INTERLOCK_COMPONENTS_ROOT", "fixtures")


def _render(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


@contextmanager
def _ledger(db_path: str):
    """
    Open a ledger for one tool call and close it afterwards.

    An MCP server is long-lived: a client starts it once and keeps it running
    for the whole session. Leaking a SQLite handle per tool call therefore
    accumulates indefinitely, and on Windows each open handle blocks deletion of
    the file it points at — which is how an editor-spawned server left a test
    directory permanently locked.
    """
    conn = core.open_ledger(db_path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing must never mask a tool error
            pass


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
    with _ledger(db_path) as conn:
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
    with _ledger(db_path) as conn:
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
    with _ledger(db_path) as conn:
        return _render(core.approve(conn, change_id, "coordinate", approved_by))


@mcp.tool()
def interlock_gate(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """
    Return the deterministic safety verdict for a change.

    The verdict is computed by pure Python with no model involvement and cannot
    be overridden — not by this server, not by any agent. Read it; do not argue
    with it.
    """
    with _ledger(db_path) as conn:
        return _render(core.gate_status(conn, change_id))


@mcp.tool()
def interlock_status(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """Return a change's workflow state, kind and current gate verdict."""
    with _ledger(db_path) as conn:
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
    with _ledger(db_path) as conn:
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
    with _ledger(db_path) as conn:
        return _render(core.graph(conn, change_id))


@mcp.tool()
def interlock_discover(
    old_symbol: str,
    provider: str,
    new_symbol: str = "",
    kind: str = "field_rename",
    components_root: str = _DEFAULT_ROOT,
) -> str:
    """
    Report what Interlock sees in this repository, without changing anything.

    Run this BEFORE interlock_check when working in an unfamiliar codebase, or
    whenever the user asks which services depend on something. It reads source
    only: no workspace copy, no git, no tests executed, no ledger written, so it
    answers in seconds where a full check takes minutes.

    Returns the components found, the dependency edges, and - most usefully -
    which consumers appear in NO api contract. Those couple through events or a
    shared database schema, so no contract review would surface them; they are
    the ones that break production.

    Also reports each component's detected toolchain and whether it needs an
    `interlock.toml`. A component without one is tested with `python -m pytest`,
    which on a Go or Java service proves nothing.

    An empty `edges` list means nothing outside the provider references that
    symbol - check the spelling, or `components_root`, before concluding the
    change is safe.
    """
    return _render(
        core.discover(components_root, provider, old_symbol, new_symbol or None, kind)
    )


@mcp.tool()
def interlock_manifest(
    components_root: str = _DEFAULT_ROOT,
    write: bool = False,
) -> str:
    """
    Propose an `interlock.toml` for each component, from its build files.

    Interlock runs each component's own test suite, and needs to be told how
    when the component is not a pytest project. This reads the markers already
    present - go.mod, package.json, pom.xml, Cargo.toml, a Makefile with a
    `test:` target - and reports the manifest each implies.

    `write=False` (the default) only previews. Pass `write=True` ONLY when the
    user has asked for the files to be created: it writes `interlock.toml` into
    each component directory. Existing manifests are never overwritten.

    Every command is a guess until it runs. Show the user what was detected and
    let them confirm it is how their tests are actually invoked.
    """
    return _render(core.manifest_plan(components_root, write=write))


@mcp.tool()
def interlock_doctor(components_root: str = _DEFAULT_ROOT) -> str:
    """
    Report whether Interlock is correctly set up here, and what is missing.

    Checks git, the ledger and workspace paths, the components root (how many
    components it sees and how many declare a manifest), and which optional IBM
    integrations are configured. Calls no model and spends no credits.

    Use it when a check behaves unexpectedly, or before running anything in a
    repository for the first time. Everything reported as optional is genuinely
    optional - the gate, the CLI and these tools work with no IBM account.
    """
    from interlock_cli.cli import _components_root_check, _mcp_server_path
    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    import shutil

    settings = _load_settings()
    health = _watsonx.health(settings.watsonx)
    return _render({
        "components_root": _components_root_check(components_root),
        "git": shutil.which("git") is not None,
        "ledger": settings.db_path,
        "workspace": settings.workspace,
        "mcp_server": str(_mcp_server_path() or ""),
        "watsonx_narration": health,
        "watsonx_orchestrate_configured": settings.orchestrate.configured,
    })


@mcp.tool()
def interlock_models() -> str:
    """
    List the watsonx.ai chat models available in the configured region.

    Needs no credentials and spends nothing - the catalogue endpoint is
    unauthenticated. Use it when narration fails with a 404, which means the
    configured WATSONX_MODEL_ID is not offered in this region. Models the
    hackathon guide places out of scope are marked `forbidden`.
    """
    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    settings = _load_settings()
    return _render({
        "configured": settings.watsonx.model_id,
        "available": _watsonx.list_chat_models(settings.watsonx),
    })


@mcp.tool()
def interlock_narrate(change_id: str, db_path: str = _DEFAULT_DB) -> str:
    """
    Explain an already-decided verdict in plain English, using watsonx.ai.

    The verdict is NOT produced here. It comes from the deterministic gate and
    is returned verbatim alongside the prose; the model is handed the finished
    decision and asked only to explain the blockers, and the gate's vocabulary
    is stripped from what it writes. If narration is switched off or
    unavailable, `narration` is null and the verdict still stands.

    Requires IBM_CLOUD_API_KEY, WATSONX_PROJECT_ID and
    INTERLOCK_ENABLE_NARRATION=1. Without them this returns the verdict and
    says why narration was skipped, rather than failing.
    """
    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    settings = _load_settings()
    with _ledger(db_path) as conn:
        gate = core.gate_status(conn, change_id)
        if not settings.watsonx.enabled:
            return _render({
                "gate": gate,
                "narration": None,
                "skipped": settings.watsonx.why_disabled(),
            })
        lines = []
        for item in core.evidence(conn, change_id):
            content = item.get("content") or {}
            detail = (
                content.get("detail") or content.get("risk") or content.get("outcome")
            )
            if detail:
                lines.append(f"{item['subject']}: {detail}")

    return _render({
        "gate": gate,
        "narration": _watsonx.narrate(gate, lines, settings.watsonx),
        "skipped": None,
    })


@mcp.tool()
def interlock_security(
    components_root: str = _DEFAULT_ROOT,
    old_symbol: str = "",
    new_symbol: str = "",
) -> str:
    """
    Report security findings in the component tree. Reads only; changes nothing.

    Checks for committed secrets and credentials, the changed symbol flowing
    into logging or authorisation code, disabled TLS verification, plaintext
    endpoints and committed credential files. With IBM credentials configured
    it also asks watsonx.ai for issues patterns cannot express - additively:
    the model can propose a finding, it can never clear one.

    IMPORTANT when reporting this to a user: an empty result means these checks
    did not fire. It is NOT a statement that the code is secure, and must not be
    presented as one. Say "no findings from these checks".

    Findings are advisory. They are recorded as evidence and appear in the PR
    review, but they never change the gate's verdict - only `interlock check
    --fail-on-security` treats them as blocking.
    """
    return _render(core.security_scan(components_root, old_symbol, new_symbol))


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

    with _ledger(db_path) as conn:
        status = core.status(conn, change_id)
        graph = core.graph(conn, change_id)
        risks = [e for e in core.evidence(conn, change_id) if e["claim_type"] == "risk"]
        security = core.security_findings(conn, change_id)
    return review_mod.render_markdown(status, graph, risks, security=security)


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
    with _ledger(db_path) as conn:
        return _render(core.changes(conn))


def main() -> None:
    """Entry point for `interlock-mcp`, declared in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
