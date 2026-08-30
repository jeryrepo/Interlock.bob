"""
orchestrator/gate.py
=====================
Deterministic safety gate for Interlock.

evaluate_gate() is pure read-only Python — zero LLM involvement.
The critic agent CANNOT override the result of this function.

Canonical edge direction: consumer -> provider.  A dependency_edge row
reads ``from_component = checkout``, ``to_component = account-service``.
This matches what the discovery agents emit and what the planning agent
consumes via ``nx.ancestors(provider)``.

Gate logic:
  1. Find all consumers that depend on the provider via dependency_edge rows.
     The provider comes from the change's spec, falling back to PROVIDER for
     legacy description-only changes.
  2. Look up which work-item step kinds this change kind requires
     (_REQUIRED_STEP_KINDS) — one for a field rename, two for a transport
     migration.
  3. Every (consumer, step_kind) pair must have a work_item row with
     status == "verified".
  4. Any pair missing a row, or in any other status → NOT_PROVEN_SAFE.
  5. All verified → VERIFIED.

Per-kind variation is data, never a dispatch table of predicates: the logic
above is the same for every change kind. See ADR-0002.

build_graph() derives the NetworkX graph from dependency_edge rows on
every call — no graph state is stored between requests.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

import networkx as nx
from pydantic import BaseModel

import orchestrator.ledger as ledger

# Fallback provider, used only for legacy changes created before ChangeSpec
# existed (no change_spec row).  AGENTS.md invariant 6 blesses this as the one
# deliberate component constant; every change carrying a spec supplies its own.
PROVIDER = "account-service"

# ---------------------------------------------------------------------------
# Gate policy.  Data, not callables — see ADR-0002.
#
# Per-kind variation is expressed ONLY as the set of work-item step kinds each
# component must have verified.  The predicate below does not change per kind,
# which is what keeps "the gate lives in exactly one place" verifiable by
# reading one function.
#
# Policy lives here and not on the spec on purpose: the spec arrives in a
# request body, so a spec-supplied requirement set could be weakened by whoever
# writes the spec.  The spec supplies nouns; the gate owns policy; the ledger
# supplies facts.
# ---------------------------------------------------------------------------

_REQUIRED_STEP_KINDS: dict[str, tuple[str, ...]] = {
    "field_rename": ("migrate",),
    "api_contract_change": ("migrate",),
    # A subscriber is safe only when it has moved AND the retired webhook has
    # drained.  Proving quiescence is an agent's job; the gate just counts.
    "transport_migration": ("subscribe", "webhook_quiet"),
}

_DEFAULT_STEP_KINDS: tuple[str, ...] = ("migrate",)

# Steps the PROVIDER itself must have verified, per change kind.
#
# Without this the gate could report VERIFIED while the provider patch had
# failed: consumers would have been migrated to a field the provider never
# gained.  Legacy description-only changes resolve to an empty tuple, so their
# verdicts are unchanged.
# The coexistence rehearsal proves the property no per-component contract test
# can: that ONE running provider serves the old and new shapes simultaneously,
# which is what lets consumers cut over one at a time. It is a provider step
# because it is the provider that must hold both contracts open.
#
# It is required here because the gate counts work items and never reads
# evidence. The rehearsal previously wrote only evidence, so a rehearsal that
# failed — or never ran at all — left the gate's verdict completely unchanged.
REHEARSAL_STEP_KIND = "coexistence_rehearsal"

_REQUIRED_PROVIDER_STEPS: dict[str, tuple[str, ...]] = {
    "field_rename": ("provider_patch", REHEARSAL_STEP_KIND),
    "api_contract_change": ("provider_patch", REHEARSAL_STEP_KIND),
    "transport_migration": ("provider_patch", REHEARSAL_STEP_KIND),
}


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

def _resolve_provider(conn: sqlite3.Connection, change_id: str) -> str:
    """
    The provider for this change: from its spec, or the legacy constant.

    Reading `spec.provider` is safe — it is a noun, not policy.
    """
    spec_row = ledger.get_change_spec(conn, change_id)
    if spec_row is None:
        return PROVIDER
    return spec_row["spec"].get("provider") or PROVIDER


def _resolve_step_kinds(conn: sqlite3.Connection, change_id: str) -> tuple[str, ...]:
    """
    Which work-item step kinds every required component must have verified.

    Unknown kinds resolve to the strictest known set, never an empty one — this
    fails closed.  A dispatch table's natural failure mode is a KeyError or a
    permissive fallthrough; this cannot express "no requirement".
    """
    spec_row = ledger.get_change_spec(conn, change_id)
    if spec_row is None:
        return _DEFAULT_STEP_KINDS
    return _REQUIRED_STEP_KINDS.get(spec_row["kind"], _DEFAULT_STEP_KINDS)


def _resolve_provider_steps(conn: sqlite3.Connection, change_id: str) -> tuple[str, ...]:
    """
    Steps the provider must have verified.  Empty for legacy no-spec changes,
    which is what keeps their gate output byte-identical.
    """
    spec_row = ledger.get_change_spec(conn, change_id)
    if spec_row is None:
        return ()
    return _REQUIRED_PROVIDER_STEPS.get(spec_row["kind"], ())


def get_required_consumers(conn: sqlite3.Connection, change_id: str) -> list[str]:
    """
    Return sorted list of component names that depend on the provider.

    Canonical edge direction: consumer -> provider.
        from_component = consumer  (the component that depends on the field)
        to_component   = provider  (the component that exposes the field)

    e.g. ``checkout -> account-service``.  Consumers are therefore read off
    the ``from_component`` end of edges whose ``to_component`` is the provider.
    """
    provider = _resolve_provider(conn, change_id)
    deps = ledger.get_dependencies(conn, change_id)
    consumers = sorted(
        {d["to_component"] for d in deps if d["from_component"] == provider}
    )
    return consumers


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

    step_kinds = _resolve_step_kinds(conn, change_id)

    # Every (component, step_kind) pair required by this change must have a
    # work_item row in status "verified".  A missing row and a row in any other
    # status are both unresolved: absence of proof is not proof.
    verified: set[tuple[str, str]] = {
        (w["component"], w["step_kind"])
        for w in ledger.get_work_items(conn, change_id)
        if w["status"] == "verified"
    }

    # Only qualify names when a kind actually requires more than one step, so
    # single-step kinds produce exactly the strings they always have.
    qualify = len(step_kinds) > 1

    unresolved: list[str] = []

    # The provider's own work first: if it never gained the new field, nothing
    # downstream is safe regardless of how many consumers migrated.
    provider = _resolve_provider(conn, change_id)
    for step_kind in _resolve_provider_steps(conn, change_id):
        if (provider, step_kind) not in verified:
            unresolved.append(f"{provider}:{step_kind}")

    for consumer in required:
        for step_kind in step_kinds:
            if (consumer, step_kind) not in verified:
                unresolved.append(
                    f"{consumer}:{step_kind}" if qualify else consumer
                )

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
            reason=dep.get("reason") or "",
        )

    nodes = [{"id": n, "label": n} for n in G.nodes()]
    edges = [
        {
            "from": u,
            "to": v,
            "edge_type": data.get("edge_type", ""),
            "reason": data.get("reason", ""),
        }
        for u, v, data in G.edges(data=True)
    ]

    return {"nodes": nodes, "edges": edges}
