"""
orchestrator/agent_runner.py
==============================
Reusable agent execution wrapper and stub workflow.

AgentRunner contract:
- Wraps any callable agent function.
- Validates output against a Pydantic schema.
- Retries once on ValidationError, then raises AgentFailure.
- Logs role, attempt, and outcome.

Stub agents:
- STUB_MODE controls whether stubs or real agents run.
- When real agents land, set STUB_MODE = False and wire in real callables.
- Stubs MUST NOT run alongside real agents during integration.
- All stubs return schema-valid objects seeded with demo data including
  analytics-worker as the undocumented dependency (discovered from source).

run_workflow():
- Resumes agent work from the change's CURRENT persisted state.
- Runs only the agent phases appropriate to the current state, then stops.
- NEVER auto-approves human gates.  It stops at COORDINATE and at
  GATE_DECISION, leaving the change waiting for POST /approve.
- Call it again after each approval to continue into the next phase segment.

Gate flow:
  POST /change-requests   → run_workflow()  → stops at COORDINATE
  POST /approve coordinate → advance COORDINATE→MODIFY, run_workflow() → stops at APPROVE
  POST /approve legacy_removal → advance APPROVE→DONE  (no agent work needed)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import shutil
from pathlib import Path
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.gate import evaluate_gate, get_required_consumers
from orchestrator.schemas import (
    Dependency,
    DiscoveryResult,
    Evidence,
    ImplementationResult,
    PlanningResult,
    VerificationResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stub mode flag
# ---------------------------------------------------------------------------

# Set to False when real agents are integrated.  The stubs defined below
# must not run when this is False.
STUB_MODE: bool = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Base URL for the orchestrator API — used by the critic agent to fetch evidence.
# Can be overridden via INTERLOCK_API_URL environment variable.
INTERLOCK_API_URL: str = os.environ.get("INTERLOCK_API_URL", "http://127.0.0.1:8000")

# Root of the fixture repositories.
_FIXTURES_ROOT: Path = Path(__file__).parent.parent / "fixtures"

# Project root (for docker-compose.yml location).
_PROJECT_ROOT: Path = Path(__file__).parent.parent

# Fixed change-request fields used by implementation agents.
# These match the demo change described in 00_SHARED_TEAM_CONTRACT.md.
_CHANGE_REQUEST_DEFAULTS = {
    "old_field": "customer_id",
    "new_field": "account_id",
    "provider": "account-service",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentFailure(Exception):
    """Raised when an agent fails validation on both attempts."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class AgentRunner:
    """
    Wraps an agent callable with validation and retry logic.

    Parameters
    ----------
    role:
        Human-readable agent name used in log output.
    fn:
        Callable that accepts a context dict and returns a dict or BaseModel.
    output_schema:
        Pydantic model class; the agent output is validated against this.
    timeout:
        Reserved for future async/subprocess use; not enforced in Phase 1.
    """

    def __init__(
        self,
        role: str,
        fn: Callable[[dict], Any],
        output_schema: Type[BaseModel],
        timeout: int = 30,
    ) -> None:
        self.role = role
        self.fn = fn
        self.output_schema = output_schema
        self.timeout = timeout

    def run(self, context: dict) -> BaseModel:
        """
        Execute the agent function and validate its output.

        Retries once on ValidationError.  Raises AgentFailure after the
        second consecutive failure so no failure is ever silent.
        """
        last_exc: Exception | None = None

        for attempt in range(1, 3):  # attempts 1 and 2
            logger.info("[%s] attempt %d/%d", self.role, attempt, 2)
            try:
                raw = self.fn(context)
                # Accept either a dict or a pre-built BaseModel instance.
                if isinstance(raw, self.output_schema):
                    result = raw
                elif isinstance(raw, dict):
                    result = self.output_schema(**raw)
                else:
                    result = self.output_schema.model_validate(raw)
                logger.info("[%s] attempt %d succeeded", self.role, attempt)
                return result
            except (ValidationError, Exception) as exc:
                last_exc = exc
                logger.warning(
                    "[%s] attempt %d failed: %s", self.role, attempt, exc
                )

        raise AgentFailure(
            f"Agent '{self.role}' failed after 2 attempts. "
            f"Last error: {last_exc}"
        )


