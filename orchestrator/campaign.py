"""
orchestrator/campaign.py
=========================
Run a set of related changes as one unit, and report one combined verdict.

Why this exists
---------------
Real migrations are not one change. "Move the estate off `customer_id`" is a
field rename, then an API contract change against the same provider, then a
transport migration for the service that was subscribing to the old event — and
they have an order. Run them one at a time and you find out about the ordering
constraint when the third one fails.

A campaign is that set, run in dependency order, with a verdict for each and one
for the whole.

What a campaign is NOT
----------------------
**It is not a second gate.** `combine()` below is a fold over verdicts that
`gate.evaluate_gate()` already produced, and it does exactly one thing: a
campaign is VERIFIED when every change in it is VERIFIED, and NOT_PROVEN_SAFE
otherwise. It never re-derives a verdict, never overrides one, and cannot turn a
NOT_PROVEN_SAFE change into a passing campaign. AGENTS.md invariant 1 says the
gate lives in one place; this does not become a second one.

**The plan is not a verdict either.** A model may propose the decomposition from
a sentence — that is what makes a large request tractable — but every change it
proposes then runs the entire deterministic pipeline unchanged, and a proposed
change that does not validate as a ChangeSpec, or names a provider that is not a
component, is discarded before anything runs. The model chooses what to check.
It has never had, and does not gain here, any say in what passes.

Ordering
--------
Deterministic, and derived from the plan rather than asked of a model: if change
B's provider is a consumer in change A, A runs first. Ties break on the order
written in the plan, so the same plan always runs the same way. A cycle is
reported rather than resolved — two changes that each require the other is a
fact about the plan the author needs to see, not something to silently linearise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from orchestrator.schemas import ChangeSpec

_SPEC_ADAPTER = TypeAdapter(ChangeSpec)

MAX_CHANGES = 25
"""
A campaign is a migration, not a batch job.

