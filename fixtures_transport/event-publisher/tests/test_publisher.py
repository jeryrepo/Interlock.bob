"""Tests for event-publisher (pre-migration baseline)."""
from publisher import deliver_via_webhook, publish


def test_webhook_delivery_works():
    result = deliver_via_webhook({"id": "evt-1"})
    assert result["transport"] == "webhook"


def test_publish_uses_a_transport():
    assert publish({"id": "evt-2"})["event"]["id"] == "evt-2"
