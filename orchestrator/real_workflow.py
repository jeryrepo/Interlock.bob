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
import stat
import subprocess
from pathlib import Path
from typing import Any

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.adapters import ImplementationFailed
from orchestrator.agent_registry import AgentSpec, agents_for, make_callable
from orchestrator.gate import (
    REHEARSAL_STEP_KIND,
    evaluate_gate,
    get_required_consumers,
)
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

_GIT_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

def _copy_ignore() -> tuple[str, ...]:
    """
    What never gets copied into a workspace.

    Reuses the discovery agents' own definition of "not source" rather than a
    second list that can drift from it. Imported lazily, not at module scope,
    so `orchestrator` still does not import `agents` at load time - the same
    reason `agent_registry` holds import paths as strings.

    Ignoring only `__pycache__`/`.git`/`*.pyc` was survivable against the
    fixtures and badly wrong against a real repository: `node_modules` and
    `.venv` were copied, and then `git add .` ran over them under a 60-second
    cap. The timeout is swallowed, `_is_usable` only tests that `.git` is a
    directory, and the result was a HEAD-less repository judged usable and
    reused forever - surfacing downstream as "no git commit recorded for this
    component" on every component, which points at the wrong problem entirely.

    These are build outputs and dependency caches. Excluding them changes
    nothing a test needs that it cannot regenerate, and the workspace is a
    throwaway copy for analysis, not a deployable tree.
    """
    from agents.discovery.repo_map import _SKIP_DIRS

    return ("*.pyc", *sorted(_SKIP_DIRS | {"__pycache__", ".git"}))


def _ignore_for(source: Path):
    """
    The copytree ignore callback for *source*.

    Skips build output and dependency caches, and - separately - the workspace
    root itself whenever it happens to live inside the tree being copied.

    That second case is not hypothetical. The workspace root defaults to a
    *relative* `.interlock_work`, so `--components-root .` from inside your own
    repository puts the destination inside the source. `copytree` snapshots the
    directory listing after the destination has been created, so the first run
    copies an empty directory and every run after that copies the previous
    run's copy - growth is exponential in the number of runs, and the only
    symptom is that things get slower until the disk fills.
    """
    patterns = _copy_ignore()
    workspace = workspace_root().resolve()
    source_root = source.resolve()
    inside = workspace == source_root or source_root in workspace.parents

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(shutil.ignore_patterns(*patterns)(directory, names))
        if inside:
            here = Path(directory).resolve()
            ignored.update(n for n in names if (here / n).resolve() == workspace)
        return ignored

    return _ignore


def workspace_root() -> Path:
    return Path(os.environ.get(_WORKSPACE_ROOT_ENV, _DEFAULT_WORKSPACE_ROOT))


def _force_rmtree(path: Path) -> None:
    """
    Delete a tree that contains a git repository.

    `shutil.rmtree` fails on Windows for files git marks read-only — objects and
    packs — and with `ignore_errors=True` it fails *silently*, leaving the tree
    behind. That is worse than raising: the caller believes the directory is
    gone, `copytree` then refuses to write into it, and the run dies somewhere
    unrelated. Clearing the read-only bit and retrying is the standard fix.
    """
    def _clear_readonly(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onexc=_clear_readonly)


def _is_usable(workspace: Path) -> bool:
    """
    True when *workspace* looks like a complete, git-initialised component tree.

    "The directory exists" is not the same as "the workspace is ready": every
    component must be its own repository, because the implementation agents
    commit into them and read back real SHAs.
    """
    if not workspace.is_dir():
        return False
    components = [p for p in workspace.iterdir() if p.is_dir()]
    if not components:
        return False
    return all((c / ".git").is_dir() for c in components)


