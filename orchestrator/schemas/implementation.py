"""
orchestrator/schemas/implementation.py
========================================
Result envelope returned by implementation agents:
  provider-patch and consumer-migration.
"""

from __future__ import annotations

from pydantic import BaseModel

from orchestrator.schemas.common import Evidence


class ImplementationResult(BaseModel):
    """Validated output from provider-patch or consumer-migration agent."""

    change_id: str
    consumer: str              # name of the service that was patched
    commit_ref: str | None     # Git commit SHA, or None if not yet committed
    evidence: list[Evidence]
