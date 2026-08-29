"""
analytics-worker — consumer fixture for Interlock demo.

Processes events that carry customer_id directly in the event payload.
This is the canonical 'undocumented' dependency that the discovery agents
must find from source code — it is never listed in an API contract.
"""
from __future__ import annotations

from typing import Any


def process_event(event: dict[str, Any]) -> dict[str, Any]:
    """
    Process an analytics event.

    Reads event["customer_id"] directly — no API contract documents this.
    Discovery agents must find this dependency from source inspection.
    After migration this will become event["account_id"].
    """
    cid = event["customer_id"]
    return {
        "processed_for": cid,
        "event_type": event.get("type", "unknown"),
        "metadata": {
            "customer_id": cid,
            "raw_event_keys": list(event.keys()),
        },
    }


def batch_process(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Process a list of events, each carrying customer_id."""
    return [process_event(e) for e in events]
