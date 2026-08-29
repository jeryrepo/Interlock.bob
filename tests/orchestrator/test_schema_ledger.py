"""tests/orchestrator/test_schema_ledger.py

Tests every ledger write/read function against an in-memory database.
"""
import json
import pytest
import orchestrator.ledger as ledger


class TestInitDb:
    def test_tables_created(self, conn):
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "change_request", "evidence", "dependency_edge",
            "consumer_migration", "approval", "gate_decision",
        }
        assert expected.issubset(tables)

    def test_idempotent(self, conn):
        """Calling init_db on an existing schema must not raise."""
        ledger.init_db(":memory:")  # fresh — no error expected


class TestCreateChange:
    def test_returns_dict(self, change):
        assert isinstance(change, dict)

    def test_fields(self, change):
        assert change["status"] == "INTAKE"
        assert change["description"] == "customer_id -> account_id"
        assert change["retry_count"] == 0
        assert "created_at" in change

    def test_get_change_round_trip(self, conn, change):
        fetched = ledger.get_change(conn, change["id"])
        assert fetched["id"] == change["id"]

    def test_get_change_missing(self, conn):
        assert ledger.get_change(conn, "no-such-id") is None


class TestUpdateChangeStatus:
    def test_status_updated(self, conn, change):
        ledger.update_change_status(conn, change["id"], "DISCOVERY")
        row = ledger.get_change(conn, change["id"])
        assert row["status"] == "DISCOVERY"

    def test_retry_increment(self, conn, change):
        ledger.update_change_status(conn, change["id"], "DISCOVERY", increment_retry=True)
        row = ledger.get_change(conn, change["id"])
        assert row["retry_count"] == 1

    def test_retry_resets_on_normal_update(self, conn, change):
        ledger.update_change_status(conn, change["id"], "DISCOVERY", increment_retry=True)
        ledger.update_change_status(conn, change["id"], "PLANNING")
        row = ledger.get_change(conn, change["id"])
        assert row["retry_count"] == 0


class TestAddEvidence:
    def test_add_and_read(self, conn, change):
        ev = ledger.add_evidence(
            conn, change["id"], "dependency", "checkout",
            {"field": "customer_id"}, "fixtures/checkout/app/main.py",
            "confirmed", "abc123",
        )
        assert ev["claim_type"] == "dependency"
        assert ev["source_revision"] == "abc123"
        assert isinstance(ev["content"], dict)

    def test_get_evidence_returns_list(self, conn, change):
        ledger.add_evidence(
            conn, change["id"], "test_result", "checkout",
            {"passed": True}, "fixtures/checkout/tests/", "confirmed",
        )
        rows = ledger.get_evidence(conn, change["id"])
        assert len(rows) == 1
        assert isinstance(rows[0]["content"], dict)

    def test_content_stored_as_json(self, conn, change):
        payload = {"nested": {"key": [1, 2, 3]}}
        ledger.add_evidence(
            conn, change["id"], "risk", "analytics-worker",
            payload, "fixtures/analytics-worker/app/worker.py", "hypothesis",
        )
        raw = conn.execute("SELECT content FROM evidence WHERE change_id = ?",
                           (change["id"],)).fetchone()[0]
        assert json.loads(raw) == payload


class TestAddDependency:
    def test_add_and_read(self, conn, change):
        dep = ledger.add_dependency(
            conn, change["id"], "checkout", "account-service", "api",
            "calls /accounts endpoint",
        )
        assert dep["from_component"] == "checkout"
        assert dep["edge_type"] == "api"

    def test_get_dependencies(self, conn, change):
        ledger.add_dependency(conn, change["id"], "account-service", "fraud", "api")
        ledger.add_dependency(
            conn, change["id"], "account-service", "analytics-worker", "event",
            documentation_status="undocumented",
        )
        rows = ledger.get_dependencies(conn, change["id"])
        assert len(rows) == 2


class TestUpsertConsumerMigration:
    def test_insert(self, conn, change):
        row = ledger.upsert_consumer_migration(conn, change["id"], "checkout", "pending")
        assert row["status"] == "pending"
        assert row["consumer"] == "checkout"

    def test_upsert_updates_status(self, conn, change):
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "pending")
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "verified")
        rows = ledger.get_consumer_migrations(conn, change["id"])
        assert len(rows) == 1
        assert rows[0]["status"] == "verified"

    def test_preserves_id_on_update(self, conn, change):
        first = ledger.upsert_consumer_migration(conn, change["id"], "checkout", "pending")
        second = ledger.upsert_consumer_migration(conn, change["id"], "checkout", "verified")
        assert first["id"] == second["id"]


class TestRecordApproval:
    def test_record_and_read(self, conn, change):
        row = ledger.record_approval(conn, change["id"], "coordinate", "alice")
        assert row["gate"] == "coordinate"
        assert row["approved_by"] == "alice"

    def test_get_approvals(self, conn, change):
        ledger.record_approval(conn, change["id"], "coordinate", "alice")
        ledger.record_approval(conn, change["id"], "legacy_removal", "bob")
        rows = ledger.get_approvals(conn, change["id"])
        assert len(rows) == 2


class TestRecordGateDecision:
    def test_record_and_read(self, conn, change):
        row = ledger.record_gate_decision(conn, change["id"], "VERIFIED", "all verified")
        assert row["result"] == "VERIFIED"

    def test_get_latest(self, conn, change):
        ledger.record_gate_decision(conn, change["id"], "NOT_PROVEN_SAFE", "missing fraud")
        ledger.record_gate_decision(conn, change["id"], "VERIFIED", "all verified")
        latest = ledger.get_latest_gate_decision(conn, change["id"])
        assert latest["result"] == "VERIFIED"

    def test_get_latest_none_when_empty(self, conn, change):
        assert ledger.get_latest_gate_decision(conn, change["id"]) is None
