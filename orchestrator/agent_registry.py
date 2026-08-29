"""
orchestrator/agent_registry.py
===============================
Which real agents run, for which change kind, in which phase.

Replaces the inline literal lists in `agent_runner.py` with declared data, so
adding a change kind is a registry entry rather than a code edit to the runner.

Two design points worth keeping:

**Lazy imports.** `AgentSpec` holds an `import_path` string, not a function
reference. The stub path therefore never imports `agents/` at all, and there is
no import-order question between `orchestrator` and `agents`.

**Signature normalisation.** The agents disagree about their signatures —
`run(data)`, `run(data, repo_path)`, `run(data, base_url)` — while
`AgentRunner.run(context)` passes a single dict. Rather than widen `AgentRunner`
or rewrite the agents, `_bind()` inspects each `run` and passes only the extra
arguments it actually declares. Eight lines, no per-agent configuration, and it
fails loudly with a TypeError if a signature drifts.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

import orchestrator.adapters as ad
from orchestrator.schemas import (
    DiscoveryResult,
    ImplementationResult,
    PlanningResult,
    VerificationResult,
)


@dataclass(frozen=True)
class AgentSpec:
    """One runnable agent, described rather than imported."""

    role: str
    import_path: str
    output_schema: type[BaseModel]
    adapter: Callable[[dict, dict], dict]

    per_component: bool = False
    """Run once per component in the migration order, rather than once."""

    step_kind: str = "migrate"
    """Which work_item this agent's success proves. Read by the gate."""

    requires_env: str | None = None
    """Environment variable that must be set, or the agent is skipped with a
    recorded risk rather than silently omitted."""


# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

REPO_MAP = AgentSpec(
    "repo-map", "agents.discovery.repo_map", DiscoveryResult, ad.discovery
)
API_DISCOVERY = AgentSpec(
    "api-contract-discovery", "agents.discovery.api_contract",
    DiscoveryResult, ad.discovery,
)
EVENT_DISCOVERY = AgentSpec(
    "event-contract-discovery", "agents.discovery.event_contract",
    DiscoveryResult, ad.discovery,
)
DB_DISCOVERY = AgentSpec(
    "db-schema-discovery", "agents.discovery.db_schema",
    DiscoveryResult, ad.discovery,
)

COMPATIBILITY_STRATEGY = AgentSpec(
    "compatibility-strategy", "agents.planning.compatibility_strategy",
    PlanningResult, ad.planning,
)

PROVIDER_PATCH = AgentSpec(
    "provider-patch", "agents.implementation.provider_patch",
    ImplementationResult, ad.implementation, step_kind="provider_patch",
)
CONSUMER_MIGRATION = AgentSpec(
    "consumer-migration", "agents.implementation.consumer_migration",
    ImplementationResult, ad.implementation,
    per_component=True, step_kind="migrate",
)
# Same module, different label and different work item: a transport cut-over is
# a symbol swap in the consumer, proved as a "subscribe" step.
SUBSCRIBER_SWITCH = AgentSpec(
    "subscriber-switch", "agents.implementation.consumer_migration",
    ImplementationResult, ad.implementation,
    per_component=True, step_kind="subscribe",
)

COEXISTENCE_REHEARSAL = AgentSpec(
    "coexistence-rehearsal", "agents.verification.coexistence_rehearsal",
    VerificationResult, ad.verification,
)
CONTRACT_TEST = AgentSpec(
    "contract-test", "agents.verification.contract_test",
    VerificationResult, ad.verification,
    per_component=True, step_kind="migrate",
)
CONTRACT_TEST_SUBSCRIBE = AgentSpec(
    "contract-test", "agents.verification.contract_test",
    VerificationResult, ad.verification,
    per_component=True, step_kind="subscribe",
)
WEBHOOK_QUIET = AgentSpec(
    "webhook-quiet", "agents.verification.webhook_quiet",
    VerificationResult, ad.verification,
    per_component=True, step_kind="webhook_quiet",
)
CRITIC = AgentSpec(
    "critic", "agents.verification.critic",
    VerificationResult, ad.verification,
    requires_env="INTERLOCK_API_URL",
)


# ---------------------------------------------------------------------------
# (change kind, workflow phase) -> agents
# ---------------------------------------------------------------------------

AGENT_REGISTRY: dict[tuple[str, str], tuple[AgentSpec, ...]] = {
    # --- field rename -------------------------------------------------------
    ("field_rename", "DISCOVERY"): (REPO_MAP, API_DISCOVERY, EVENT_DISCOVERY, DB_DISCOVERY),
    ("field_rename", "PLANNING"): (COMPATIBILITY_STRATEGY,),
    ("field_rename", "MODIFY"): (PROVIDER_PATCH, CONSUMER_MIGRATION),
    ("field_rename", "REHEARSE"): (COEXISTENCE_REHEARSAL,),
    ("field_rename", "VERIFY"): (CONTRACT_TEST, CRITIC),

    # --- API contract change ------------------------------------------------
    # No db-schema discovery: an API contract change does not live in SQL.
    ("api_contract_change", "DISCOVERY"): (REPO_MAP, API_DISCOVERY, EVENT_DISCOVERY),
    ("api_contract_change", "PLANNING"): (COMPATIBILITY_STRATEGY,),
    ("api_contract_change", "MODIFY"): (PROVIDER_PATCH, CONSUMER_MIGRATION),
    ("api_contract_change", "REHEARSE"): (COEXISTENCE_REHEARSAL,),
    ("api_contract_change", "VERIFY"): (CONTRACT_TEST, CRITIC),

    # --- webhook -> pub/sub -------------------------------------------------
    # Event discovery only; the consumers are subscribers, not API callers.
    ("transport_migration", "DISCOVERY"): (REPO_MAP, EVENT_DISCOVERY),
    ("transport_migration", "PLANNING"): (COMPATIBILITY_STRATEGY,),
    ("transport_migration", "MODIFY"): (PROVIDER_PATCH, SUBSCRIBER_SWITCH),
    ("transport_migration", "REHEARSE"): (COEXISTENCE_REHEARSAL,),
    # Two proofs per subscriber: its own suite still passes after the switch,
    # and it has actually drained off the retired webhook.
    ("transport_migration", "VERIFY"): (CONTRACT_TEST_SUBSCRIBE, WEBHOOK_QUIET, CRITIC),
}


def agents_for(kind: str, phase: str) -> tuple[AgentSpec, ...]:
    """Registered agents for a (kind, phase), or an empty tuple."""
    return AGENT_REGISTRY.get((kind, phase), ())


# ---------------------------------------------------------------------------
# Signature normalisation
# ---------------------------------------------------------------------------

def _bind(fn: Callable[..., Any], context: dict) -> Any:
    """Call `fn(data)` plus only the extra arguments it actually declares."""
    params = inspect.signature(fn).parameters
    extra: dict[str, Any] = {}
    if "repo_path" in params:
        extra["repo_path"] = Path(context["repo_path"])
    if "base_url" in params:
        extra["base_url"] = context.get("base_url")
    return fn(context["data"], **extra)


def make_callable(spec: AgentSpec) -> Callable[[dict], dict]:
    """
    Produce the single-dict callable `AgentRunner` expects.

    The import happens per call rather than at module load, so importing the
    registry costs nothing and a broken agent module surfaces as an
    `AgentFailure` for that agent rather than a startup crash.
    """

    def _call(context: dict) -> dict:
        module = importlib.import_module(spec.import_path)
        raw = _bind(module.run, context)
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()
        return spec.adapter(raw, context)

    _call.__name__ = f"real_{spec.role.replace('-', '_')}"
    return _call
