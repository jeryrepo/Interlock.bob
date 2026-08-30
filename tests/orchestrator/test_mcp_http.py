"""
tests/orchestrator/test_mcp_http.py
====================================
Tests for the MCP-over-HTTP surface that watsonx Orchestrate consumes.

Every assertion here corresponds to a way the obvious implementation failed when
it was actually run:

- no lifespan driving → `RuntimeError: Task group is not initialized`
- DNS-rebinding protection left on → 421 to every request, localhost included
- `streamable_http_path="/"` → 307 on the exact URL you register
- a `""` mount without a path guard → 401 for `/health` and 404s become 401

The session manager can only be started once per `MCPServer` instance, so the
client is module-scoped. A second `TestClient(main.app)` context in this module
would raise `RuntimeError: ... can only be called once per instance`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

KEY = "test-mcp-key"
HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    # The streamable-http transport negotiates on Accept; omitting the
    # event-stream type makes the server reject the request as unacceptable.
    "Accept": "application/json, text/event-stream",
}


def _rpc(method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """
    A single app instance with the MCP surface mounted.

    Module-scoped deliberately: `StreamableHTTPSessionManager.run()` raises if
    called twice on the same server object, and `http_mcp` is a module global.
    """
    import os

    workspace = tmp_path_factory.mktemp("mcp-work")
    previous = {
        k: os.environ.get(k)
        for k in ("INTERLOCK_EXTERNAL_AGENT_KEY", "INTERLOCK_WORKSPACE",
                  "INTERLOCK_DB_PATH")
    }
    os.environ["INTERLOCK_EXTERNAL_AGENT_KEY"] = KEY
    os.environ["INTERLOCK_WORKSPACE"] = str(workspace / "ws")
    os.environ["INTERLOCK_DB_PATH"] = str(workspace / "ledger.db")

    import importlib

    import orchestrator.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    importlib.reload(main)


# ---------------------------------------------------------------------------
# The mount works at all
# ---------------------------------------------------------------------------

class TestMount:
    def test_tools_list_succeeds(self, client):
        """
        The whole recipe in one assertion: lifespan driven, transport security
        off, exact path. Any of the three wrong and this is 421, 307 or a
        RuntimeError rather than 200.
        """
        response = client.post("/mcp", headers=HEADERS, json=_rpc("tools/list"))
        assert response.status_code == 200, response.text
        assert "result" in response.json()

    def test_no_redirect_on_the_registered_url(self, client):
        """`/mcp` is the URL registered with Orchestrate; it must not 307."""
        response = client.post(
            "/mcp", headers=HEADERS, json=_rpc("tools/list"), follow_redirects=False
        )
        assert response.status_code != 307

    def test_the_expected_tools_are_exposed(self, client):
        tools = client.post("/mcp", headers=HEADERS, json=_rpc("tools/list")).json()
        names = {t["name"] for t in tools["result"]["tools"]}
        assert names == {
            "interlock_check", "interlock_discover", "interlock_gate",
            "interlock_evidence", "interlock_dependency_graph",
            "interlock_list_changes",
        }

    def test_every_tool_documents_itself(self, client):
        """Orchestrate rejects tools without descriptions."""
        tools = client.post("/mcp", headers=HEADERS, json=_rpc("tools/list")).json()
        for tool in tools["result"]["tools"]:
            assert tool.get("description"), tool["name"]


# ---------------------------------------------------------------------------
# It does not swallow the rest of the app
# ---------------------------------------------------------------------------

class TestMountDoesNotShadowTheApp:
    """
    A `""` mount matches every path. Without a guard the middleware answered
    401 for `/health` and turned every 404 into a 401.
    """

    def test_health_is_still_reachable_without_the_key(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert "integrations" in response.json()

    def test_health_reports_the_mcp_surface_as_mounted(self, client):
        integrations = client.get("/health").json()["integrations"]
        assert "/mcp" in integrations["watsonx_orchestrate"]["mcp_http"]

    def test_unknown_paths_are_404_not_401(self, client):
        assert client.get("/definitely-not-a-route").status_code == 404

    def test_existing_api_routes_still_work(self, client):
        """An additive mount must not break the pre-existing API."""
        assert client.get("/change-requests/no-such-change").status_code == 404


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_credentials_rejected(self, client):
        response = client.post(
            "/mcp",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=_rpc("tools/list"),
        )
        assert response.status_code == 401

    def test_wrong_credentials_rejected(self, client):
        response = client.post(
            "/mcp", headers={**HEADERS, "Authorization": "Bearer nope"},
            json=_rpc("tools/list"),
        )
        assert response.status_code == 401

    def test_x_api_key_header_is_accepted(self, client):
        """Orchestrate connections can present the key as x-api-key."""
        headers = {k: v for k, v in HEADERS.items() if k != "Authorization"}
        response = client.post(
            "/mcp", headers={**headers, "X-API-Key": KEY}, json=_rpc("tools/list")
        )
        assert response.status_code == 200

    def test_a_non_ascii_configured_key_does_not_crash(self):
        """
        `hmac.compare_digest` raises TypeError on a str containing any
        non-ASCII character, which would turn a wrong key into a 500. Comparing
        encoded bytes keeps it a clean rejection.

        Tested at the middleware rather than over HTTP: the HTTP client refuses
        to send a non-ASCII header at all, so the realistic case is a developer
        whose *configured* key has an accent in it.
        """
        import asyncio

        from interlock_mcp.http import SharedSecretMiddleware

        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        async def never_called(scope, receive, send):
            raise AssertionError("request should not have reached the MCP app")

        middleware = SharedSecretMiddleware(never_called, "clé-secrète")
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
        asyncio.run(middleware(scope, receive, send))
        assert sent[0]["status"] == 401


# ---------------------------------------------------------------------------
# Tool shape
# ---------------------------------------------------------------------------

class TestToolContract:
    def _schema(self, client, name: str) -> dict:
        tools = client.post("/mcp", headers=HEADERS, json=_rpc("tools/list")).json()
        return next(t for t in tools["result"]["tools"] if t["name"] == name)

    def test_a_model_cannot_choose_a_filesystem_path(self, client):
        """
        `interlock_check` executes each component's declared test command, so a
        supervisor LLM choosing the directory would be choosing what runs.
        These parameters exist on the stdio tools and deliberately not here.
        """
        properties = self._schema(client, "interlock_check")["inputSchema"]["properties"]
        assert "components_root" not in properties
        assert "db_path" not in properties

    def test_discover_also_refuses_a_caller_chosen_directory(self, client):
        """
        The same rule as interlock_check, for the same reason.

        `discover` executes nothing, so the risk is narrower - but a supervisor
        able to name the directory could still walk Interlock through a tree the
        operator never chose, and report its contents back. The components root
        stays server-side on every tool of this surface.
        """
        properties = self._schema(client, "interlock_discover")["inputSchema"]["properties"]
        assert "components_root" not in properties
        assert "db_path" not in properties

    def test_discover_is_reachable_and_needs_no_prior_change(self, client):
        """The cheap reconnaissance call: no change_id, nothing recorded first."""
        properties = self._schema(client, "interlock_discover")["inputSchema"]["properties"]
        assert "change_id" not in properties
        assert {"old_symbol", "provider"} <= set(properties)

    def test_check_exposes_the_external_implementation_mode(self, client):
        """The mode that makes a C-to-Python port checkable must be reachable."""
        properties = self._schema(client, "interlock_check")["inputSchema"]["properties"]
        assert "implementation" in properties

    def test_no_tool_can_override_the_gate_or_approve_removal(self, client):
        tools = client.post("/mcp", headers=HEADERS, json=_rpc("tools/list")).json()
        names = {t["name"] for t in tools["result"]["tools"]}
        forbidden = ("override", "legacy_removal", "set_verified", "force", "bypass")
        assert not [n for n in names for k in forbidden if k in n]


class TestPreviewIsNotAVerdict:
    """
    `core.gate_status()` returns a live preview with `decided: false` before a
    decision is recorded. A model reading `result` and ignoring `decided` would
    present a mid-flight snapshot as final, so the fields are removed.
    """

    def test_undecided_gate_carries_no_verdict(self):
        from interlock_mcp.http import _gate_payload

        payload = _gate_payload({
            "change_id": "c1", "decided": False,
            "result": "NOT_PROVEN_SAFE", "reason": "still running",
            "unresolved": ["checkout"],
        })
        assert "result" not in payload
        assert "reason" not in payload
        assert payload["status"] == "running"
        assert payload["unresolved_so_far"] == ["checkout"]

    def test_decided_gate_passes_through_untouched(self):
        from interlock_mcp.http import _gate_payload

        decided = {
            "change_id": "c1", "decided": True, "result": "VERIFIED",
            "reason": "All required consumers have been verified.",
            "unresolved": [],
        }
        assert _gate_payload(decided) == decided


class TestDisabledWithoutAKey:
    def test_build_returns_none_without_a_key(self):
        """Refuse rather than default open — these tools execute commands."""
        from interlock_mcp.http import build
        from orchestrator.settings import Settings

        app, lifespan = build(Settings())
        assert app is None and lifespan is None