# ---------------------------------------------------------------------------
# Stub agent callables
# ---------------------------------------------------------------------------
# Each stub accepts a context dict and returns a schema-valid object.
# These are kept as a fallback/demo-safety mode.  Set STUB_MODE = True to
# use them instead of the real agents.
# ---------------------------------------------------------------------------

_DEMO_CONSUMERS = ["checkout", "fraud", "analytics-worker"]


def _evidence(claim_type: str, subject: str, content: dict, source_ref: str, confidence: str = "confirmed") -> Evidence:
    return Evidence(
        claim_type=claim_type,  # type: ignore[arg-type]
        subject=subject,
        content=content,
        source_ref=source_ref,
        confidence=confidence,  # type: ignore[arg-type]
    )


def stub_repo_map(context: dict) -> DiscoveryResult:
    change_id = context["change_id"]
    return DiscoveryResult(
        change_id=change_id,
        evidence=[
            _evidence(
                "dependency",
                "account-service",
                {"repo": "fixtures/account-service", "openapi": "openapi.yaml"},
                "fixtures/account-service/openapi.yaml",
            )
        ],
        dependencies=[
            Dependency(
                from_component="checkout",
                to_component="account-service",
                edge_type="api",
                reason="checkout calls /accounts/{customer_id}",
            ),
            Dependency(
                from_component="fraud",
                to_component="account-service",
                edge_type="api",
                reason="fraud calls /accounts/{customer_id} for risk scoring",
            ),
        ],
    )


def stub_api_contract_discovery(context: dict) -> DiscoveryResult:
    change_id = context["change_id"]
    return DiscoveryResult(
        change_id=change_id,
        evidence=[
            _evidence(
                "dependency",
                "checkout",
                {"endpoint": "/accounts/{customer_id}", "method": "GET"},
                "fixtures/account-service/openapi.yaml",
            )
        ],
        dependencies=[],
    )


def stub_event_contract_discovery(context: dict) -> DiscoveryResult:
    change_id = context["change_id"]
    # analytics-worker is discovered from source code as an undocumented
    # event consumer — this is the key story beat.
    return DiscoveryResult(
        change_id=change_id,
        evidence=[
            _evidence(
                "dependency",
                "analytics-worker",
                {
                    "file": "fixtures/analytics-worker/app/worker.py",
                    "pattern": "customer_id",
                    "note": "Direct field access found in source; not in API docs",
                },
                "fixtures/analytics-worker/app/worker.py",
                "hypothesis",
            )
        ],
        dependencies=[
            Dependency(
                from_component="analytics-worker",
                to_component="account-service",
                edge_type="undocumented",
                reason="Source code directly accesses customer_id field",
            )
        ],
    )


def stub_db_schema_discovery(context: dict) -> DiscoveryResult:
    change_id = context["change_id"]
    # platform-config holds a schema reference but is not an active API consumer
    # that requires a migration to be verified — it only needs a schema update.
    # We record it as evidence but do NOT add it as a dependency_edge pointing
    # at account-service, so the gate does not block waiting for its verification.
    return DiscoveryResult(
        change_id=change_id,
        evidence=[
            _evidence(
                "dependency",
                "platform-config",
                {"schema_ref": "customer_id column present"},
                "fixtures/platform-config/schema.sql",
            )
        ],
        dependencies=[],
    )


def stub_compatibility_strategy(context: dict) -> PlanningResult:
    change_id = context["change_id"]
    return PlanningResult(
        change_id=change_id,
        migration_order=_DEMO_CONSUMERS,
        evidence=[
            _evidence(
                "migration_status",
                "migration-plan",
                {
                    "strategy": "dual-write",
                    "order": _DEMO_CONSUMERS,
                    "note": "analytics-worker must be patched before field removal",
                },
                "orchestrator/agent_runner.py",
            )
        ],
    )


