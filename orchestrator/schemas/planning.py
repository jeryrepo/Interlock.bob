"""
orchestrator/schemas/planning.py
=================================
Result envelope returned by the planning agent (compatibility-strategy).
"""

from __future__ import annotations

from pydantic import BaseModel

from orchestrator.schemas.common import Evidence


class PlanningResult(BaseModel):
    """Validated output from the compatibility-strategy agent."""

    change_id: str
    migration_order: list[str]   # ordered list of consumer names
    evidence: list[Evidence]
