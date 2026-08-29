"""tests/orchestrator/test_api.py

FastAPI endpoint tests using TestClient.
All tests use an in-memory SQLite database via app.state override.

These tests exercise API and state-machine structure in isolation.
They force STUB_MODE=True so they remain fast and deterministic regardless of
what the module-level default is set to for production use.

Gate flow under test:
  POST /change-requests → status=COORDINATE
  POST /approve coordinate → status=GATE_DECISION
  POST /approve legacy_removal → status=DONE
"""
import pytest
from fastapi.testclient import TestClient

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.main import app
import orchestrator.agent_runner as agent_runner


@pytest.fixture(autouse=True)
def force_stub_mode(monkeypatch):
    """Force stub mode for all tests in this module — keeps them fast and deterministic."""
    monkeypatch.setattr(agent_runner, "STUB_MODE", True)


@pytest.fixture
def client():
    """TestClient backed by a fresh in-memory database."""
    conn = ledger.init_db(":memory:")
    app.state.conn = conn
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    conn.close()


@pytest.fixture
def posted(client):
    """A change request POSTed; workflow stops at COORDINATE."""
    resp = client.post(
        "/change-requests",
        json={"description": "customer_id -> account_id"},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
def at_approve(client, posted):
    """Change advanced to APPROVE state via coordinate approval."""
    resp = client.post(
        f"/change-requests/{posted['id']}/approve",
        json={"gate": "coordinate", "approved_by": "alice"},
    )
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "APPROVE"
    return posted


@pytest.fixture
def done(client, at_approve):
    """Change fully approved through both gates — status DONE."""
    resp = client.post(
        f"/change-requests/{at_approve['id']}/approve",
        json={"gate": "legacy_removal", "approved_by": "alice"},
    )
    assert resp.status_code == 200
    return at_approve


class TestPostChangeRequests:
    def test_returns_201(self, client):
        resp = client.post(
            "/change-requests",
            json={"description": "test migration"},
        )
        assert resp.status_code == 201

    def test_response_stops_at_coordinate(self, client):
        """POST /change-requests must stop at COORDINATE — never auto-approve."""
        resp = client.post(
            "/change-requests",
            json={"description": "test migration"},
        )
        body = resp.json()
        assert "id" in body
        assert body["status"] == "COORDINATE"

    def test_missing_description_returns_422(self, client):
        resp = client.post("/change-requests", json={})
        assert resp.status_code == 422


class TestGetChangeRequest:
    def test_returns_200(self, client, posted):
        resp = client.get(f"/change-requests/{posted['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == posted["id"]

    def test_unknown_id_returns_404(self, client):
        resp = client.get("/change-requests/no-such-id")
        assert resp.status_code == 404


class TestGetEvidence:
    def test_returns_evidence_list(self, client, posted):
        resp = client.get(f"/change-requests/{posted['id']}/evidence")
        assert resp.status_code == 200
        body = resp.json()
        assert "evidence" in body
        assert isinstance(body["evidence"], list)
        assert len(body["evidence"]) > 0

    def test_unknown_id_returns_404(self, client):
        resp = client.get("/change-requests/no-such-id/evidence")
        assert resp.status_code == 404


class TestGetGraph:
    def test_returns_nodes_and_edges(self, client, posted):
        resp = client.get(f"/change-requests/{posted['id']}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert "nodes" in body
        assert "edges" in body
        assert len(body["nodes"]) > 0

    def test_analytics_worker_in_graph(self, client, posted):
        resp = client.get(f"/change-requests/{posted['id']}/graph")
        node_ids = {n["id"] for n in resp.json()["nodes"]}
        assert "analytics-worker" in node_ids

    def test_unknown_id_returns_404(self, client):
        resp = client.get("/change-requests/no-such-id/graph")
        assert resp.status_code == 404


class TestApprove:
    # ------------------------------------------------------------------
    # Coordinate gate
    # ------------------------------------------------------------------

    def test_approve_coordinate_advances_to_approve(self, client, posted):
        """Approving coordinate runs MODIFY/REHEARSE/VERIFY and, when VERIFIED, reaches APPROVE."""
        resp = client.post(
            f"/change-requests/{posted['id']}/approve",
            json={"gate": "coordinate", "approved_by": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["new_status"] == "APPROVE"

    def test_approve_coordinate_wrong_state_returns_409(self, client, done):
        """Change is in DONE — approving coordinate gate must be rejected."""
        resp = client.post(
            f"/change-requests/{done['id']}/approve",
            json={"gate": "coordinate", "approved_by": "alice"},
        )
        assert resp.status_code == 409


    # ------------------------------------------------------------------
    # Legacy-removal gate
    # ------------------------------------------------------------------

    def test_approve_legacy_removal_reaches_done(self, client, at_approve):
        """After coordinate approval, legacy_removal reaches DONE."""
        resp = client.post(
            f"/change-requests/{at_approve['id']}/approve",
            json={"gate": "legacy_removal", "approved_by": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["new_status"] == "DONE"

    def test_legacy_removal_rejected_when_consumer_not_verified(self, client):
        """
        Attempting legacy_removal approval while a consumer migration is not
        yet 'verified' must be rejected with 409.

        This is the deterministic safety guarantee: evaluate_gate() is called
        server-side before the approval is accepted, regardless of state.
        """
        conn = app.state.conn

        # Build a change in APPROVE state where one consumer is still in_progress.
        import uuid
        cid = str(uuid.uuid4())
        ledger.create_change(conn, cid, "test")

        # Seed graph: checkout depends on account-service
        ledger.add_dependency(conn, cid, "checkout", "account-service", "api")

        # Advance to PLANNING and seed consumer row
        sm.advance(conn, cid)  # INTAKE → DISCOVERY
        ledger.upsert_consumer_migration(conn, cid, "checkout", "pending")
        sm.advance(conn, cid)  # DISCOVERY → PLANNING
        sm.advance(conn, cid)  # PLANNING → COORDINATE
        ledger.record_approval(conn, cid, "coordinate", "tester")
        sm.advance(conn, cid)  # COORDINATE → MODIFY

        # Consumer is still in_progress — not verified
        ledger.upsert_consumer_migration(conn, cid, "checkout", "in_progress")

        # Force through to APPROVE without real verification
        sm.force_state(conn, cid, "APPROVE")
        # Record a gate decision that is NOT_PROVEN_SAFE to match ledger reality
        ledger.record_gate_decision(conn, cid, "NOT_PROVEN_SAFE", "checkout not verified")

        resp = client.post(
            f"/change-requests/{cid}/approve",
            json={"gate": "legacy_removal", "approved_by": "alice"},
        )
        assert resp.status_code == 409
        assert "NOT_PROVEN_SAFE" in resp.json()["detail"]

    def test_legacy_removal_rejected_when_no_migration_record(self, client):
        """
        If a required consumer has no migration record at all, legacy_removal
        must be rejected — gate returns NOT_PROVEN_SAFE.
        """
        conn = app.state.conn

        import uuid
        cid = str(uuid.uuid4())
        ledger.create_change(conn, cid, "test")

        # Dependency exists but NO consumer_migration row
        ledger.add_dependency(conn, cid, "checkout", "account-service", "api")
        sm.force_state(conn, cid, "APPROVE")

        resp = client.post(
            f"/change-requests/{cid}/approve",
            json={"gate": "legacy_removal", "approved_by": "alice"},
        )
        assert resp.status_code == 409
        assert "NOT_PROVEN_SAFE" in resp.json()["detail"]

    # ------------------------------------------------------------------
    # General gate validation
    # ------------------------------------------------------------------

    def test_approve_unknown_gate_returns_400(self, client, posted):
        resp = client.post(
            f"/change-requests/{posted['id']}/approve",
            json={"gate": "no_such_gate", "approved_by": "alice"},
        )
        assert resp.status_code == 400

    def test_approve_missing_id_returns_404(self, client):
        resp = client.post(
            "/change-requests/no-such-id/approve",
            json={"gate": "coordinate", "approved_by": "alice"},
        )
        assert resp.status_code == 404

    def test_full_two_gate_flow_reaches_done(self, client):
        """End-to-end: POST → coordinate approval → legacy_removal approval → DONE."""
        # Create
        resp = client.post(
            "/change-requests",
            json={"description": "customer_id -> account_id"},
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]
        assert resp.json()["status"] == "COORDINATE"

        # Coordinate gate
        resp = client.post(
            f"/change-requests/{cid}/approve",
            json={"gate": "coordinate", "approved_by": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["new_status"] == "APPROVE"

        # Legacy removal gate
        resp = client.post(
            f"/change-requests/{cid}/approve",
            json={"gate": "legacy_removal", "approved_by": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["new_status"] == "DONE"

        # Confirm persisted state
        resp = client.get(f"/change-requests/{cid}")
        assert resp.json()["status"] == "DONE"
