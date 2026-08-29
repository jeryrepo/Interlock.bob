"""
orchestrator/schemas/verification.py
======================================
Result envelope returned by verification agents:
  contract-test, coexistence-rehearsal, and critic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from orchestrator.schemas.common import Evidence


class VerificationResult(BaseModel):
    """Validated output from a verification agent."""

    change_id: str
    consumer: str
    status: Literal["verified", "failed"]
    evidence: list[Evidence]
