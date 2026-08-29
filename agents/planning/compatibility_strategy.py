"""
compatibility_strategy — Planning agent for Interlock.

Derives a safe migration order (DAG) from discovery evidence.
Returns a plain dict; never writes SQLite; never calls other agents.

# ---------------------------------------------------------------------------
# SCHEMA INTEGRATION POINT
# When orchestrator/schemas/ (Person 1) is available, replace the TypedDict
# definitions below with imports from:
#
#   from orchestrator.schemas.planning import (
#       CompatibilityStrategyInput,
#       CompatibilityStrategyResult,
#       MigrationStep,
#   )
#   from orchestrator.schemas.common import Evidence, Dependency, ChangeRequest
#
# The public run(data: dict) -> dict signature stays unchanged; the orchestrator
# validates data before calling this function and validates the return value.
# ---------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any, TypedDict

import networkx as nx


# ---------------------------------------------------------------------------
# Internal TypedDicts (documentation only — not runtime-validated)
# ---------------------------------------------------------------------------

class _ChangeRequest(TypedDict):
    id: str
    old_field: str
    new_field: str
    provider: str


class _Dependency(TypedDict):
    from_component: str
    to_component: str
    edge_type: str   # "api" | "event" | "db" | "undocumented"
    reason: str | None


class _Evidence(TypedDict):
    claim_type: str   # "dependency" | "migration_status" | "test_result" | "risk"
    subject: str
    content: dict
    source_ref: str
    confidence: str   # "hypothesis" | "confirmed" | "refuted"
    source_revision: str | None


class _MigrationStep(TypedDict):
    component: str
    action: str
    depends_on: list[str]
    rationale: str


class _StrategyResult(TypedDict):
    affected_consumers: list[str]
    migration_steps: list[_MigrationStep]
    compatibility_requirements: list[str]
    verification_requirements: list[str]
    evidence: list[_Evidence]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(data: dict[str, Any]) -> dict[str, Any]:
    """
    Derive a safe migration plan from discovery evidence.

    Parameters
    ----------
    data : dict with keys:
        - change_request: dict matching _ChangeRequest shape
        - dependencies:   list of dicts matching _Dependency shape
        - evidence:       list of dicts matching _Evidence shape (may be empty)

    Returns
    -------
    dict matching _StrategyResult shape

    Raises
    ------
    ValueError  if dependencies are missing, the provider is absent from the
                dependency graph, or the graph contains a cycle.
    """
    cr: dict = data["change_request"]
    deps: list[dict] = data.get("dependencies") or []
    incoming_evidence: list[dict] = data.get("evidence") or []

    provider: str = cr["provider"]
    old_field: str = cr["old_field"]
    new_field: str = cr["new_field"]

    if not deps:
        raise ValueError(
            "No dependencies supplied — cannot derive migration order. "
            "Ensure discovery has run before calling compatibility-strategy."
        )

    # ------------------------------------------------------------------
    # Build directed graph
    # Canonical contract: consumer -> provider
    #   from_component = consumer  (the node that depends on the field)
    #   to_component   = provider  (the node that exposes the field)
    # So g.add_edge(from_component, to_component) produces edges that
    # flow FROM consumers TOWARD the provider:
    #   checkout         -> account-service
    #   fraud            -> account-service
    #   analytics-worker -> account-service
    # Upstream consumers are therefore reachable via nx.ancestors.
    # ------------------------------------------------------------------
    g: nx.DiGraph = nx.DiGraph()
    db_edge_sources: set[str] = set()

    for dep in deps:
        src = dep["from_component"]
        dst = dep["to_component"]
        g.add_edge(src, dst)
        if dep.get("edge_type") == "db":
            db_edge_sources.add(src)   # consumer (from_component) is the db-edge dependent

    # ------------------------------------------------------------------
    # Validate: provider must appear as a node
    # ------------------------------------------------------------------
    if provider not in g.nodes:
        raise ValueError(
            f"Provider '{provider}' not found in the dependency graph. "
            f"Known nodes: {sorted(g.nodes)}."
        )

    # ------------------------------------------------------------------
    # Detect cycles before any topological work
    # ------------------------------------------------------------------
    if not nx.is_directed_acyclic_graph(g):
        cycles = list(nx.simple_cycles(g))
        raise ValueError(
            f"Dependency graph contains a cycle — cannot derive safe migration order. "
            f"Cycles detected: {cycles}."
        )

    # ------------------------------------------------------------------
    # Find all consumers: nodes that can REACH the provider via directed edges.
    # With canonical edges (consumer -> provider), nx.ancestors(g, provider)
    # returns every component that directly or transitively depends on it.
    # ------------------------------------------------------------------
    consumers: set[str] = nx.ancestors(g, provider)

    if not consumers:
        # Provider exists but nothing depends on it — plan is trivially provider-only.
        consumers = set()

    # ------------------------------------------------------------------
    # Sort consumers topologically.
    # With canonical edges (consumer -> ... -> provider), we need consumers
    # to be migrated in dependency order: a consumer that another consumer
    # depends on (transitively toward the provider) must come first.
    # nx.topological_sort on the consumer-to-provider graph puts the most
    # upstream consumers (closest to the provider) LAST.  We reverse this
    # to get "direct consumers before their own dependents" ordering.
    # ------------------------------------------------------------------
    full_topo = list(nx.topological_sort(g))  # most-upstream consumers last
    consumer_order = [n for n in full_topo if n in consumers]

    # Move db-edge sources to the end (platform-config pattern)
    non_db = [c for c in consumer_order if c not in db_edge_sources]
    db_last = [c for c in consumer_order if c in db_edge_sources]
    consumer_order = non_db + db_last

    # ------------------------------------------------------------------
    # Build migration steps
    # ------------------------------------------------------------------
    steps: list[_MigrationStep] = []

    # Step 0: provider patch
    steps.append({
        "component": provider,
        "action": (
            f"Add '{new_field}' field to API responses while retaining '{old_field}'. "
            "Both fields must coexist until all consumers have migrated."
        ),
        "depends_on": [],
        "rationale": (
            f"Provider must expose both '{old_field}' and '{new_field}' during the "
            "compatibility window so consumers can migrate independently."
        ),
    })

    prev: str = provider
    for consumer in consumer_order:
        steps.append({
            "component": consumer,
            "action": (
                f"Migrate '{old_field}' references to '{new_field}'. "
                "Update source code, tests, and any configuration."
            ),
            "depends_on": [prev],
            "rationale": (
                f"'{consumer}' depends (directly or transitively) on '{provider}'. "
                f"It must switch to '{new_field}' after the provider compatibility window opens."
            ),
        })
        prev = consumer

    # ------------------------------------------------------------------
    # Compatibility requirements
    # ------------------------------------------------------------------
    compatibility_requirements: list[str] = [
        (
            f"'{provider}' must expose BOTH '{old_field}' AND '{new_field}' in all "
            "API/event payloads for the duration of the migration window."
        ),
        (
            f"No consumer may be considered migrated until its test suite passes "
            f"against '{new_field}' exclusively."
        ),
        (
            f"Legacy field '{old_field}' must not be removed until every affected "
            "consumer is verified."
        ),
    ]

    # ------------------------------------------------------------------
    # Verification requirements — one per consumer
    # ------------------------------------------------------------------
    verification_requirements: list[str] = []
    for consumer in consumer_order:
        verification_requirements.append(
            f"'{consumer}': must have a passing test_result evidence item confirming "
            f"'{new_field}' is used and '{old_field}' is absent from its source."
        )

    # ------------------------------------------------------------------
    # Evidence — one entry per consumer, citing the dependency source
    # ------------------------------------------------------------------
    # Canonical edge direction: consumer is from_component, provider is to_component.
    dep_lookup: dict[tuple[str, str], dict] = {
        (d["from_component"], d["to_component"]): d for d in deps
    }
    result_evidence: list[_Evidence] = []
    for consumer in consumer_order:
        # Prefer the direct edge from consumer to provider; fall back to any
        # edge whose from_component is this consumer (covers transitive cases).
        direct_key = (consumer, provider)
        dep_for_consumer = dep_lookup.get(direct_key)
        if dep_for_consumer is None:
            dep_for_consumer = next(
                (d for d in deps if d["from_component"] == consumer), None
            )
        source_ref = (
            dep_for_consumer.get("reason") or f"dependency:{provider}->{consumer}"
            if dep_for_consumer else f"dependency:{provider}->{consumer}"
        )
        result_evidence.append({
            "claim_type": "dependency",
            "subject": consumer,
            "content": {
                "migration_step": f"{consumer} must migrate {old_field} -> {new_field}",
                "dependency_edge": dep_for_consumer or {},
            },
            "source_ref": source_ref,
            "confidence": "confirmed",
            "source_revision": None,
        })

    # ------------------------------------------------------------------
    # Merge any incoming evidence alongside our derived evidence
    # ------------------------------------------------------------------
    all_evidence = list(incoming_evidence) + result_evidence

    affected_consumers = list(consumer_order)

    return {
        "affected_consumers": affected_consumers,
        "migration_steps": steps,
        "compatibility_requirements": compatibility_requirements,
        "verification_requirements": verification_requirements,
        "evidence": all_evidence,
    }
