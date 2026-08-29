"""tests/orchestrator/test_state_machine.py

Tests for state transitions, can_advance rules, and InvalidTransition.
"""
import pytest
import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.state_machine import InvalidTransition


class TestGetState:
    def test_returns_intake(self, conn, change):
        assert sm.get_state(conn, change["id"]) == "INTAKE"

    def test_missing_change_raises(self, conn):
        with pytest.raises(InvalidTransition, match="not found"):
            sm.get_state(conn, "no-such-id")


class TestCanAdvance:
    def test_intake_always_ready(self, conn, change):
        assert sm.can_advance(conn, change["id"], "INTAKE") is True

    def test_discovery_requires_dependency(self, conn, change):
        ledger.update_change_status(conn, change["id"], "DISCOVERY")
        # No deps yet → cannot advance
        assert sm.can_advance(conn, change["id"], "DISCOVERY") is False
        ledger.add_dependency(conn, change["id"], "checkout", "account-service", "api")
        assert sm.can_advance(conn, change["id"], "DISCOVERY") is True

    def test_planning_requires_consumer_row(self, conn, change):
        ledger.update_change_status(conn, change["id"], "PLANNING")
        assert sm.can_advance(conn, change["id"], "PLANNING") is False
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "pending")
        assert sm.can_advance(conn, change["id"], "PLANNING") is True

    def test_coordinate_requires_approval(self, conn, change):
        ledger.update_change_status(conn, change["id"], "COORDINATE")
        assert sm.can_advance(conn, change["id"], "COORDINATE") is False
        ledger.record_approval(conn, change["id"], "coordinate", "alice")
        assert sm.can_advance(conn, change["id"], "COORDINATE") is True

    def test_modify_requires_in_progress(self, conn, change):
        ledger.update_change_status(conn, change["id"], "MODIFY")
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "pending")
        assert sm.can_advance(conn, change["id"], "MODIFY") is False
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "in_progress")
        assert sm.can_advance(conn, change["id"], "MODIFY") is True

    def test_verify_requires_done_statuses(self, conn, change):
        ledger.update_change_status(conn, change["id"], "VERIFY")
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "in_progress")
        assert sm.can_advance(conn, change["id"], "VERIFY") is False
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "verified")
        assert sm.can_advance(conn, change["id"], "VERIFY") is True

    def test_gate_decision_requires_verified_result(self, conn, change):
        ledger.update_change_status(conn, change["id"], "GATE_DECISION")
        assert sm.can_advance(conn, change["id"], "GATE_DECISION") is False
        ledger.record_gate_decision(conn, change["id"], "NOT_PROVEN_SAFE", "blocked")
        assert sm.can_advance(conn, change["id"], "GATE_DECISION") is False
        ledger.record_gate_decision(conn, change["id"], "VERIFIED", "all ok")
        assert sm.can_advance(conn, change["id"], "GATE_DECISION") is True

    def test_approve_requires_legacy_removal_approval(self, conn, change):
        ledger.update_change_status(conn, change["id"], "APPROVE")
        assert sm.can_advance(conn, change["id"], "APPROVE") is False
        ledger.record_approval(conn, change["id"], "legacy_removal", "alice")
        assert sm.can_advance(conn, change["id"], "APPROVE") is True

    def test_done_cannot_advance(self, conn, change):
        ledger.update_change_status(conn, change["id"], "DONE")
        assert sm.can_advance(conn, change["id"], "DONE") is False


class TestAdvance:
    def test_legal_advance_from_intake(self, conn, change):
        new_state = sm.advance(conn, change["id"])
        assert new_state == "DISCOVERY"
        assert ledger.get_change(conn, change["id"])["status"] == "DISCOVERY"

    def test_advance_without_preconditions_raises(self, conn, change):
        sm.advance(conn, change["id"])  # INTAKE → DISCOVERY
        with pytest.raises(InvalidTransition, match="preconditions not met"):
            sm.advance(conn, change["id"])  # DISCOVERY → PLANNING (no deps yet)

    def test_advance_from_done_raises(self, conn, change):
        sm.force_state(conn, change["id"], "DONE")
        with pytest.raises(InvalidTransition, match="terminal"):
            sm.advance(conn, change["id"])

    def test_full_legal_sequence(self, conn, change):
        """Drive through all states with minimal ledger seeding."""
        cid = change["id"]

        # INTAKE → DISCOVERY
        sm.advance(conn, cid)
        ledger.add_dependency(conn, cid, "checkout", "account-service", "api")

        # DISCOVERY → PLANNING
        sm.advance(conn, cid)
        ledger.upsert_consumer_migration(conn, cid, "checkout", "pending")

        # PLANNING → COORDINATE
        sm.advance(conn, cid)
        ledger.record_approval(conn, cid, "coordinate", "alice")

        # COORDINATE → MODIFY
        sm.advance(conn, cid)
        ledger.upsert_consumer_migration(conn, cid, "checkout", "in_progress")

        # MODIFY → REHEARSE
        ledger.add_evidence(conn, cid, "test_result", "x", {}, "f", "confirmed")
        sm.advance(conn, cid)

        # REHEARSE → VERIFY
        sm.advance(conn, cid)
        ledger.upsert_consumer_migration(conn, cid, "checkout", "verified")

        # VERIFY → GATE_DECISION
        sm.advance(conn, cid)
        ledger.record_gate_decision(conn, cid, "VERIFIED", "ok")

        # GATE_DECISION → APPROVE
        sm.advance(conn, cid)
        ledger.record_approval(conn, cid, "legacy_removal", "alice")

        # APPROVE → DONE
        sm.advance(conn, cid)
        assert ledger.get_change(conn, cid)["status"] == "DONE"


class TestForceState:
    def test_sets_arbitrary_state(self, conn, change):
        sm.force_state(conn, change["id"], "VERIFY")
        assert sm.get_state(conn, change["id"]) == "VERIFY"

    def test_unknown_state_raises(self, conn, change):
        with pytest.raises(InvalidTransition, match="Unknown state"):
            sm.force_state(conn, change["id"], "MADE_UP")
