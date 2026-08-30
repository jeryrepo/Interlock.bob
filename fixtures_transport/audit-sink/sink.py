"""
audit-sink — UNDOCUMENTED subscriber.

Nothing links this service to event-publisher: no contract, no config, no
README reference. It calls the webhook transport directly from source, which
is the only way discovery can find it.
"""
from __future__ import annotations

from typing import Any

import transport


def on_account_event(event: dict[str, Any]) -> dict[str, Any]:
    record = transport.deliver_via_webhook(event)
    return {"audited": event.get("id"), "transport": record["transport"]}
