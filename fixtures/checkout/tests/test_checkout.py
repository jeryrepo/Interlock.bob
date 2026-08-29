"""Tests for checkout service (pre-migration baseline)."""
from checkout import process_order


def test_process_order_uses_customer_id():
    response = {"customer_id": "cust-123"}
    result = process_order(response, item="widget")
    assert result["order_customer"] == "cust-123"


def test_process_order_status_pending():
    result = process_order({"customer_id": "cust-x"}, item="gadget")
    assert result["status"] == "pending"
