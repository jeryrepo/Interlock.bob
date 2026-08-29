"""
analytics-worker — consumer fixture for Interlock demo.

Processes events that carry customer_id in the event payload.
"""
from __future__ import annotations

from typing import Any


def process_event(event: dict[str, Any]) -> dict[str, Any]:
    """Process an analytics event."""
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
    """Process a list of events."""
    return [process_event(e) for e in events]
