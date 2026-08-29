"""
notifier — documented subscriber.

Receives account events through the publisher's webhook transport. After
the migration it must consume from the pub/sub topic instead.
"""
from __future__ import annotations

from typing import Any

import transport


def handle_account_event(event: dict[str, Any]) -> dict[str, Any]:
    """Handle one account event delivered over the current transport."""
    delivery = transport.deliver_via_webhook(event)
    return {"notified": event.get("id"), "via": delivery["transport"]}
