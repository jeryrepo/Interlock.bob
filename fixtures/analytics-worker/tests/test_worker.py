"""Tests for analytics-worker (pre-migration baseline).

These tests operate on the PRE-migration state: events carry account_id.
After migration, account_id will be replaced by account_id.
"""
from worker import process_event, batch_process


def test_process_event_uses_customer_id():
    event = {"account_id": "cust-456", "type": "purchase"}
    result = process_event(event)
    assert result["processed_for"] == "cust-456"


def test_process_event_preserves_type():
    event = {"account_id": "cust-789", "type": "view"}
    result = process_event(event)
    assert result["event_type"] == "view"


def test_process_event_metadata_carries_customer_id():
    event = {"account_id": "cust-abc", "type": "login"}
    result = process_event(event)
    assert result["metadata"]["account_id"] == "cust-abc"


def test_batch_process():
    events = [
        {"account_id": "cust-1", "type": "click"},
        {"account_id": "cust-2", "type": "buy"},
    ]
    results = batch_process(events)
    assert len(results) == 2
    assert results[0]["processed_for"] == "cust-1"
    assert results[1]["processed_for"] == "cust-2"
