"""
orchestrator/schemas/work.py
=============================
Work items: one row per (change, component, step_kind).

Generalises `consumer_migration`, whose name was the domain. A field rename
needs one step per consumer; a webhook-to-pub/sub cut-over needs two — the
subscriber moves, and the retired webhook drains.

The gate counts verified work items, so adding a step kind is how a change kind
expresses a stricter proof requirement without changing the gate's logic. See
ADR-0002.

No `depends_on` and no ordering column: `PlanningResult.migration_order` already
carries sequence and the runner iterates it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StepKind = Literal["provider_patch", "migrate", "subscribe", "webhook_quiet"]
WorkStatus = Literal["pending", "in_progress", "verified", "failed", "blocked"]


class WorkItem(BaseModel):
    change_id: str
    component: str
    step_kind: StepKind = "migrate"
    status: WorkStatus = "pending"
    detail: dict = Field(default_factory=dict)