def stub_provider_patch(context: dict) -> ImplementationResult:
    change_id = context["change_id"]
    return ImplementationResult(
        change_id=change_id,
        consumer="account-service",
        commit_ref="stub-commit-provider-abc1234",
        evidence=[
            _evidence(
                "migration_status",
                "account-service",
                {"action": "add account_id alias alongside customer_id"},
                "fixtures/account-service/app/main.py",
            )
        ],
    )


def stub_consumer_migration_fn(context: dict) -> ImplementationResult:
    consumer = context.get("consumer", "checkout")
    change_id = context["change_id"]
    return ImplementationResult(
        change_id=change_id,
        consumer=consumer,
        commit_ref=f"stub-commit-{consumer}-abc1234",
        evidence=[
            _evidence(
                "migration_status",
                consumer,
                {"action": f"replaced customer_id with account_id in {consumer}"},
                f"fixtures/{consumer}/app/main.py",
            )
        ],
    )


def stub_contract_test(context: dict) -> VerificationResult:
    consumer = context.get("consumer", "checkout")
    change_id = context["change_id"]
    return VerificationResult(
        change_id=change_id,
        consumer=consumer,
        status="verified",
        evidence=[
            _evidence(
                "test_result",
                consumer,
                {"tests_passed": True, "suite": "contract"},
                f"fixtures/{consumer}/tests/",
            )
        ],
    )


def stub_coexistence_rehearsal(context: dict) -> VerificationResult:
    change_id = context["change_id"]
    return VerificationResult(
        change_id=change_id,
        consumer="all",
        status="verified",
        evidence=[
            _evidence(
                "test_result",
                "coexistence-rehearsal",
                {"dual_write_passed": True, "rollback_tested": True},
                "docker-compose.yml",
            )
        ],
    )


