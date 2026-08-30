"""
interlock_mcp/http.py
======================
Interlock's MCP tools over streamable-http, for watsonx Orchestrate's MCP
toolkit. Same `interlock_cli.core` underneath as the stdio server; different
transport. The stdio path (`.bob/mcp.json`, IBM Bob) is untouched.

Three things this does that stdio does not need, each found by running it
rather than by reading docs:

1. **Lifespan propagation.** `streamable_http_app()` returns a Starlette app
   whose lifespan starts the session manager, and Starlette does *not* propagate
   lifespan into mounted sub-apps. Without the parent driving it, every call
   fails with `RuntimeError: Task group is not initialized`.
   `stateless_http=True` does **not** avoid this.
2. **Transport security off.** DNS-rebinding protection auto-enables when the
   host defaults to `127.0.0.1`, and then returns **421 Invalid Host header** to
   every request — localhost included. The shared-secret middleware below
   authenticates every call, so host pinning adds nothing.
3. **Exact-path mounting.** Mounting at `/mcp` with `streamable_http_path="/"`
   makes the URL you register return a 307 redirect. Using the full path inside
   the sub-app and mounting at root means `POST /mcp` matches exactly.

Two deliberate differences from the stdio tools
-----------------------------------------------
**Narrower signatures.** The stdio tools accept `db_path` and `components_root`
because a developer on their own machine legitimately chooses them. A supervisor
LLM must not: `interlock_check` executes each component's declared test command,
so letting a model pick the directory would let it pick what gets executed.
These tools simply do not have those parameters.

**A preview is not a verdict.** When the gate has not decided, the response
carries no `result` and no `reason` — only `status: "running"`. A model that
reads `result` and ignores `decided` would otherwise present a mid-flight
snapshot as a final answer.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from interlock_cli import core
from orchestrator.settings import Settings
from orchestrator.settings import load as load_settings

http_mcp = MCPServer("interlock")

# Recorded on the approval so the ledger distinguishes a machine-driven
# coordination step from a human one.
_APPROVED_BY = "orchestrate-agent (automated coordination)"

_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def _settings() -> Settings:
    return load_settings()


def _conn(settings: Settings) -> sqlite3.Connection:
    """
    One cached connection per ledger path.

    Over stdio the process is short-lived, so opening per call was harmless.
    Behind HTTP the process is long-lived and that leaks a file handle per tool
    invocation — which on Windows also locks the file it points at.
    """
    existing = _CONNECTIONS.get(settings.db_path)
    if existing is not None:
        return existing
    conn = core.open_ledger(settings.db_path)
    _CONNECTIONS[settings.db_path] = conn
    return conn


def _render(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _gate_payload(gate: dict[str, Any]) -> dict[str, Any]:
    """
    Strip the verdict fields from an undecided gate.

    `core.gate_status()` returns a live preview with `decided: false` before the
    orchestrator records a decision. Passing that through unchanged invites a
    model to read `result` and report it as final.
    """
    if not gate.get("decided"):
        return {
            "change_id": gate.get("change_id"),
            "decided": False,
            "status": "running",
            "unresolved_so_far": gate.get("unresolved", []),
        }
    return gate


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@http_mcp.tool()
def interlock_check(
    old_symbol: str,
    new_symbol: str,
    provider: str,
    kind: str = "field_rename",
    implementation: str = "builtin",
) -> str:
    """
    Run a breaking change through Interlock and return the deterministic verdict.

    Discovers every consumer — including ones absent from any published contract
    — migrates or verifies them on an isolated copy, runs their real test suites,
    and returns VERIFIED or NOT_PROVEN_SAFE. Use this before shipping a change
    that other services depend on.

    The verdict is produced by a deterministic program. Report it exactly as
    given; it cannot be overridden.

    Args:
        old_symbol: the symbol being replaced, e.g. customer_id or calc_legacy_c
        new_symbol: its replacement, e.g. account_id or calc_py
        provider: the component that owns the change, e.g. account-service
        kind: field_rename | api_contract_change | transport_migration
        implementation: "builtin" when Interlock should rewrite the consumers
            (Python only), or "external" when a human or another agent has
            already done the work and Interlock should verify it — the mode that
            makes a C-to-Python port checkable.
    """
    settings = _settings()
    spec = core.build_spec(
        kind, provider, old_symbol, new_symbol,
        settings.components_root, implementation=implementation,
    )
    result = core.check(_conn(settings), f"{old_symbol} -> {new_symbol}", spec)
    result["gate"] = _gate_payload(result["gate"])
    return _render(result)


@http_mcp.tool()
def interlock_gate(change_id: str) -> str:
    """
    Return the deterministic safety verdict for an existing change.

    Computed by pure Python with no model involvement and cannot be overridden —
    not by this server, not by any agent. Read it; do not argue with it.
    """
    settings = _settings()
    return _render(_gate_payload(core.gate_status(_conn(settings), change_id)))


@http_mcp.tool()
def interlock_evidence(change_id: str, claim_type: str | None = None) -> str:
    """
    Return the evidence trail for a change.

    Every claim carries a source reference and, where it concerns code that
    changed, a real git commit SHA. Use this to explain *why* a verdict is what
    it is.

    Args:
        claim_type: optionally filter to dependency | migration_status |
            test_result | risk. "risk" is the useful one for diagnosing a
            NOT_PROVEN_SAFE verdict.
    """
    settings = _settings()
    items = core.evidence(_conn(settings), change_id)
    if claim_type:
        items = [e for e in items if e["claim_type"] == claim_type]
    return _render(items)


@http_mcp.tool()
def interlock_discover(
    old_symbol: str,
    provider: str,
    new_symbol: str = "",
    kind: str = "field_rename",
) -> str:
    """
    Report what Interlock sees in the repository, without changing anything.

    Prefer this over interlock_check when the user asks which services depend on
    something, or whether a change is worth worrying about. It reads source
    only - no workspace copy, no git, no tests executed, nothing written - so it
    answers in seconds where a full check takes minutes.

    Returns the components found, the dependency edges between them, and which
    consumers appear in NO api contract. Those couple through events or a shared
    database schema, so no contract review would surface them; they are the ones
    that break production.

    Also reports each component's detected toolchain and whether it needs an
    `interlock.toml` to be testable at all.

    An empty `edges` list means nothing outside the provider references that
    symbol. Report that as "no consumers found", never as "the change is safe" -
    only interlock_check produces a verdict.
    """
    settings = _settings()
    return _render(core.discover(
        settings.components_root, provider, old_symbol, new_symbol or None, kind,
    ))


@http_mcp.tool()
def interlock_dependency_graph(change_id: str) -> str:
    """
    Return the discovered dependency graph as nodes and typed edges.

    Edge types are api, event, db or undocumented. An `undocumented` edge is a
    consumer found only by reading source — the kind that breaks production
    because nobody knew it existed.
    """
    settings = _settings()
    return _render(core.graph(_conn(settings), change_id))


@http_mcp.tool()
def interlock_security(old_symbol: str = "", new_symbol: str = "") -> str:
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
    settings = _settings()
    return _render(
        core.security_scan(settings.components_root, old_symbol, new_symbol)
    )


@http_mcp.tool()
def interlock_list_changes() -> str:
    """List the changes recorded in this Interlock instance, newest first."""
    settings = _settings()
    return _render(core.changes(_conn(settings)))


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class SharedSecretMiddleware:
    """
    Reject any request that does not present the configured key.

    These tools execute each component's declared test command, so an open
    endpoint is a remote code execution surface rather than a convenience.
    """

    def __init__(self, app: ASGIApp, key: str, path: str = "/mcp") -> None:
        self.app = app
        self.key = key.encode()
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # This is mounted as a catch-all so `POST /mcp` matches exactly without
        # a 307 redirect, which means every unmatched request reaches here too.
        # Anything that is not the MCP path is a genuine 404, and must not be
        # answered with 401 — that would make a typo in a URL look like an
        # authentication problem, and would hide /health behind the key.
        if scope.get("path", "") != self.path:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(
                scope, receive, send
            )
            return

        headers = {
            k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
        }
        presented = (
            headers.get("authorization", "").removeprefix("Bearer ").strip()
            or headers.get("x-api-key", "").strip()
        )
        # Encode both sides: compare_digest raises TypeError on a str containing
        # any non-ASCII character, which would turn a wrong key into a 500.
        if not presented or not hmac.compare_digest(presented.encode(), self.key):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


def build(settings: Settings):
    """
    Return `(asgi_app, lifespan_source)`, or `(None, None)` when no key is set.

    None means "do not mount" — the same refuse-rather-than-default-open stance
    as `/chat/completions`, for the same reason.
    """
    key = settings.orchestrate.external_agent_key
    if not key:
        return None, None

    sub = http_mcp.streamable_http_app(
        # Full path inside the sub-app, mounted at root: `POST /mcp` matches
        # exactly. With streamable_http_path="/" the registered URL 307s.
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Auto-enables otherwise and 421s every request, localhost included.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    return SharedSecretMiddleware(sub, key, path="/mcp"), sub
