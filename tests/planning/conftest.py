"""
Shared mock-data builders for tests/planning/.

All inputs are plain dicts matching the shared contract shapes from
docs/prompts/00_SHARED_TEAM_CONTRACT.md — no Pydantic imports.

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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def three_consumer_input():
    """
    account-service is the provider.
    Three consumers: svc-a (api), svc-b (event), svc-c (undocumented).
    No db consumers. No hardcoded Interlock names.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("svc-a", "account-service", "api"),
        dep("svc-b", "account-service", "event"),
        dep("svc-c", "account-service", "undocumented"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def with_db_consumer_input():
    """
    Three api consumers + one db consumer (should be sorted last).
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("svc-a", "account-service", "api"),
        dep("svc-b", "account-service", "event"),
        dep("platform-cfg", "account-service", "db"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def unrelated_component_input():
    """
    svc-a depends on account-service; svc-x depends on some-other-service only.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("svc-a", "account-service", "api"),
        dep("svc-x", "some-other-service", "api"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def cyclic_input():
    """
    svc-a -> account-service -> svc-a creates a cycle.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("svc-a", "account-service", "api"),
        dep("account-service", "svc-a", "api"),   # cycle
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def no_analytics_worker_input():
    """
    Graph deliberately contains NO analytics-worker node.
    Used to prove strategy does not hardcode that name.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("svc-alpha", "account-service", "api"),
        dep("svc-beta",  "account-service", "event"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def arbitrary_names_input():
    """
    Entirely arbitrary service names — would break any hardcoding immediately.
    """
    cr = make_cr(
        provider="zeta-core",
        old_field="legacy_ref",
        new_field="modern_ref",
    )
    deps = [
        dep("omega-ui",    "zeta-core", "api"),
        dep("gamma-worker","zeta-core", "event"),
        dep("delta-cfg",   "zeta-core", "db"),
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}


@pytest.fixture
def no_consumers_input():
    """
    Provider exists in graph but nothing depends on it.
    """
    cr = make_cr(provider="account-service")
    deps = [
        dep("account-service", "some-db", "db"),  # provider depends on something, nothing depends on provider
    ]
    return {"change_request": cr, "dependencies": deps, "evidence": []}
