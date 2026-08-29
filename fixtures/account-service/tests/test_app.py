"""Tests for account-service.

Structural tests that hold regardless of migration state.
"""
from app import get_account


def test_get_account_returns_customer_id():
    result = get_account("cust-001")
    assert "customer_id" in result


def test_get_account_value_matches_input():
    result = get_account("cust-abc")
    assert result["customer_id"] == "cust-abc"
