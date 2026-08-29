"""tests/orchestrator/test_gate.py

Deterministic gate tests — no LLM calls.

Critical cases (from spec):
  1. All consumers verified → VERIFIED
  2. One consumer unresolved → NOT_PROVEN_SAFE
  3. No migration record at all → NOT_PROVEN_SAFE
  4. No consumers in graph → NOT_PROVEN_SAFE
"""
import pytest
import orchestrator.ledger as ledger
from orchestrator.gate import evaluate_gate, get_required_consumers, build_graph


def _seed_graph(conn, change_id, consumers):
    """
    Add dependency edges from account-service to each consumer.

    Canonical direction is provider -> consumer.
    """
    for consumer in consumers:
        ledger.add_dependency(conn, change_id, "account-service", consumer, "api")


class TestGetRequiredConsumers:
    def test_returns_consumers_pointing_to_provider(self, conn, change):
        _seed_graph(conn, change["id"], ["checkout", "fraud"])
        consumers = get_required_consumers(conn, change["id"])
        assert set(consumers) == {"checkout", "fraud"}

    def test_ignores_edges_not_to_provider(self, conn, change):
        # Edge from some-other-service to checkout does not originate at the provider.
        ledger.add_dependency(conn, change["id"], "checkout", "some-other-service", "api")
        consumers = get_required_consumers(conn, change["id"])
        assert consumers == []

    def test_no_edges_returns_empty(self, conn, change):
        assert get_required_consumers(conn, change["id"]) == []

    def test_includes_transitive_consumers(self, conn, change):
        ledger.add_dependency(
            conn, change["id"], "account-service", "event-router", "event"
        )
        ledger.add_dependency(
            conn, change["id"], "event-router", "warehouse", "event"
        )
        assert get_required_consumers(conn, change["id"]) == [
            "event-router",
            "warehouse",
        ]


class TestEvaluateGate:
    def test_all_verified_returns_verified(self, conn, change):
        """Critical case 1: all consumers verified → VERIFIED."""
        _seed_graph(conn, change["id"], ["checkout", "fraud", "analytics-worker"])
        for consumer in ["checkout", "fraud", "analytics-worker"]:
            ledger.upsert_consumer_migration(conn, change["id"], consumer, "verified")

        decision = evaluate_gate(conn, change["id"])
        assert decision.result == "VERIFIED"
        assert decision.unresolved == []
        assert set(decision.required_consumers) == {"checkout", "fraud", "analytics-worker"}

    def test_one_unresolved_returns_not_proven_safe(self, conn, change):
        """Critical case 2: one consumer unresolved → NOT_PROVEN_SAFE."""
        _seed_graph(conn, change["id"], ["checkout", "fraud"])
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "verified")
        ledger.upsert_consumer_migration(conn, change["id"], "fraud", "in_progress")

        decision = evaluate_gate(conn, change["id"])
        assert decision.result == "NOT_PROVEN_SAFE"
        assert "fraud" in decision.unresolved

    def test_no_migration_record_returns_not_proven_safe(self, conn, change):
        """Critical case 3: consumer has no migration row → NOT_PROVEN_SAFE."""
        _seed_graph(conn, change["id"], ["checkout"])
        # No consumer_migration row for checkout at all

        decision = evaluate_gate(conn, change["id"])
        assert decision.result == "NOT_PROVEN_SAFE"
        assert "checkout" in decision.unresolved

    def test_no_consumers_in_graph_returns_not_proven_safe(self, conn, change):
        """Critical case 4: empty graph → NOT_PROVEN_SAFE."""
        decision = evaluate_gate(conn, change["id"])
        assert decision.result == "NOT_PROVEN_SAFE"
        assert "No required consumers" in decision.reason

    def test_failed_consumer_is_unresolved(self, conn, change):
        """A consumer with status='failed' must block the gate."""
        _seed_graph(conn, change["id"], ["checkout"])
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "failed")

        decision = evaluate_gate(conn, change["id"])
        assert decision.result == "NOT_PROVEN_SAFE"
        assert "checkout" in decision.unresolved

    def test_analytics_worker_discovered_blocks_gate_until_verified(self, conn, change):
        """
        analytics-worker must be discovered and verified — it cannot be
        silently skipped just because it is undocumented.
        """
        _seed_graph(conn, change["id"], ["checkout", "fraud"])
        ledger.add_dependency(
            conn, change["id"], "account-service", "analytics-worker", "event",
            documentation_status="undocumented",
        )
        # Only checkout and fraud verified, analytics-worker missing
        ledger.upsert_consumer_migration(conn, change["id"], "checkout", "verified")
        ledger.upsert_consumer_migration(conn, change["id"], "fraud", "verified")

        decision = evaluate_gate(conn, change["id"])
        assert decision.result == "NOT_PROVEN_SAFE"
        assert "analytics-worker" in decision.unresolved


class TestBuildGraph:
    def test_nodes_and_edges_present(self, conn, change):
        _seed_graph(conn, change["id"], ["checkout", "fraud"])
        graph = build_graph(conn, change["id"])
        node_ids = {n["id"] for n in graph["nodes"]}
        assert "checkout" in node_ids
        assert "account-service" in node_ids
        assert len(graph["edges"]) == 2

    def test_empty_graph(self, conn, change):
        graph = build_graph(conn, change["id"])
        assert graph["nodes"] == []
        assert graph["edges"] == []

    def test_edge_has_type(self, conn, change):
        ledger.add_dependency(
            conn, change["id"], "account-service", "analytics-worker", "event",
            "source scan", "undocumented",
        )
        graph = build_graph(conn, change["id"])
        edge = graph["edges"][0]
        assert edge["edge_type"] == "event"
        assert edge["documentation_status"] == "undocumented"
        assert edge["reason"] == "source scan"
