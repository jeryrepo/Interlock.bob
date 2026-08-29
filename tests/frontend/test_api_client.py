"""tests/frontend/test_api_client.py

The client must surface failures as ApiError and must never fabricate data.
The orchestrator's own FastAPI app is used as the server under test, so the
client is exercised against the real contract rather than a hand-written mock.
"""
import sys
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

import orchestrator.ledger as ledger
from orchestrator.main import app

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
sys.path.insert(0, str(FRONTEND))

from utils.api_client import ApiError, InterlockClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """InterlockClient whose requests are routed into the FastAPI TestClient."""
    with TestClient(app, raise_server_exceptions=False) as test_client:
        # Replace the lifespan-opened on-disk connection with a scratch one.
        app.state.conn.close()
        conn = ledger.init_db(":memory:")
        app.state.conn = conn

        def _request(method, url, timeout=None, **kwargs):
            path = url.replace("http://testserver", "")
            return test_client.request(method, path, **kwargs)

        def _get(url, timeout=None, **kwargs):
            return _request("GET", url, **kwargs)

        monkeypatch.setattr(requests, "request", _request)
        monkeypatch.setattr(requests, "get", _get)
        yield InterlockClient(base_url="http://testserver")
        conn.close()


def test_full_happy_path_returns_backend_values(client):
    created = client.create_change_request("customer_id -> account_id")
    change_id = created["id"]
    assert created["status"] == "COORDINATE"

    evidence = client.get_evidence(change_id)["evidence"]
    assert evidence, "discovery must have written evidence"

    graph = client.get_graph(change_id)
    assert graph["edges"]

    gate = client.get_gate(change_id)
    assert gate["decided"] is False

    client.approve(change_id, "coordinate", "alice")

    gate = client.get_gate(change_id)
    assert gate["decided"] is True
    assert gate["result"] == "VERIFIED"

    approvals = client.get_approvals(change_id)["approvals"]
    assert [a["gate"] for a in approvals] == ["coordinate"]


def test_missing_change_raises_api_error_with_status(client):
    with pytest.raises(ApiError) as exc:
        client.get_change_request("does-not-exist")
    assert exc.value.status == 404
    assert exc.value.unreachable is False


def test_rejected_approval_raises_and_is_not_swallowed(client):
    created = client.create_change_request("customer_id -> account_id")
    # legacy_removal requires state APPROVE; the change is at COORDINATE.
    with pytest.raises(ApiError) as exc:
        client.approve(created["id"], "legacy_removal")
    assert exc.value.status == 409
    assert exc.value.detail


def test_unreachable_backend_is_flagged(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "request", boom)
    monkeypatch.setattr(requests, "get", boom)

    offline = InterlockClient(base_url="http://127.0.0.1:9")
    assert offline.health() is False
    with pytest.raises(ApiError) as exc:
        offline.get_change_request("x")
    assert exc.value.unreachable is True


def test_snapshot_reports_section_errors_instead_of_faking_data(client):
    snap = client.snapshot("does-not-exist")
    assert snap["change"] is None
    assert set(snap["errors"]) == {"change", "evidence", "graph", "gate", "approvals"}
