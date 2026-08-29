"""tests/orchestrator/test_api_projections.py

Tests for the read-only projections consumed by the Streamlit UI:
  GET /change-requests/{id}/gate
  GET /change-requests/{id}/approvals

These endpoints must never mutate state and must never present a live gate
preview as a recorded decision.
"""
import pytest
from fastapi.testclient import TestClient

import orchestrator.ledger as ledger
from orchestrator.main import app
import orchestrator.agent_runner as agent_runner


@pytest.fixture(autouse=True)
def force_stub_mode(monkeypatch):
    """Force stub mode for all tests in this module — keeps them fast and deterministic."""
    monkeypatch.setattr(agent_runner, "STUB_MODE", True)


@pytest.fixture
def client():
    """
    TestClient backed by a fresh in-memory database.

    The app's lifespan opens the configured on-disk DB and assigns
    app.state.conn, so the in-memory override must be installed *after*
    the context manager starts or these tests would mutate interlock.db.
    """
    with TestClient(app, raise_server_exceptions=True) as c:
        app.state.conn.close()
        conn = ledger.init_db(":memory:")
        app.state.conn = conn
        yield c
        conn.close()


@pytest.fixture
def posted(client):
    resp = client.post(
        "/change-requests", json={"description": "customer_id -> account_id"}
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
def at_approve(client, posted):
    resp = client.post(
        f"/change-requests/{posted['id']}/approve",
        json={"gate": "coordinate", "approved_by": "alice"},
    )
    assert resp.status_code == 200
    return posted


# ---------------------------------------------------------------------------
# /gate
# ---------------------------------------------------------------------------


def test_gate_before_decision_is_not_marked_decided(client, posted):
    """At COORDINATE no gate_decision row exists yet."""
    body = client.get(f"/change-requests/{posted['id']}/gate").json()
    assert body["decided"] is False
    assert body["decided_at"] is None
    assert body["state"] == "COORDINATE"


def test_gate_lists_planned_consumers_before_migration(client, posted):
    """Consumer rows seeded during planning are visible and still pending."""
    body = client.get(f"/change-requests/{posted['id']}/gate").json()
    statuses = {c["consumer"]: c["status"] for c in body["consumers"]}
    assert statuses
    assert set(statuses.values()) == {"pending"}


def test_gate_after_verification_is_recorded_and_verified(client, at_approve):
    body = client.get(f"/change-requests/{at_approve['id']}/gate").json()
    assert body["decided"] is True
    assert body["result"] == "VERIFIED"
    assert body["decided_at"] is not None
    assert body["unresolved"] == []
    assert body["required_consumers"]
    assert all(c["status"] == "verified" for c in body["consumers"])


def test_gate_reports_unresolved_when_a_consumer_is_not_verified(client, at_approve):
    """Regressing a consumer to failed must surface it as unresolved."""
    conn = app.state.conn
    change_id = at_approve["id"]
    consumer = client.get(f"/change-requests/{change_id}/gate").json()[
        "required_consumers"
    ][0]
    ledger.upsert_consumer_migration(conn, change_id, consumer, "failed")

    body = client.get(f"/change-requests/{change_id}/gate").json()
    # The recorded decision is preserved verbatim...
    assert body["decided"] is True
    # ...while the live read exposes the regression.
    assert consumer in body["unresolved"]


def test_gate_is_side_effect_free(client, posted):
    before = client.get(f"/change-requests/{posted['id']}").json()
    client.get(f"/change-requests/{posted['id']}/gate")
    after = client.get(f"/change-requests/{posted['id']}").json()
    assert before == after


def test_gate_404_for_unknown_change(client):
    assert client.get("/change-requests/nope/gate").status_code == 404


# ---------------------------------------------------------------------------
# /approvals
# ---------------------------------------------------------------------------


def test_approvals_empty_before_any_approval(client, posted):
    body = client.get(f"/change-requests/{posted['id']}/approvals").json()
    assert body["approvals"] == []


def test_approvals_records_coordinate_gate(client, at_approve):
    body = client.get(f"/change-requests/{at_approve['id']}/approvals").json()
    gates = {a["gate"]: a for a in body["approvals"]}
    assert "coordinate" in gates
    assert gates["coordinate"]["approved_by"] == "alice"


def test_approvals_404_for_unknown_change(client):
    assert client.get("/change-requests/nope/approvals").status_code == 404
