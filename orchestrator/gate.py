"""
orchestrator/gate.py
=====================
Deterministic safety gate for Interlock.

evaluate_gate() is pure read-only Python — zero LLM involvement.
The critic agent CANNOT override the result of this function.

Canonical edge direction: provider -> consumer.  A dependency_edge row
reads ``from_component = account-service``, ``to_component = checkout``.

Gate logic:
  1. Find every direct and transitive consumer reachable from the provider.
  2. Every required consumer must have a consumer_migration row with
     status == "verified".
  3. Any consumer missing a row → NOT_PROVEN_SAFE.
  4. Any consumer with status != "verified" → NOT_PROVEN_SAFE.
  5. All verified → VERIFIED.

build_graph() derives the NetworkX graph from dependency_edge rows on
every call — no graph state is stored between requests.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

import networkx as nx
from pydantic import BaseModel

import orchestrator.ledger as ledger

# The canonical provider name.  Consumers of this component are the ones
# whose migration status the gate inspects.
PROVIDER = "account-service"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class GateDecision(BaseModel):
    result: Literal["VERIFIED", "NOT_PROVEN_SAFE"]
    reason: str
    required_consumers: list[str]
    unresolved: list[str]   # consumers that blocked the gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_required_consumers(conn: sqlite3.Connection, change_id: str) -> list[str]:
    """
    Return sorted list of component names that depend on the provider.

    Canonical edge direction: provider -> consumer.

    e.g. ``account-service -> checkout``.
    """
    deps = ledger.get_dependencies(conn, change_id)
    graph = nx.DiGraph(
        (dependency["from_component"], dependency["to_component"])
        for dependency in deps
    )
    if PROVIDER not in graph:
        return []
    return sorted(nx.descendants(graph, PROVIDER))


# ---------------------------------------------------------------------------
# Gate evaluation (read-only)
# ---------------------------------------------------------------------------

def evaluate_gate(conn: sqlite3.Connection, change_id: str) -> GateDecision:
    """
    Evaluate whether all required consumers are fully migrated.

    This function is side-effect free — it only reads the ledger.
    The caller is responsible for writing the gate_decision record.
    """
    required = get_required_consumers(conn, change_id)

    if not required:
        # No consumers discovered — gate cannot confirm safety.
        return GateDecision(
            result="NOT_PROVEN_SAFE",
            reason="No required consumers found in dependency graph.",
            required_consumers=[],
            unresolved=[],
        )

    migrations_by_consumer: dict[str, str] = {
        m["consumer"]: m["status"]
        for m in ledger.get_consumer_migrations(conn, change_id)
    }

    unresolved: list[str] = []
    for consumer in required:
        status = migrations_by_consumer.get(consumer)
        if status is None:
            unresolved.append(consumer)
        elif status != "verified":
            unresolved.append(consumer)

    if unresolved:
        return GateDecision(
            result="NOT_PROVEN_SAFE",
            reason=(
                f"The following consumers are not verified: "
                f"{', '.join(unresolved)}"
            ),
            required_consumers=required,
            unresolved=unresolved,
        )

    return GateDecision(
        result="VERIFIED",
        reason="All required consumers have been verified.",
        required_consumers=required,
        unresolved=[],
    )


# ---------------------------------------------------------------------------
# Graph derivation
# ---------------------------------------------------------------------------

def build_graph(conn: sqlite3.Connection, change_id: str) -> dict:
    """
    Build a NetworkX DiGraph from dependency_edge rows and return a
    serialisable dict with 'nodes' and 'edges' suitable for pyvis/Streamlit.

    The graph is built fresh on every call — no state is cached.
    """
    deps = ledger.get_dependencies(conn, change_id)

    G = nx.DiGraph()
    for dep in deps:
        G.add_node(dep["from_component"])
        G.add_node(dep["to_component"])
        G.add_edge(
            dep["from_component"],
            dep["to_component"],
            edge_type=dep["edge_type"],
            documentation_status=dep.get("documentation_status", "documented"),
            reason=dep.get("reason") or "",
        )

    nodes = [{"id": n, "label": n} for n in G.nodes()]
    edges = [
        {
            "from": u,
            "to": v,
            "edge_type": data.get("edge_type", ""),
            "documentation_status": data.get("documentation_status", "documented"),
            "reason": data.get("reason", ""),
        }
        for u, v, data in G.edges(data=True)
    ]

    return {"nodes": nodes, "edges": edges}
