"""
event-publisher — provider fixture for the webhook -> pub/sub migration.

PRE-MIGRATION state: events are delivered by calling deliver_via_webhook.
During the coexistence window the publisher gains deliver_via_pubsub and
keeps the webhook path, so subscribers can be cut over one at a time.
"""
from __future__ import annotations

from typing import Any


def deliver_via_webhook(event: dict[str, Any]) -> dict[str, Any]:
    """Deliver an event over the legacy HTTP webhook."""
    return {"transport": "webhook", "path": "/hooks/accounts", "event": event}


def publish(event: dict[str, Any]) -> dict[str, Any]:
    """Publish one event using the currently configured transport."""
    return deliver_via_webhook(event)
