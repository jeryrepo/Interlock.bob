"""tests/orchestrator/test_graph.py

Tests for gate.build_graph — correct node/edge derivation from ledger rows.
"""
import orchestrator.ledger as ledger
from orchestrator.gate import build_graph


class TestBuildGraph:
    def test_single_edge_produces_two_nodes(self, conn, change):
        ledger.add_dependency(conn, change["id"], "checkout", "account-service", "api")
        graph = build_graph(conn, change["id"])
        node_ids = {n["id"] for n in graph["nodes"]}
        assert "checkout" in node_ids
        assert "account-service" in node_ids
        assert len(graph["edges"]) == 1

    def test_edge_fields(self, conn, change):
        ledger.add_dependency(
            conn, change["id"], "fraud", "account-service", "api", "risk scoring"
        )
        graph = build_graph(conn, change["id"])
        edge = graph["edges"][0]
        assert edge["from"] == "fraud"
        assert edge["to"] == "account-service"
        assert edge["edge_type"] == "api"
        assert edge["reason"] == "risk scoring"

    def test_multiple_edges_correct_count(self, conn, change):
        ledger.add_dependency(conn, change["id"], "checkout", "account-service", "api")
        ledger.add_dependency(conn, change["id"], "fraud", "account-service", "api")
        ledger.add_dependency(conn, change["id"], "analytics-worker", "account-service", "undocumented")
        graph = build_graph(conn, change["id"])
        assert len(graph["nodes"]) == 4  # checkout, fraud, analytics-worker, account-service
        assert len(graph["edges"]) == 3

    def test_undocumented_edge_preserved(self, conn, change):
        ledger.add_dependency(
            conn, change["id"], "analytics-worker", "account-service",
            "undocumented", "source code scan"
        )
        graph = build_graph(conn, change["id"])
        undoc_edges = [e for e in graph["edges"] if e["edge_type"] == "undocumented"]
        assert len(undoc_edges) == 1
        assert undoc_edges[0]["from"] == "analytics-worker"

    def test_empty_returns_empty_collections(self, conn, change):
        graph = build_graph(conn, change["id"])
        assert graph["nodes"] == []
        assert graph["edges"] == []

    def test_no_duplicate_nodes_for_shared_dependency(self, conn, change):
        """Two consumers depending on the same provider should not duplicate that node."""
        ledger.add_dependency(conn, change["id"], "checkout", "account-service", "api")
        ledger.add_dependency(conn, change["id"], "fraud", "account-service", "api")
        graph = build_graph(conn, change["id"])
        node_ids = [n["id"] for n in graph["nodes"]]
        assert node_ids.count("account-service") == 1

    def test_node_has_id_and_label(self, conn, change):
        ledger.add_dependency(conn, change["id"], "checkout", "account-service", "api")
        graph = build_graph(conn, change["id"])
        for node in graph["nodes"]:
            assert "id" in node
            assert "label" in node
            assert node["id"] == node["label"]

    def test_graph_rebuilt_fresh_each_call(self, conn, change):
        """build_graph must reflect the current ledger state on every call."""
        ledger.add_dependency(conn, change["id"], "checkout", "account-service", "api")
        graph1 = build_graph(conn, change["id"])
        assert len(graph1["nodes"]) == 2

        ledger.add_dependency(conn, change["id"], "fraud", "account-service", "api")
        graph2 = build_graph(conn, change["id"])
        assert len(graph2["nodes"]) == 3  # one more node added
