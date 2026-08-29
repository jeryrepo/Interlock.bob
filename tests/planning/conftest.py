"""
Shared mock-data builders for tests/planning/.

All inputs are plain dicts matching the shared contract shapes from
docs/prompts/00_SHARED_TEAM_CONTRACT.md — no Pydantic imports.

Canonical edge direction:
  from_component = provider  (the node that exposes the field)
  to_component   = consumer  (the node that depends on the changing field)

Example: account-service -> checkout
  dep("account-service", "checkout", "api")

Literal value sets (from contract):
  edge_type  : "api" | "event" | "db" | "undocumented"
  claim_type : "dependency" | "migration_status" | "test_result" | "risk"
  confidence : "hypothesis" | "confirmed" | "refuted"
"""

from __future__ import annotations
import pytest


# ---------------------------------------------------------------------------
# Change-request builders
# ---------------------------------------------------------------------------

def make_cr(
    provider: str = "account-service",
    old_field: str = "customer_id",
    new_field: str = "account_id",
    cr_id: str = "cr-001",
) -> dict:
    return {
        "id": cr_id,
        "old_field": old_field,
        "new_field": new_field,
        "provider": provider,
    }


# ---------------------------------------------------------------------------
# Dependency builders
# ---------------------------------------------------------------------------

def dep(
    from_component: str,
    to_component: str,
    edge_type: str = "api",
    reason: str | None = None,
) -> dict:
    return {
        "from_component": from_component,
        "to_component": to_component,
        "edge_type": edge_type,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Fixtures — all use canonical direction: provider -> consumer
# ---------------------------------------------------------------------------

@pytest.fixture
def three_consumer_input():
    """
    account-service is the provider.
    Three consumers: svc-a (api), svc-b (event), svc-c (undocumented).
    Canonical edges: account-service -> svc-a / svc-b / svc-c
    No db consumers. No hardcoded Interlock names.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service", "svc-a", "api"),
        dep("account-service", "svc-b", "event"),
        dep("account-service", "svc-c", "undocumented"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def with_db_consumer_input():
    """
    Two api/event consumers + one db consumer (should be sorted last).
    Canonical edges: svc-a / svc-b / platform-cfg -> account-service
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service", "svc-a",        "api"),
        dep("account-service", "svc-b",        "event"),
        dep("account-service", "platform-cfg", "db"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def unrelated_component_input():
    """
    svc-a is a consumer of account-service.
    svc-x is a consumer of some-other-service only (no path to account-service).
    Canonical edges: svc-a -> account-service, svc-x -> some-other-service
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service",    "svc-a", "api"),
        dep("some-other-service", "svc-x", "api"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def cyclic_input():
    """
    svc-a -> account-service -> svc-a creates a cycle.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service", "svc-a",          "api"),
        dep("svc-a",          "account-service", "api"),   # cycle back
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def no_analytics_worker_input():
    """
    Graph deliberately contains NO analytics-worker node.
    Used to prove strategy does not hardcode that name.
    Canonical edges: svc-alpha / svc-beta -> account-service
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service", "svc-alpha", "api"),
        dep("account-service", "svc-beta",  "event"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def arbitrary_names_input():
    """
    Entirely arbitrary service names — would break any hardcoding immediately.
    Canonical edges: omega-ui / gamma-worker / delta-cfg -> zeta-core
    """
    cr = make_cr(
        provider="zeta-core",
        old_field="legacy_ref",
        new_field="modern_ref",
    )
    deps = [
        dep("zeta-core", "omega-ui",     "api"),
        dep("zeta-core", "gamma-worker", "event"),
        dep("zeta-core", "delta-cfg",    "db"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def no_consumers_input():
    """
    Provider exists but has no consumers pointing to it.
    Here account-service appears only as a consumer, so it has no downstream
    consumers and nx.descendants(g, "account-service") is empty.
    """
    cr = make_cr(provider="account-service")
    deps = [
        # account-service is from_component here — meaning account-service
        # is the consumer in this edge, not the provider.  This gives
        # account-service a graph node but zero in-edges (no consumers
        # pointing at it), so nx.ancestors returns empty.
        dep("some-upstream-svc", "account-service", "api"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def canonical_transitive_input():
    """
    Canonical regression test (consumer -> provider direction):
      account-service -> service-alpha  (direct consumer)
      account-service -> service-beta   (direct consumer)
      service-beta    -> service-gamma  (transitive consumer via service-beta)

    Expected affected consumers: service-alpha, service-beta, service-gamma
    Expected order: service-gamma before service-beta
                    (service-gamma depends on service-beta, so service-gamma
                    must migrate first, then service-beta, then the provider
                    can drop the old field)
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service", "service-alpha", "api"),
        dep("account-service", "service-beta",  "event"),
        dep("service-beta",    "service-gamma", "undocumented"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}
