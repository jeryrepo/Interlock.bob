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
import sqlite3
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.gate import evaluate_gate
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

# Whether the stub workflow is available as the no-spec fallback.
#
# Real agents are selected per change by the presence of a change_spec row, not
# by this flag — see orchestrator/real_workflow.py.  A change WITH a spec always
# runs real agents regardless of this setting; a change WITHOUT one runs the
# stubs below while this is True, and is rejected when it is False.
STUB_MODE: bool = True

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

        # `from last_exc` matters: the caller reads __cause__ to recover the
        # agent's own explanation rather than this wrapper's summary.
        raise AgentFailure(
            f"Agent '{self.role}' failed after 2 attempts. "
            f"Last error: {last_exc}"
        ) from last_exc


# ---------------------------------------------------------------------------
# Stub agent callables
# ---------------------------------------------------------------------------
# Each stub accepts a context dict and returns a schema-valid object.
# These are replaced by real agent callables when STUB_MODE = False.
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
                from_component="account-service",
                to_component="checkout",
                edge_type="api",
                reason="checkout calls /accounts/{customer_id}",
            ),
            Dependency(
                from_component="account-service",
                to_component="fraud",
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
                from_component="account-service",
                to_component="analytics-worker",
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
# Workflow orchestration
# ---------------------------------------------------------------------------


def _run_discovery_phase(conn: sqlite3.Connection, change_id: str) -> None:
    """INTAKE → DISCOVERY: run all four discovery agents."""
    sm.advance(conn, change_id)  # INTAKE → DISCOVERY

    discovery_agents = [
        ("repo-map", stub_repo_map),
        ("api-contract-discovery", stub_api_contract_discovery),
        ("event-contract-discovery", stub_event_contract_discovery),
        ("db-schema-discovery", stub_db_schema_discovery),
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

    runner = AgentRunner("compatibility-strategy", stub_compatibility_strategy, PlanningResult)
    plan: PlanningResult = runner.run({"change_id": change_id})  # type: ignore[assignment]
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

    runner = AgentRunner("provider-patch", stub_provider_patch, ImplementationResult)
    patch: ImplementationResult = runner.run({"change_id": change_id})  # type: ignore[assignment]
    for ev in patch.evidence:
        ledger.add_evidence(
            conn, change_id, ev.claim_type, ev.subject, ev.content,
            ev.source_ref, ev.confidence, ev.source_revision,
        )

    for consumer in migration_order:
        ledger.upsert_consumer_migration(conn, change_id, consumer, "in_progress")
        runner = AgentRunner(
            f"consumer-migration:{consumer}",
            stub_consumer_migration_fn,
            ImplementationResult,
        )
        impl: ImplementationResult = runner.run({"change_id": change_id, "consumer": consumer})  # type: ignore[assignment]
        for ev in impl.evidence:
            ledger.add_evidence(
                conn, change_id, ev.claim_type, ev.subject, ev.content,
                ev.source_ref, ev.confidence, ev.source_revision,
            )
    return migration_order


def _run_rehearse_phase(conn: sqlite3.Connection, change_id: str) -> None:
    """MODIFY → REHEARSE: coexistence rehearsal."""
    sm.advance(conn, change_id)  # MODIFY → REHEARSE

    runner = AgentRunner("coexistence-rehearsal", stub_coexistence_rehearsal, VerificationResult)
    rehearsal: VerificationResult = runner.run({"change_id": change_id})  # type: ignore[assignment]
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

    for consumer in migration_order:
        runner = AgentRunner(
            f"contract-test:{consumer}",
            stub_contract_test,
            VerificationResult,
        )
        vr: VerificationResult = runner.run({"change_id": change_id, "consumer": consumer})  # type: ignore[assignment]
        for ev in vr.evidence:
            ledger.add_evidence(
                conn, change_id, ev.claim_type, ev.subject, ev.content,
                ev.source_ref, ev.confidence, ev.source_revision,
            )
        ledger.upsert_consumer_migration(conn, change_id, consumer, vr.status)

    runner = AgentRunner("critic", stub_critic, VerificationResult)
    critic_result: VerificationResult = runner.run({"change_id": change_id})  # type: ignore[assignment]
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
    # Routing: a change carrying a structured spec runs the real agents; a
    # legacy description-only change runs the stubs below.  No existing client
    # or test sends a spec, so they all keep the stub path unchanged.
    spec_row = ledger.get_change_spec(conn, change_id)
    if spec_row is not None:
        from orchestrator.real_workflow import run_real_workflow

        run_real_workflow(conn, change_id, spec_row)
        return

    if not STUB_MODE:
        raise RuntimeError(
            "run_workflow() called with STUB_MODE=False and the change carries "
            "no spec, so there is nothing to run. Supply a ChangeSpec to use "
            "the real agents."
        )

    current = sm.get_state(conn, change_id)
    logger.info("[run_workflow] change %s resuming from state %s", change_id, current)

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
