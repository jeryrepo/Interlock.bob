"""
orchestrator/schemas/common.py
==============================
Shared Pydantic models used across all agent result types.

These are the canonical definitions from the team contract.
Do NOT duplicate them elsewhere.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Evidence(BaseModel):
    """A single piece of structured evidence produced by an agent."""

    claim_type: Literal["dependency", "migration_status", "test_result", "risk"]
    subject: str
    content: dict
    source_ref: str
    confidence: Literal["hypothesis", "confirmed", "refuted"]
    source_revision: str | None = None


class Dependency(BaseModel):
    """A directed dependency edge between two components."""

    from_component: str
    to_component: str
    edge_type: Literal["api", "event", "db", "undocumented"]
    documentation_status: Literal["documented", "undocumented"] = "documented"
    reason: str | None = None