def stub_critic(context: dict) -> VerificationResult:
    change_id = context["change_id"]
    return VerificationResult(
        change_id=change_id,
        consumer="all",
        status="verified",
        evidence=[
            _evidence(
                "risk",
                "critic-assessment",
                {
                    "evidence_quality": "high",
                    "gaps": [],
                    "recommendation": "proceed",
                },
                "orchestrator/agent_runner.py",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Real agent adapter callables
# ---------------------------------------------------------------------------
# Each adapter wraps a real agent's run() to match the AgentRunner calling
# convention: fn(context: dict) -> dict | BaseModel.
#
# The real agents are NOT modified — adaptations live here only.
# ---------------------------------------------------------------------------

def _real_repo_map(context: dict) -> dict:
    from agents.discovery import repo_map
    return repo_map.run(context)


def _real_api_contract_discovery(context: dict) -> dict:
    from agents.discovery import api_contract
    return api_contract.run(context)


def _real_event_contract_discovery(context: dict) -> dict:
    from agents.discovery import event_contract
    return event_contract.run(context)


def _real_db_schema_discovery(context: dict) -> dict:
    from agents.discovery import db_schema
    return db_schema.run(context)


def _real_compatibility_strategy(context: dict) -> dict:
    """
    Adapter: compatibility_strategy.run() expects:
      {"change_request": {...}, "dependencies": [...], "evidence": [...]}

    The context carries change_id; we fetch dependencies from the ledger and
    build the change_request dict from defaults.  The agent returns a
    _StrategyResult dict, which we reshape into PlanningResult shape.
    """
    from agents.planning import compatibility_strategy

    change_id: str = context["change_id"]
    conn: sqlite3.Connection = context["conn"]

    dep_rows = ledger.get_dependencies(conn, change_id)
    ev_rows = ledger.get_evidence(conn, change_id)

    # Build minimal change_request for the planning agent
    change_request = {
        "id": change_id,
        "old_field": _CHANGE_REQUEST_DEFAULTS["old_field"],
        "new_field": _CHANGE_REQUEST_DEFAULTS["new_field"],
        "provider": _CHANGE_REQUEST_DEFAULTS["provider"],
    }

    strategy_data = {
        "change_request": change_request,
        "dependencies": dep_rows,
        "evidence": ev_rows,
    }

    result = compatibility_strategy.run(strategy_data)

    # Reshape _StrategyResult → PlanningResult shape.
    # affected_consumers is the ordered migration list.
    # evidence items are plain dicts — coerce to Evidence-compatible form.
    raw_evidence = result.get("evidence", [])
    coerced_evidence: list[dict] = []
    for ev in raw_evidence:
        # Ensure required fields are present; planning agent emits "dependency"
        # claim_type for its consumer evidence entries.
        coerced_evidence.append({
            "claim_type": ev.get("claim_type", "migration_status"),
            "subject": ev.get("subject", "migration-plan"),
            "content": ev.get("content", {}),
            "source_ref": ev.get("source_ref", f"dependency:{change_id}"),
            "confidence": ev.get("confidence", "confirmed"),
            "source_revision": ev.get("source_revision"),
        })

    # Add a top-level migration-plan summary evidence entry
    affected = result.get("affected_consumers", [])
    coerced_evidence.insert(0, {
        "claim_type": "migration_status",
        "subject": "migration-plan",
        "content": {
            "strategy": "topological-sort",
            "order": affected,
            "migration_steps": result.get("migration_steps", []),
            "compatibility_requirements": result.get("compatibility_requirements", []),
        },
        "source_ref": "agents/planning/compatibility_strategy.py",
        "confidence": "confirmed",
        "source_revision": None,
    })

    return {
        "change_id": change_id,
        "migration_order": affected,
        "evidence": coerced_evidence,
    }


def _make_real_provider_patch() -> Callable[[dict], dict]:
    """
    Return a closure that calls provider_patch.run() with the correct
    repo_path and adapts the result to ImplementationResult shape.
    """
    def _adapter(context: dict) -> dict:
        from agents.implementation import provider_patch

        change_id: str = context["change_id"]
        provider = _CHANGE_REQUEST_DEFAULTS["provider"]
        repo_path = _FIXTURES_ROOT / provider

        change_request = {
            "id": change_id,
            **_CHANGE_REQUEST_DEFAULTS,
        }
        data = {
            "change_request": change_request,
            "strategy_result": context.get("strategy_result", {}),
        }

        result = provider_patch.run(data, repo_path)

        # Adapt _PatchResult → ImplementationResult shape.
        evidence: list[dict] = result.get("evidence", [])
        for ev in evidence:
            ev.setdefault("confidence", "confirmed")

        return {
            "change_id": change_id,
            "consumer": provider,
            "commit_ref": result.get("commit_sha"),
            "evidence": evidence,
        }

    return _adapter


def _make_real_consumer_migration(consumer: str) -> Callable[[dict], dict]:
    """
    Return a closure for a specific consumer that calls consumer_migration.run()
    and adapts the result to ImplementationResult shape.
    """
    def _adapter(context: dict) -> dict:
        from agents.implementation import consumer_migration

        change_id: str = context["change_id"]
        repo_path = _FIXTURES_ROOT / consumer

        change_request = {
            "id": change_id,
            **_CHANGE_REQUEST_DEFAULTS,
        }
        data = {
            "consumer": consumer,
            "change_request": change_request,
            "strategy_result": context.get("strategy_result", {}),
        }

        result = consumer_migration.run(data, repo_path)

        # Adapt _MigrationResult → ImplementationResult shape.
        evidence: list[dict] = result.get("evidence", [])
        for ev in evidence:
            ev.setdefault("confidence", "confirmed")

        return {
            "change_id": change_id,
            "consumer": consumer,
            "commit_ref": result.get("commit_sha"),
            "evidence": evidence,
        }

    return _adapter


def _make_real_contract_test(consumer: str) -> Callable[[dict], VerificationResult]:
    """
    Return a closure for a specific consumer that calls contract_test.run()
    using the fixture's repo directory.
    """
    def _adapter(context: dict) -> VerificationResult:
        from agents.verification import contract_test

        change_id: str = context["change_id"]
        commit_ref: str | None = context.get("commit_ref")
        repo_path = _FIXTURES_ROOT / consumer

        data = {
            "change_id": change_id,
            "consumer": consumer,
            "commit_ref": commit_ref,
        }

        return contract_test.run(data, repo_path)

    return _adapter


def _make_real_coexistence_rehearsal() -> Callable[[dict], VerificationResult]:
    """
    Return a closure that calls coexistence_rehearsal.run() with the
    project-level docker-compose.yml.  Skipped (returns stub-like verified)
    when the -docker marker is excluded, but the real path IS taken in a
    normal run.
    """
    def _adapter(context: dict) -> VerificationResult:
        from agents.verification import coexistence_rehearsal

        change_id: str = context["change_id"]
        compose_file = _PROJECT_ROOT / "docker-compose.yml"

        data = {
            "change_id": change_id,
            "consumer": "coexistence",
        }

        return coexistence_rehearsal.run(data, compose_file)

    return _adapter


def _make_real_critic(required_consumers: list[str]) -> Callable[[dict], VerificationResult]:
    """
    Return a closure that calls critic.run() with the orchestrator base URL
    and the known required consumers list.
    """
    def _adapter(context: dict) -> VerificationResult:
        from agents.verification import critic

        change_id: str = context["change_id"]
        base_url = os.environ.get("INTERLOCK_API_URL", INTERLOCK_API_URL)

        data = {
            "change_id": change_id,
            "consumer": "critic",
            "required_consumers": required_consumers,
        }

        return critic.run(data, base_url=base_url)

    return _adapter


# ---------------------------------------------------------------------------
# Workflow orchestration
# ---------------------------------------------------------------------------


def _run_discovery_phase(conn: sqlite3.Connection, change_id: str) -> None:
    """INTAKE → DISCOVERY: run all four discovery agents."""
    sm.advance(conn, change_id)  # INTAKE → DISCOVERY

    if STUB_MODE:
        discovery_agents = [
            ("repo-map", stub_repo_map),
            ("api-contract-discovery", stub_api_contract_discovery),
            ("event-contract-discovery", stub_event_contract_discovery),
            ("db-schema-discovery", stub_db_schema_discovery),
        ]
    else:
        discovery_agents = [
            ("repo-map", _real_repo_map),
            ("api-contract-discovery", _real_api_contract_discovery),
            ("event-contract-discovery", _real_event_contract_discovery),
            ("db-schema-discovery", _real_db_schema_discovery),
        ]

    for role, fn in discovery_agents:
        runner = AgentRunner(role, fn, DiscoveryResult)
        result: DiscoveryResult = runner.run({"change_id": change_id})  # type: ignore[assignment]
        for ev in result.evidence:
            ledger.add_evidence(
                conn, change_id, ev.claim_type, ev.subject, ev.content,
                ev.source_ref, ev.confidence, ev.source_revision,
            )
        for dep in result.dependencies:
            ledger.add_dependency(
                conn, change_id, dep.from_component, dep.to_component,
                dep.edge_type, dep.reason,
            )


def _run_planning_phase(conn: sqlite3.Connection, change_id: str) -> list[str]:
    """DISCOVERY → PLANNING: run compatibility-strategy, seed consumer rows.
    Returns the migration_order list for use by subsequent phases."""
    sm.advance(conn, change_id)  # DISCOVERY → PLANNING

    if STUB_MODE:
        planning_fn = stub_compatibility_strategy
        planning_context: dict = {"change_id": change_id}
    else:
        planning_fn = _real_compatibility_strategy
        # Pass conn so the adapter can read ledger state
        planning_context = {"change_id": change_id, "conn": conn}

    runner = AgentRunner("compatibility-strategy", planning_fn, PlanningResult)
    plan: PlanningResult = runner.run(planning_context)  # type: ignore[assignment]
    for ev in plan.evidence:
        ledger.add_evidence(
            conn, change_id, ev.claim_type, ev.subject, ev.content,
            ev.source_ref, ev.confidence, ev.source_revision,
        )
    for consumer in plan.migration_order:
        ledger.upsert_consumer_migration(conn, change_id, consumer, "pending")

    sm.advance(conn, change_id)  # PLANNING → COORDINATE

    # Stop here — human must POST /approve {gate: "coordinate"} to continue.
    logger.info("[run_workflow] change %s waiting at COORDINATE for human approval", change_id)
    return plan.migration_order


def _run_modify_phase(conn: sqlite3.Connection, change_id: str) -> list[str]:
    """MODIFY: patch provider, migrate each consumer.
    Returns the migration_order list for use by subsequent phases."""
    # Derive consumer list from existing consumer_migration rows (seeded in planning).
    migrations = ledger.get_consumer_migrations(conn, change_id)
    migration_order = [m["consumer"] for m in migrations]

    if STUB_MODE:
        provider_patch_fn = stub_provider_patch
    else:
        provider_patch_fn = _make_real_provider_patch()

    runner = AgentRunner("provider-patch", provider_patch_fn, ImplementationResult)
    patch: ImplementationResult = runner.run({"change_id": change_id})  # type: ignore[assignment]
    for ev in patch.evidence:
        ledger.add_evidence(
            conn, change_id, ev.claim_type, ev.subject, ev.content,
            ev.source_ref, ev.confidence, ev.source_revision,
        )

    # Track commit SHAs per consumer for use by contract-test and critic.
    commit_refs: dict[str, str | None] = {
        _CHANGE_REQUEST_DEFAULTS["provider"]: patch.commit_ref
    }

    for consumer in migration_order:
        ledger.upsert_consumer_migration(conn, change_id, consumer, "in_progress")

        if STUB_MODE:
            consumer_fn = stub_consumer_migration_fn
            consumer_context: dict = {"change_id": change_id, "consumer": consumer}
        else:
            consumer_fn = _make_real_consumer_migration(consumer)
            consumer_context = {"change_id": change_id}

        runner = AgentRunner(
            f"consumer-migration:{consumer}",
            consumer_fn,
            ImplementationResult,
        )
        impl: ImplementationResult = runner.run(consumer_context)  # type: ignore[assignment]
        for ev in impl.evidence:
            ledger.add_evidence(
                conn, change_id, ev.claim_type, ev.subject, ev.content,
                ev.source_ref, ev.confidence, ev.source_revision,
            )
        commit_refs[consumer] = impl.commit_ref

    # Stash commit_refs in the DB via evidence so verify phase can retrieve them.
    # We use a single migration_status evidence entry with all commit refs.
    if not STUB_MODE:
        ledger.add_evidence(
            conn, change_id,
            "migration_status", "_commit_refs",
            commit_refs,
            "orchestrator/agent_runner.py",
            "confirmed",
            None,
        )

    return migration_order


def _run_rehearse_phase(conn: sqlite3.Connection, change_id: str) -> None:
    """MODIFY → REHEARSE: coexistence rehearsal."""
    sm.advance(conn, change_id)  # MODIFY → REHEARSE

    if STUB_MODE:
        rehearsal_fn = stub_coexistence_rehearsal
        rehearsal_context: dict = {"change_id": change_id}
    else:
        rehearsal_fn = _make_real_coexistence_rehearsal()
        rehearsal_context = {"change_id": change_id}

    runner = AgentRunner("coexistence-rehearsal", rehearsal_fn, VerificationResult)
    rehearsal: VerificationResult = runner.run(rehearsal_context)  # type: ignore[assignment]
    for ev in rehearsal.evidence:
        ledger.add_evidence(
            conn, change_id, ev.claim_type, ev.subject, ev.content,
            ev.source_ref, ev.confidence, ev.source_revision,
        )


def _run_verify_phase(conn: sqlite3.Connection, change_id: str, migration_order: list[str]) -> None:
    """REHEARSE → VERIFY: contract tests + critic, evaluate gate, advance to APPROVE.

    GATE_DECISION → APPROVE is a deterministic transition (no human input):
    if the gate is VERIFIED we advance immediately.  The human gate is
    APPROVE → DONE, which requires POST /approve {gate: "legacy_removal"}.
    """
    sm.advance(conn, change_id)  # REHEARSE → VERIFY

    # Retrieve commit refs if stored
    commit_refs: dict[str, str | None] = {}
    if not STUB_MODE:
        ev_rows = ledger.get_evidence(conn, change_id)
        for ev in ev_rows:
            if ev["claim_type"] == "migration_status" and ev["subject"] == "_commit_refs":
                commit_refs = ev["content"]
                break

    for consumer in migration_order:
        if STUB_MODE:
            contract_fn = stub_contract_test
            contract_context: dict = {"change_id": change_id, "consumer": consumer}
        else:
            contract_fn = _make_real_contract_test(consumer)
            contract_context = {
                "change_id": change_id,
                "consumer": consumer,
                "commit_ref": commit_refs.get(consumer),
            }

        runner = AgentRunner(
            f"contract-test:{consumer}",
            contract_fn,
            VerificationResult,
        )
        vr: VerificationResult = runner.run(contract_context)  # type: ignore[assignment]
        for ev in vr.evidence:
            ledger.add_evidence(
                conn, change_id, ev.claim_type, ev.subject, ev.content,
                ev.source_ref, ev.confidence, ev.source_revision,
            )
        ledger.upsert_consumer_migration(conn, change_id, consumer, vr.status)

    # Resolve required consumers from the dependency graph for the critic.
    required_consumers = get_required_consumers(conn, change_id)

    if STUB_MODE:
        critic_fn = stub_critic
        critic_context: dict = {"change_id": change_id}
    else:
        critic_fn = _make_real_critic(required_consumers)
        critic_context = {"change_id": change_id}

    runner = AgentRunner("critic", critic_fn, VerificationResult)
    critic_result: VerificationResult = runner.run(critic_context)  # type: ignore[assignment]
    for ev in critic_result.evidence:
        ledger.add_evidence(
            conn, change_id, ev.claim_type, ev.subject, ev.content,
            ev.source_ref, ev.confidence, ev.source_revision,
        )

    sm.advance(conn, change_id)  # VERIFY → GATE_DECISION

    decision = evaluate_gate(conn, change_id)
    ledger.record_gate_decision(conn, change_id, decision.result, decision.reason)

    # GATE_DECISION → APPROVE is automatic when VERIFIED.
    # If NOT_PROVEN_SAFE the change stays at GATE_DECISION; the Streamlit UI
    # shows the blocking consumers and the human must not approve.
    if decision.result == "VERIFIED":
        sm.advance(conn, change_id)  # GATE_DECISION → APPROVE
        logger.info("[run_workflow] change %s advanced to APPROVE — awaiting legacy_removal", change_id)
    else:
        logger.warning(
            "[run_workflow] change %s BLOCKED at GATE_DECISION: %s",
            change_id, decision.reason,
        )


def run_workflow(conn: sqlite3.Connection, change_id: str) -> None:
    """
    Resume agent work from the change's current persisted state.

    This function is IDEMPOTENT with respect to the state machine: it reads
    the current state and only runs the phases appropriate for that state,
    then returns.  It NEVER auto-approves human gates.

    Call sequence:
      1. POST /change-requests     → run_workflow() runs INTAKE→PLANNING,
                                     stops at COORDINATE.
      2. POST /approve coordinate  → endpoint advances to MODIFY, then calls
                                     run_workflow() which runs MODIFY→GATE_DECISION,
                                     stops at APPROVE.
      3. POST /approve legacy_removal → endpoint advances to DONE; no agent
                                        work needed.
    """
    current = sm.get_state(conn, change_id)
    logger.info("[run_workflow] change %s resuming from state %s (STUB_MODE=%s)", change_id, current, STUB_MODE)

    if current == "INTAKE":
        _run_discovery_phase(conn, change_id)
        _run_planning_phase(conn, change_id)
        # Now in COORDINATE — stop and wait for human approval.
        return

    if current == "MODIFY":
        migration_order = _run_modify_phase(conn, change_id)
        _run_rehearse_phase(conn, change_id)
        _run_verify_phase(conn, change_id, migration_order)
        # Now in APPROVE (gate VERIFIED) or GATE_DECISION (blocked).
        # Either way: stop and let the human decide next.
        return

    # Any other state: nothing for the agent runner to do right now.
    logger.info(
        "[run_workflow] change %s is in state %s — no agent work to run",
        change_id, current,
    )
