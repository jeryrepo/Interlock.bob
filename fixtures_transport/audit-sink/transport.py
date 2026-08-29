"""Transport shim used by subscribers.

Both functions exist for the whole coexistence window. The migration
switches call sites from deliver_via_webhook to deliver_via_pubsub.
"""
from __future__ import annotations

from typing import Any


def deliver_via_webhook(event: dict[str, Any]) -> dict[str, Any]:
    return {"transport": "webhook", "event": event}


def deliver_via_pubsub(event: dict[str, Any]) -> dict[str, Any]:
    return {"transport": "pubsub", "topic": "accounts.events", "event": event}
