"""
analytics-worker — consumer fixture for Interlock demo.

Processes events that carry customer_id directly in the event payload.
This dependency is intentionally undocumented — no API contract, no README
reference, no configuration links this service to account-service.

Discovery agents must find this dependency purely from source-code inspection.
"""
from __future__ import annotations

from typing import Any


def process_event(event: dict[str, Any]) -> dict[str, Any]:
    """
    Process an analytics event.

    Directly accesses event["customer_id"] — this is the undocumented
    dependency that discovery must find via AST inspection.
    No API spec, no service-registry entry, no comment documents this link.
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