Each change copies the component tree and runs every component's real test
suite, so twenty-five is already many minutes of work. The cap exists so a
malformed plan - or a model asked for "everything" - fails immediately with a
clear message instead of after an hour.
"""


@dataclass
class PlannedChange:
    """One change in a campaign, before it has run."""

    name: str
    spec: dict[str, Any]
    reason: str = ""

    @property
    def provider(self) -> str:
        return self.spec.get("provider", "")

    @property
    def symbols(self) -> tuple[str, str]:
        return (
            self.spec.get("old_field") or self.spec.get("old_symbol") or "",
            self.spec.get("new_field") or self.spec.get("new_symbol") or "",
        )


@dataclass
class CampaignResult:
    """What happened, per change and overall."""

    description: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    result: str = "NOT_PROVEN_SAFE"
    reason: str = ""
    order: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "result": self.result,
            "reason": self.reason,
            "order": list(self.order),
            "changes": list(self.changes),
        }


# ---------------------------------------------------------------------------
# Building a plan
# ---------------------------------------------------------------------------

def build_plan(
    entries: list[dict[str, Any]], components_root: str
) -> tuple[list[PlannedChange], list[str]]:
    """
    Validate raw plan entries into PlannedChanges.

    Returns `(planned, problems)`. A bad entry never stops the others from being
    read: the caller shows every problem at once, because fixing a plan one
    error per run is how people give up on a tool.
    """
    from interlock_cli.core import build_spec, provider_problem

    planned: list[PlannedChange] = []
    problems: list[str] = []

    if len(entries) > MAX_CHANGES:
        problems.append(
            f"a campaign is capped at {MAX_CHANGES} changes; this plan has "
            f"{len(entries)}. Split it, or run the parts separately."
        )
        return planned, problems

    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        name = str(entry.get("name") or f"change-{index}")
        if name in seen:
            problems.append(f"{name}: duplicate change name")
            continue
        seen.add(name)

        provider = str(entry.get("provider", "")).strip()
        old = str(entry.get("old") or entry.get("old_symbol") or "").strip()
        new = str(entry.get("new") or entry.get("new_symbol") or "").strip()
        if not provider or not old or not new:
            problems.append(f"{name}: needs provider, old and new")
            continue

        issue = provider_problem(provider, components_root)
        if issue:
            problems.append(f"{name}: {issue}")
            continue

        try:
            spec = build_spec(
                str(entry.get("kind", "field_rename")),
                provider, old, new, components_root,
                entry.get("topic"), entry.get("webhook_path"), entry.get("endpoint"),
                str(entry.get("implementation", "builtin")),
            )
        except (ValidationError, ValueError) as exc:
            problems.append(f"{name}: invalid change spec: {exc}")
            continue

        planned.append(
            PlannedChange(name=name, spec=spec, reason=str(entry.get("reason", "")))
        )

    return planned, problems


def load_plan_file(path: str) -> list[dict[str, Any]]:
    """
    Read a campaign plan from YAML or JSON.

    The deterministic path: no model, no credentials, reproducible. A plan file
    checked into the repository is also reviewable, which a sentence typed at a
    terminal is not.
    """
    import json

    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml

        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)

    if isinstance(raw, dict):
        if "changes" not in raw:
            # Say which key is missing. Defaulting to an empty list turned a
            # typo'd key into "no runnable changes", which reads as a problem
            # with the repository rather than with the file just written.
            raise ValueError(
                f"a plan mapping must have a 'changes' key; found: "
                f"{sorted(raw)[:8] or 'nothing'}"
            )
        raw = raw["changes"]
    if not isinstance(raw, list):
        raise ValueError("a plan must be a list of changes, or {changes: [...]}")
    return [entry for entry in raw if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def order_changes(
    planned: list[PlannedChange], consumers_of: dict[str, set[str]]
) -> tuple[list[PlannedChange], list[str]]:
    """
    Order changes so a provider is changed before anything that consumes it.

    `consumers_of` maps a provider to the components that depend on it, as
    discovered — not guessed here. If change B's provider is a consumer in
    change A, A must run first, or B migrates against a contract that is about
    to move underneath it.

    A stable topological sort: ties keep the order written in the plan, so the
    same plan always runs the same way. Returns `(ordered, cycles)`; a cycle is
    reported rather than broken, because two changes that each require the other
    is a fact the author has to resolve.
    """
    by_provider: dict[str, list[PlannedChange]] = {}
    for change in planned:
        by_provider.setdefault(change.provider, []).append(change)

    blockers: dict[str, set[str]] = {c.name: set() for c in planned}
    for change in planned:
        dependents = consumers_of.get(change.provider, set())
        for other in planned:
            if other.name == change.name:
                continue
            if other.provider in dependents:
                # `other` consumes what `change` provides, so `change` first.
                blockers[other.name].add(change.name)

    # Drop mutual constraints before sorting.
    #
    # Two changes to the SAME symbol on different providers each show up among
    # the other's consumers, because discovery reports every component that
    # references the symbol without asserting which end owns it. That is a
    # symmetric pair, and a symmetric pair carries no ordering information — it
    # is not evidence that either must run first. Treating it as a cycle
    # deadlocked plans that were perfectly runnable, so mutual edges are
    # removed and those changes fall back to the order the author wrote.
    #
    # A cycle that survives this is a real one: A before B before C before A,
    # with no pair mutually blocked. That is still reported rather than broken.
    for change in planned:
        for other in planned:
            if change.name in blockers[other.name] and other.name in blockers[change.name]:
                blockers[other.name].discard(change.name)
                blockers[change.name].discard(other.name)

    ordered: list[PlannedChange] = []
    remaining = list(planned)
    resolved: set[str] = set()

    while remaining:
        ready = [c for c in remaining if not (blockers[c.name] - resolved)]
        if not ready:
            # Everything left is in, or behind, a cycle.
            return ordered, sorted(c.name for c in remaining)
        for change in ready:
            ordered.append(change)
            resolved.add(change.name)
        remaining = [c for c in remaining if c.name not in resolved]

    return ordered, []


# ---------------------------------------------------------------------------
# The combined verdict
# ---------------------------------------------------------------------------

def combine(results: list[dict[str, Any]]) -> tuple[str, str]:
    """
    Fold per-change verdicts into one. Decides nothing on its own.

    A campaign is VERIFIED when every change in it is VERIFIED. Anything else -
    one unproven change, one that could not run, an empty plan - is
    NOT_PROVEN_SAFE. There is no threshold, no majority, and no notion of
    "mostly safe": the whole point of a campaign is that the changes are
    related, so a single unproven one means the estate is not in a state anyone
    verified.

    An empty campaign is NOT_PROVEN_SAFE rather than trivially VERIFIED. Nothing
    was checked, so nothing was proven, and returning VERIFIED for a plan that
    did nothing is exactly the fabricated result invariant 4 forbids.
    """
    if not results:
        return "NOT_PROVEN_SAFE", "The campaign contained no changes, so nothing was proven."

    unproven = [r["name"] for r in results if r.get("result") != "VERIFIED"]
    if unproven:
        return "NOT_PROVEN_SAFE", (
            f"{len(unproven)} of {len(results)} change(s) are not proven safe: "
            + ", ".join(unproven)
        )
    return "VERIFIED", f"All {len(results)} change(s) in this campaign are proven safe."


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def run_campaign(
    conn: sqlite3.Connection,
    description: str,
    planned: list[PlannedChange],
    stop_on_failure: bool = True,
) -> CampaignResult:
    """
    Run each change through the ordinary pipeline and combine the verdicts.

    Every change goes through `core.check`, which is the same entry point a
    single change uses — same agents, same state machine, same gate. Nothing
    here is a shortcut around any of that.

    `stop_on_failure` defaults to True because the changes are ordered by
    dependency: once one fails, the ones after it would be migrating against a
    contract that never moved, and their verdicts would describe a world that
    does not exist. They are reported as `not_run`, which is honest, rather than
    given a verdict nothing earned.
    """
    from interlock_cli import core

    outcome = CampaignResult(description=description)
    outcome.order = [c.name for c in planned]
    stopped = False

    for change in planned:
        if stopped:
            outcome.changes.append({
                "name": change.name,
                "provider": change.provider,
                "result": "not_run",
                "reason": "an earlier change in this campaign was not proven safe",
                "change_id": None,
            })
            continue

        old, new = change.symbols
        try:
            status = core.check(conn, f"{old} -> {new}", change.spec)
            gate = status["gate"]
            outcome.changes.append({
                "name": change.name,
                "provider": change.provider,
                "change_id": status["change_id"],
                "result": gate["result"],
                "reason": gate["reason"],
                "unresolved": gate.get("unresolved") or [],
            })
            if gate["result"] != "VERIFIED" and stop_on_failure:
                stopped = True
        except Exception as exc:  # noqa: BLE001
            # A change that could not run is not a change that passed. Recorded
            # as a failure with its reason, never skipped.
            outcome.changes.append({
                "name": change.name,
                "provider": change.provider,
                "change_id": None,
                "result": "NOT_PROVEN_SAFE",
                "reason": f"{type(exc).__name__}: {exc}",
                "unresolved": [],
            })
            if stop_on_failure:
                stopped = True

    ran = [c for c in outcome.changes if c["result"] != "not_run"]
    outcome.result, outcome.reason = combine(ran)
    skipped = len(outcome.changes) - len(ran)
    if skipped:
        outcome.reason += f" {skipped} further change(s) were not attempted."
    return outcome
