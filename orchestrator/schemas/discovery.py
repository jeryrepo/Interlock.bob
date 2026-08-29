"""
orchestrator/schemas/discovery.py
==================================
Result envelope returned by all discovery-phase agents:
  repo-map, api-contract-discovery, event-contract-discovery, db-schema-discovery.
"""

from __future__ import annotations

from pydantic import BaseModel

from orchestrator.schemas.common import Dependency, Evidence


class DiscoveryResult(BaseModel):
    """Validated output from any discovery agent."""

    change_id: str
    evidence: list[Evidence]
    dependencies: list[Dependency]
