"""Tests for checkout service (pre-migration baseline).

These tests operate on the PRE-migration state: account-service responses
carry account_id. After migration, account_id will be replaced by account_id.
"""
from checkout import process_order


def test_process_order_uses_customer_id():
    response = {"account_id": "cust-123"}
    result = process_order(response, item="widget")
    assert result["order_customer"] == "cust-123"


def test_process_order_status_pending():
    result = process_order({"account_id": "cust-x"}, item="gadget")
    assert result["status"] == "pending"


def test_process_order_item_preserved():
    result = process_order({"account_id": "cust-y"}, item="keyboard", quantity=2)
    assert result["item"] == "keyboard"
    assert result["quantity"] == 2
