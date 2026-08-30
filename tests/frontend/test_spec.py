"""
tests/frontend/test_spec.py
============================
Tests for the UI's ChangeSpec builder and the client's spec plumbing.

The regression these exist to prevent: the UI used to POST only a description,
which meant the orchestrator ran its stub workflow and every panel rendered
seeded demo data while looking exactly like a real run. Sending a spec is what
selects the real agents, so it is worth pinning down.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import orchestrator.ledger as ledger
import orchestrator.main as main

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from utils.api_client import InterlockClient  # noqa: E402
from utils.spec import CHANGE_KINDS, build_spec, missing_fields  # noqa: E402


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

class TestBuildSpec:
    def test_field_rename_uses_field_keys(self):
        spec = build_spec("field_rename", "account-service", "customer_id", "account_id")
        assert spec["old_field"] == "customer_id"
        assert spec["new_field"] == "account_id"
        assert "old_symbol" not in spec

    def test_api_contract_change_uses_field_keys(self):
        spec = build_spec("api_contract_change", "svc", "a", "b")
        assert spec["old_field"] == "a" and spec["new_field"] == "b"

    def test_transport_migration_uses_symbol_keys(self):
        """A transport migration moves a delivery symbol, not a data field."""
        spec = build_spec(
            "transport_migration", "event-publisher",
            "deliver_via_webhook", "deliver_via_pubsub", "fixtures_transport",
        )
        assert spec["old_symbol"] == "deliver_via_webhook"
        assert spec["new_symbol"] == "deliver_via_pubsub"
        assert "old_field" not in spec
        assert spec["topic"] and spec["webhook_path"]

    def test_components_root_is_carried(self):
        spec = build_spec("field_rename", "svc", "a", "b", "fixtures_transport")
        assert spec["components_root"] == "fixtures_transport"

    def test_blank_components_root_falls_back(self):
        assert build_spec("field_rename", "svc", "a", "b", "")["components_root"] == "fixtures"

    def test_the_ui_offers_exactly_the_supported_kinds(self):
        """A kind in the dropdown with no backend support would 422 on submit."""
        from orchestrator.schemas import CHANGE_KINDS as BACKEND_KINDS

        assert set(CHANGE_KINDS) == set(BACKEND_KINDS)

    @pytest.mark.parametrize("kind", CHANGE_KINDS)
    def test_every_offered_kind_validates_against_the_backend_model(self, kind):
        """
        The UI builds shape only; the backend owns validation. This proves the
        two agree, so a user cannot assemble a payload the API will reject.
        """
        from pydantic import TypeAdapter

        from orchestrator.schemas import ChangeSpec

        spec = build_spec(kind, "svc", "old_thing", "new_thing", "fixtures")
        TypeAdapter(ChangeSpec).validate_python(spec)


class TestMissingFields:
    def test_reports_each_blank_input(self):
        assert missing_fields("", "old", "") == ["provider", "to"]

    def test_whitespace_only_counts_as_blank(self):
        assert missing_fields("   ", "old", "new") == ["provider"]

    def test_complete_input_reports_nothing(self):
        assert missing_fields("svc", "old", "new") == []


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    with TestClient(main.app) as client:
        main.app.state.conn.close()
        main.app.state.conn = ledger.init_db(":memory:")
        yield client


@pytest.fixture
def ui(api):
    """The real UI client, with its transport pointed at the test app."""
    client = InterlockClient("http://testserver")
    client._request = lambda method, path, **kw: api.request(method, path, **kw).json()
    return client


class TestClientSpecPlumbing:
    def test_omitting_a_spec_keeps_the_stub_path(self, ui):
        """Back-compat: a description-only request must still work."""
        created = ui.create_change_request("customer_id -> account_id")
        assert ui.get_spec(created["id"])["kind"] is None

    def test_sending_a_spec_selects_the_real_path(self, ui):
        spec = build_spec("field_rename", "account-service", "customer_id", "account_id")
        created = ui.create_change_request("customer_id -> account_id", spec)
        assert ui.get_spec(created["id"])["kind"] == "field_rename"


@pytest.mark.integration
class TestUiDrivesRealAgents:
    """
    The regression that mattered: the UI rendering seeded stub data while
    looking like a real run.
    """

    def test_dependencies_are_discovered_not_seeded(self, ui):
        spec = build_spec("field_rename", "account-service", "customer_id", "account_id")
        change_id = ui.create_change_request("customer_id -> account_id", spec)["id"]

        edges = {(e["from"], e["to"]): e["edge_type"] for e in ui.get_graph(change_id)["edges"]}
        # analytics-worker is linked to the provider only by source code.
        assert ("account-service", "analytics-worker") in edges
        assert edges[("account-service", "analytics-worker")] == "event"

    def test_commit_refs_are_real_shas(self, ui):
        spec = build_spec("field_rename", "account-service", "customer_id", "account_id")
        change_id = ui.create_change_request("x", spec)["id"]
        ui.approve(change_id, "coordinate", "ui-user")

        evidence = ui.get_evidence(change_id)["evidence"]
        shas = {
            e["source_revision"]
            for e in evidence
            if e["claim_type"] == "migration_status" and e["source_revision"]
        }
        assert shas, "no commit refs recorded"
        assert all(len(s) == 40 for s in shas), "stub placeholders, not real SHAs"

    def test_gate_reaches_verified_through_the_ui_client(self, ui):
        spec = build_spec("field_rename", "account-service", "customer_id", "account_id")
        change_id = ui.create_change_request("x", spec)["id"]
        ui.approve(change_id, "coordinate", "ui-user")
        assert ui.get_gate(change_id)["result"] == "VERIFIED"