def _git(args: list[str], cwd: Path):
    """Run one git command. Returns the CompletedProcess, or None on timeout."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Workspace setup only. A hung git here leaves the workspace
        # incomplete, and _is_usable() rebuilds it on the next attempt.
        logger.warning("[real_workflow] git %s timed out in %s", args, cwd)
        return None


def _has_content(component: Path) -> bool:
    """True if the component holds anything git could have committed."""
    return any(p.name != ".git" for p in component.iterdir())


def _has_head(component: Path) -> bool:
    """
    True if *component* is its own repository with a baseline commit.

    The `.git` check is load-bearing, not belt-and-braces. `git -C <dir>
    rev-parse HEAD` walks up the directory tree, so in a workspace nested
    inside the user's own checkout it answers with the *enclosing* repository's
    HEAD - reporting success for a component that was never initialised at all.
    Confirming the component owns a `.git` first keeps the answer local to it.
    """
    if not (component / ".git").is_dir():
        return False
    result = _git(["rev-parse", "--verify", "HEAD"], component)
    return result is not None and result.returncode == 0


def _verify_baseline(components: list[Path]) -> None:
    """
    Fail loudly when a component has files but no baseline commit.

    Every implementation agent reads a real commit SHA back out of these
    repositories. Without a HEAD they each report "no git commit recorded for
    this component" - true, but it names a symptom three layers from the cause,
    which is almost always that `git add .` hit the 60-second cap on a tree far
    larger than it should have been. Saying so here costs one `rev-parse` per
    component and turns an afternoon into a sentence.

    A component with no files at all is left alone: git cannot commit nothing,
    and an empty directory is a real (if useless) component, not an error.
    """
    broken = [c.name for c in components if _has_content(c) and not _has_head(c)]
    if not broken:
        return
    where = components[0].parent
    raise RuntimeError(
        f"workspace baseline commit failed for: {', '.join(broken)} (in {where}). "
        "Two causes account for nearly all of these. On Windows, the workspace "
        f"path is long ({len(str(where))} characters here) and git refuses to "
        "write objects past the 260-character limit with 'Filename too long' - "
        "set INTERLOCK_WORKSPACE to somewhere short, or enable long paths. "
        f"Otherwise `git add .` exceeded its {_GIT_TIMEOUT_SECONDS}s limit on an "
        "oversized tree - check for large directories that are not build output, "
        "and so are not skipped by the copy."
    )


def prepare_workspace(change_id: str, components_root: str) -> Path:
    """
    Return an isolated, git-initialised copy of *components_root*.

    Idempotent: if the workspace already exists it is reused, so resuming a
    change after the coordination approval does not discard the provider patch
    made in the previous call.
    """
    # Absolute, deliberately. Agents are handed this path and some of them
    # decide whether to join it against their own repo_path by testing
    # `is_absolute()`. A relative workspace made that test fail and produced
    # `<workspace>/account-service/<workspace>/account-service`, so the
    # coexistence rehearsal never found the provider and every change came back
    # NOT_PROVEN_SAFE. It also makes the workspace independent of the caller's
    # working directory, which the CLI does not control.
    target = (workspace_root() / change_id).resolve()
    if _is_usable(target):
        return target
    if target.exists():
        # A workspace that exists but is incomplete gets rebuilt rather than
        # reused. This happens after an interrupted run, or on Windows where a
        # lingering subprocess handle makes `rm -rf` delete only part of the
        # tree. Reusing a half-built workspace produced a confusing
        # NOT_PROVEN_SAFE where every component failed for no stated reason.
        logger.warning("[real_workflow] rebuilding incomplete workspace %s", target)
        _force_rmtree(target)

    source = Path(components_root)
    if not source.is_dir():
        raise FileNotFoundError(f"components_root does not exist: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=_ignore_for(source))

    # Each component becomes its own repository so commit SHAs are per-component
    # and genuinely distinct.
    components = sorted(p for p in target.iterdir() if p.is_dir())
    for component in components:
        _git(["init"], component)
        _git(["config", "user.email", "interlock@example.com"], component)
        _git(["config", "user.name", "Interlock"], component)
        _git(["add", "."], component)
        _git(["commit", "-m", "baseline"], component)
    _verify_baseline(components)

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
    extra: dict[str, Any] | None = None,
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

    data: dict[str, Any] = {
        "change_id": change_id,
        "fixtures_root": str(workspace),
        "components_root": str(workspace),
        "provider": provider,
        # Absolute, not the bare component name. Agents that resolve this
        # against their own repo_path would otherwise join it twice.
        "provider_path": str(workspace / provider),
        "consumer": component,
        "old_field": old_symbol,
        "new_field": new_symbol,
        "change_request": change_request,
        "dependencies": ledger.get_dependencies(conn, change_id),
        "required_consumers": get_required_consumers(conn, change_id),
        "strategy_result": {},
        # Whether the provider is expected to serve the new symbol yet. Never a
        # constant: it depends on whether the provider patch has actually landed
        # at the point the agent runs. Callers that know override it.
        "expect_new": False,
    }
    if extra:
        data.update(extra)

    return {
        "change_id": change_id,
        "role": agent.role,
        "component": component,
        "repo_path": str(workspace / target),
        "base_url": os.environ.get("INTERLOCK_API_URL"),
        "data": data,
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
    extra: dict[str, Any] | None = None,
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
    context = _build_context(
        conn, change_id, spec_row, agent, workspace, component, extra
    )
    runner = AgentRunner(label, make_callable(agent), agent.output_schema)
    try:
        return runner.run(context)
    except AgentFailure as exc:
        # AgentRunner retries then wraps, so `str(exc)` is the wrapper's
        # "failed after 2 attempts" message. The agent's own explanation is on
        # the ImplementationFailed cause; prefer it, and keep any evidence the
        # agent produced so the reason survives into the ledger.
        cause = exc.__cause__ if isinstance(exc.__cause__, ImplementationFailed) else None
        detail = str(cause) if cause else str(exc)
        _record_risk(
            conn, change_id, component or agent.role, f"{agent.role}_failed", detail
        )
        if cause is not None and cause.evidence:
            for item in cause.evidence:
                ledger.add_evidence(
                    conn, change_id,
                    item.get("claim_type", "risk"),
                    item.get("subject", component or agent.role),
                    item.get("content", {}),
                    item.get("source_ref", agent.role),
                    item.get("confidence", "confirmed"),
                    item.get("source_revision"),
                )
        logger.warning("[real_workflow] %s failed: %s", label, detail)
        return None


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _already_failed(
    conn: sqlite3.Connection, change_id: str, component: str, step_kind: str
) -> bool:
    """True when this step is already recorded as failed for this change."""
    return any(
        w["component"] == component
        and w["step_kind"] == step_kind
        and w["status"] == "failed"
        for w in ledger.get_work_items(conn, change_id)
    )


def _implementation_mode(spec_row: dict) -> str:
    """`builtin` (Interlock edits) or `external` (it verifies someone else's work)."""
    return spec_row["spec"].get("implementation", "builtin")


def _discovery(conn, change_id, spec_row, workspace) -> None:
    sm.advance(conn, change_id)  # INTAKE -> DISCOVERY
    for agent in agents_for(spec_row["kind"], "DISCOVERY", _implementation_mode(spec_row)):
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
    for agent in agents_for(spec_row["kind"], "PLANNING", _implementation_mode(spec_row)):
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

    for agent in agents_for(spec_row["kind"], "MODIFY", _implementation_mode(spec_row)):
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


def _provider_patch_landed(conn, change_id, provider: str) -> bool:
    """Whether the provider patch is recorded as verified for this change."""
    return any(
        w["component"] == provider
        and w["step_kind"] == "provider_patch"
        and w["status"] == "verified"
        for w in ledger.get_work_items(conn, change_id)
    )


def _rehearse(conn, change_id, spec_row, workspace) -> None:
    sm.advance(conn, change_id)  # MODIFY -> REHEARSE

    provider = spec_row["spec"]["provider"]
    agents = agents_for(spec_row["kind"], "REHEARSE", _implementation_mode(spec_row))
    if not agents:
        _record_risk(
            conn, change_id, "coexistence-rehearsal", "rehearsal_not_run",
            "No rehearsal agent registered for this change kind.",
        )
        # No agent means no proof, and no proof must block. Recording the work
        # item is what makes that visible to the gate, which counts work items
        # and never reads evidence.
        ledger.upsert_work_item(
            conn, change_id, provider, "failed", REHEARSAL_STEP_KIND,
            {"error": "no rehearsal agent registered for this change kind"},
        )
        return

    # The rehearsal runs after _modify, so the provider is expected to serve the
    # new symbol exactly when its patch landed. This used to be hardcoded False,
    # which made the "new field present too early" assertion trip on every real
    # run — the rehearsal could only ever fail.
    expect_new = _provider_patch_landed(conn, change_id, provider)

    for agent in agents:
        result = _run_agent(
            conn, change_id, spec_row, agent, workspace,
            extra={"expect_new": expect_new},
        )
        if result is None:
            ledger.upsert_work_item(
                conn, change_id, provider, "failed", REHEARSAL_STEP_KIND,
                {"error": f"{agent.role} did not run"},
            )
            continue
        assert isinstance(result, VerificationResult)
        _write_evidence(conn, change_id, result.evidence)
        # A rehearsal that ran and failed previously wrote evidence and nothing
        # else, so the gate never saw it. It does now.
        ledger.upsert_work_item(
            conn, change_id, provider,
            "verified" if result.status == "verified" else "failed",
            REHEARSAL_STEP_KIND,
        )


def _verify(conn, change_id, spec_row, workspace, order: list[str]) -> None:
    sm.advance(conn, change_id)  # REHEARSE -> VERIFY

    for agent in agents_for(spec_row["kind"], "VERIFY", _implementation_mode(spec_row)):
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

            # Failure is sticky. A step that failed during MODIFY must not be
            # resurrected by a later passing test: a component that was never
            # migrated can still have a green suite of its own, and letting
            # VERIFY overwrite that would mark an unmigrated consumer safe.
            # Verification confirms a step; it never overturns a failure.
            if _already_failed(conn, change_id, component, agent.step_kind):
                _record_risk(
                    conn, change_id, component, "verification_after_failure",
                    f"{component}:{agent.step_kind} failed during implementation; "
                    f"a passing {agent.role} does not clear it.",
                )
                continue

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
