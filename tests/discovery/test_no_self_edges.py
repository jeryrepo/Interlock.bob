"""
tests/discovery/test_no_self_edges.py
=======================================
Cross-agent invariants for discovery dependency edges:

  1. No self-referencing edges: from_component must never equal to_component.
     A self-edge causes a cycle in the planning phase's topological sort.

  2. Correct direction: every edge emitted for the canonical demo change
     (provider = "account-service") must have to_component == "account-service".
     Canonical direction is consumer -> provider: a component DEPENDS ON the
     provider, so the edge points from the consumer toward the provider.
     Emitting the edge in the reverse direction (provider -> consumer) means
     gate.py's get_required_consumers() — which filters to_component == PROVIDER
     — will find zero consumers and always return NOT_PROVEN_SAFE.

Parametrized over all four real discovery agents.
"""
from __future__ import annotations

import pytest

from agents.discovery import api_contract, db_schema, event_contract, repo_map

_DISCOVERY_AGENTS = [
    ("repo-map", repo_map.run),
    ("api-contract-discovery", api_contract.run),
    ("event-contract-discovery", event_contract.run),
    ("db-schema-discovery", db_schema.run),
]

_PROVIDER = "account-service"


@pytest.mark.parametrize("agent_name,agent_fn", _DISCOVERY_AGENTS)
def test_no_self_referencing_dependency_edge(agent_name, agent_fn, base_data):
    """
    No discovery agent may produce a dependency edge where
    from_component == to_component for any consumer.

    A component cannot depend on itself — such edges cause cycle detection
    failures in the planning phase.
    """
    result = agent_fn(base_data)
    dependencies = result.get("dependencies", [])

    self_edges = [
        dep for dep in dependencies
        if dep["from_component"] == dep["to_component"]
    ]

    assert not self_edges, (
        f"Agent '{agent_name}' emitted {len(self_edges)} self-referencing "
        f"dependency edge(s):\n"
        + "\n".join(
            f"  from={d['from_component']!r} -> to={d['to_component']!r} "
            f"(edge_type={d.get('edge_type')!r}, reason={d.get('reason')!r})"
            for d in self_edges
        )
    )


@pytest.mark.parametrize("agent_name,agent_fn", _DISCOVERY_AGENTS)
def test_all_edges_point_provider_to_consumer(agent_name, agent_fn, base_data):
    """
    For every dependency edge emitted where one endpoint is the provider
    (account-service), the edge must be directed provider -> consumer:
      from_component = "account-service"
      to_component   = <consumer>

    Emitting the edge in the reverse direction (consumer -> account-service)
    means gate.py's get_required_consumers() — which collects to_component
    where from_component == PROVIDER — will find zero consumers and always
    return NOT_PROVEN_SAFE, even after every consumer is genuinely verified.
    """
    result = agent_fn(base_data)
    dependencies = result.get("dependencies", [])

    # Edges where one side is the provider
    provider_edges = [
        dep for dep in dependencies
        if dep["from_component"] == _PROVIDER or dep["to_component"] == _PROVIDER
    ]

    reversed_edges = [
        dep for dep in provider_edges
        if dep["to_component"] == _PROVIDER
    ]

    assert not reversed_edges, (
        f"Agent '{agent_name}' emitted {len(reversed_edges)} reversed edge(s) "
        f"(provider is to_component — should be from_component):\n"
        + "\n".join(
            f"  from={d['from_component']!r} -> to={d['to_component']!r} "
            f"(edge_type={d.get('edge_type')!r})"
            for d in reversed_edges
        )
    )
