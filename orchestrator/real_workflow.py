"""
orchestrator/real_workflow.py
==============================
The workflow that runs the *real* agents, selected by change kind.

Routing lives in `agent_runner.run_workflow()`:

    spec present  -> this module
    spec absent   -> the legacy stub workflow, unchanged

That split is what lets real agents land without touching a single existing
test: no current test sends a spec, so every one of them keeps the stub path.
`STUB_MODE` stays True and now means "stubs are the no-spec fallback".

Isolation
---------
The implementation agents rewrite files and run `git commit` inside the path
they are given, and `fixtures/` lives inside this repository. Pointing them at
`fixtures/` directly would commit into the user's own working tree — which has
happened before in this project's history.

So every real run operates on a **workspace copy**: `fixtures/` is copied once
per change into `.interlock_work/<change_id>/`, each component is `git init`-ed
with a baseline commit, and the agents only ever see that copy. The workspace is
deterministic by change id so the second `run_workflow()` call (after the human
approves coordination) finds the same tree the first one built.

Honesty
-------
When an agent cannot run — a missing precondition, an unset environment
variable — this module records an explicit `risk` evidence row and leaves the
work item unverified. It never fabricates a `test_result` to satisfy the state
machine. See AGENTS.md invariant 4.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.agent_registry import AgentSpec, agents_for, make_callable
from orchestrator.gate import evaluate_gate, get_required_consumers
from orchestrator.schemas import (
    DiscoveryResult,
    ImplementationResult,
    PlanningResult,
    VerificationResult,
    symbols_for,
)

logger = logging.getLogger(__name__)

_WORKSPACE_ROOT_ENV = "INTERLOCK_WORKSPACE"
_DEFAULT_WORKSPACE_ROOT = ".interlock_work"


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

def workspace_root() -> Path:
    return Path(os.environ.get(_WORKSPACE_ROOT_ENV, _DEFAULT_WORKSPACE_ROOT))


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def prepare_workspace(change_id: str, components_root: str) -> Path:
    """
    Return an isolated, git-initialised copy of *components_root*.

    Idempotent: if the workspace already exists it is reused, so resuming a
    change after the coordination approval does not discard the provider patch
    made in the previous call.
    """
    target = workspace_root() / change_id
    if target.exists():
        return target

    source = Path(components_root)
    if not source.is_dir():
        raise FileNotFoundError(f"components_root does not exist: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source, target, ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc")
    )

    # Each component becomes its own repository so commit SHAs are per-component
    # and genuinely distinct.
    for component in sorted(p for p in target.iterdir() if p.is_dir()):
        _git(["init"], component)
        _git(["config", "user.email", "interlock@example.com"], component)
        _git(["config", "user.name", "Interlock"], component)
        _git(["add", "."], component)
        _git(["commit", "-m", "baseline"], component)

    logger.info("[real_workflow] prepared workspace %s", target)
    return target


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def _build_context(
    conn: sqlite3.Connection,
    change_id: str,
    spec_row: dict,
    agent: AgentSpec,
    workspace: Path,
    component: str | None = None,
) -> dict[str, Any]:
    """Assemble everything any agent might need, in one dict."""
    spec = spec_row["spec"]
    provider = spec["provider"]
    old_symbol, new_symbol = symbols_for(spec)

    target = component or provider
    change_request = {
        "id": change_id,
        "provider": provider,
        "old_field": old_symbol,
        "new_field": new_symbol,
    }

    return {
        "change_id": change_id,
        "role": agent.role,
        "component": component,
        "repo_path": str(workspace / target),
        "base_url": os.environ.get("INTERLOCK_API_URL"),
        "data": {
            "change_id": change_id,
            "fixtures_root": str(workspace),
            "components_root": str(workspace),
            "provider": provider,
            "provider_path": provider,
            "consumer": component,
            "old_field": old_symbol,
            "new_field": new_symbol,
            "change_request": change_request,
            "dependencies": ledger.get_dependencies(conn, change_id),
            "required_consumers": get_required_consumers(conn, change_id),
            "strategy_result": {},
            "expect_new": False,
        },
    }


def _write_evidence(conn: sqlite3.Connection, change_id: str, evidence) -> None:
    for ev in evidence:
        ledger.add_evidence(
            conn, change_id, ev.claim_type, ev.subject, ev.content,
            ev.source_ref, ev.confidence, ev.source_revision,
        )


def _record_risk(
    conn: sqlite3.Connection, change_id: str, subject: str, risk: str, detail: str
) -> None:
    """
    Record that something could not be proven.

    Deliberately a `risk` claim and not a `test_result`: fabricating a test
    result to unblock the state machine is precisely what invariant 4 forbids.
    """
    ledger.add_evidence(
        conn, change_id, "risk", subject,
        {"risk": risk, "detail": detail},
        "orchestrator/real_workflow.py", "confirmed", None,
    )


def _run_agent(
    conn: sqlite3.Connection,
    change_id: str,
    spec_row: dict,
    agent: AgentSpec,
    workspace: Path,
    component: str | None = None,
):
    """Execute one agent, or record why it could not run. Returns the model or None."""
    from orchestrator.agent_runner import AgentFailure, AgentRunner

    if agent.requires_env and not os.environ.get(agent.requires_env):
        _record_risk(
            conn, change_id, agent.role, f"{agent.role}_not_run",
            f"{agent.requires_env} is not set, so {agent.role} was skipped.",
        )
        return None

    label = f"{agent.role}:{component}" if component else agent.role
    context = _build_context(conn, change_id, spec_row, agent, workspace, component)
    runner = AgentRunner(label, make_callable(agent), agent.output_schema)
    try:
        return runner.run(context)
    except AgentFailure as exc:
        _record_risk(
            conn, change_id, component or agent.role, f"{agent.role}_failed", str(exc)
        )
        logger.warning("[real_workflow] %s failed: %s", label, exc)
        return None


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _discovery(conn, change_id, spec_row, workspace) -> None:
    sm.advance(conn, change_id)  # INTAKE -> DISCOVERY
    for agent in agents_for(spec_row["kind"], "DISCOVERY"):
        result = _run_agent(conn, change_id, spec_row, agent, workspace)
        if result is None:
            continue
        assert isinstance(result, DiscoveryResult)
        _write_evidence(conn, change_id, result.evidence)
        for dep in result.dependencies:
            ledger.add_dependency(
                conn, change_id, dep.from_component, dep.to_component,
                dep.edge_type, dep.reason,
            )


def _planning(conn, change_id, spec_row, workspace) -> list[str]:
    sm.advance(conn, change_id)  # DISCOVERY -> PLANNING

    order: list[str] = []
    for agent in agents_for(spec_row["kind"], "PLANNING"):
        result = _run_agent(conn, change_id, spec_row, agent, workspace)
        if result is None:
            continue
        assert isinstance(result, PlanningResult)
        _write_evidence(conn, change_id, result.evidence)
        order = list(result.migration_order)

    # Fall back to the discovered consumers so a planning failure cannot
    # silently empty the work list and let the gate see nothing to check.
    if not order:
        order = get_required_consumers(conn, change_id)

    for component in order:
        for step_kind in _step_kinds_for(spec_row["kind"]):
            ledger.upsert_work_item(conn, change_id, component, "pending", step_kind)

    sm.advance(conn, change_id)  # PLANNING -> COORDINATE
    logger.info("[real_workflow] change %s waiting at COORDINATE", change_id)
    return order


def _step_kinds_for(kind: str) -> tuple[str, ...]:
    """The step kinds the gate will require — seeded so nothing is forgotten."""
    from orchestrator.gate import _DEFAULT_STEP_KINDS, _REQUIRED_STEP_KINDS

    return _REQUIRED_STEP_KINDS.get(kind, _DEFAULT_STEP_KINDS)


def _modify(conn, change_id, spec_row, workspace) -> list[str]:
    order = [m["consumer"] for m in ledger.get_consumer_migrations(conn, change_id)]
    if not order:
        order = get_required_consumers(conn, change_id)

    provider = spec_row["spec"]["provider"]

    for agent in agents_for(spec_row["kind"], "MODIFY"):
        if not agent.per_component:
            # Provider-side work is tracked as a work item on the provider so
            # the gate can require it (see gate._REQUIRED_PROVIDER_STEPS).
            ledger.upsert_work_item(
                conn, change_id, provider, "in_progress", agent.step_kind
            )
            result = _run_agent(conn, change_id, spec_row, agent, workspace)
            if result is None:
                ledger.upsert_work_item(
                    conn, change_id, provider, "failed", agent.step_kind,
                    {"error": f"{agent.role} failed"},
                )
                continue
            assert isinstance(result, ImplementationResult)
            _write_evidence(conn, change_id, result.evidence)
            ledger.upsert_work_item(
                conn, change_id, provider, "verified", agent.step_kind,
                {"commit_sha": result.commit_ref},
            )
            continue

        for component in order:
            ledger.upsert_work_item(
                conn, change_id, component, "in_progress", agent.step_kind
            )
            result = _run_agent(conn, change_id, spec_row, agent, workspace, component)
            if result is None:
                ledger.upsert_work_item(
                    conn, change_id, component, "failed", agent.step_kind,
                    {"error": "implementation agent failed"},
                )
                continue
            assert isinstance(result, ImplementationResult)
            _write_evidence(conn, change_id, result.evidence)
            ledger.upsert_work_item(
                conn, change_id, component, "in_progress", agent.step_kind,
                {"commit_sha": result.commit_ref},
            )
    return order


def _rehearse(conn, change_id, spec_row, workspace) -> None:
    sm.advance(conn, change_id)  # MODIFY -> REHEARSE

    agents = agents_for(spec_row["kind"], "REHEARSE")
    if not agents:
        _record_risk(
            conn, change_id, "coexistence-rehearsal", "rehearsal_not_run",
            "No rehearsal agent registered for this change kind.",
        )
        return

    for agent in agents:
        result = _run_agent(conn, change_id, spec_row, agent, workspace)
        if result is None:
            continue
        assert isinstance(result, VerificationResult)
        _write_evidence(conn, change_id, result.evidence)


def _verify(conn, change_id, spec_row, workspace, order: list[str]) -> None:
    sm.advance(conn, change_id)  # REHEARSE -> VERIFY

    for agent in agents_for(spec_row["kind"], "VERIFY"):
        if not agent.per_component:
            result = _run_agent(conn, change_id, spec_row, agent, workspace)
            if result is not None:
                assert isinstance(result, VerificationResult)
                _write_evidence(conn, change_id, result.evidence)
            continue

        for component in order:
            result = _run_agent(conn, change_id, spec_row, agent, workspace, component)
            if result is None:
                ledger.upsert_work_item(
                    conn, change_id, component, "failed", agent.step_kind,
                    {"error": f"{agent.role} did not run"},
                )
                continue
            assert isinstance(result, VerificationResult)
            _write_evidence(conn, change_id, result.evidence)
            ledger.upsert_work_item(
                conn, change_id, component,
                "verified" if result.status == "verified" else "failed",
                agent.step_kind,
            )

    # Any step still not terminal would stall the state machine. Mark it failed
    # with the reason recorded — unproven is failed, not quietly skipped.
    for item in ledger.get_work_items(conn, change_id):
        if item["status"] in ("pending", "in_progress"):
            ledger.upsert_work_item(
                conn, change_id, item["component"], "failed", item["step_kind"],
                {"error": f"no agent proved step '{item['step_kind']}'"},
            )
            _record_risk(
                conn, change_id, item["component"], "step_unproven",
                f"No registered agent proves step '{item['step_kind']}' for this change kind.",
            )

    sm.advance(conn, change_id)  # VERIFY -> GATE_DECISION

    decision = evaluate_gate(conn, change_id)
    ledger.record_gate_decision(conn, change_id, decision.result, decision.reason)
    logger.info("[real_workflow] gate for %s: %s", change_id, decision.result)

    if decision.result == "VERIFIED":
        sm.advance(conn, change_id)  # GATE_DECISION -> APPROVE


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_real_workflow(
    conn: sqlite3.Connection, change_id: str, spec_row: dict
) -> None:
    """Run the real-agent workflow from the change's current persisted state."""
    spec = spec_row["spec"]
    workspace = prepare_workspace(change_id, spec.get("components_root", "fixtures"))
    current = sm.get_state(conn, change_id)
    logger.info(
        "[real_workflow] change %s (%s) resuming from %s",
        change_id, spec_row["kind"], current,
    )

    if current == "INTAKE":
        _discovery(conn, change_id, spec_row, workspace)
        _planning(conn, change_id, spec_row, workspace)
        return

    if current == "MODIFY":
        order = _modify(conn, change_id, spec_row, workspace)
        _rehearse(conn, change_id, spec_row, workspace)
        _verify(conn, change_id, spec_row, workspace, order)
        return

    logger.info("[real_workflow] change %s in %s — nothing to run", change_id, current)
