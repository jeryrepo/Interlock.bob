"""Tests for account-service (pre-migration baseline).

These tests operate on the PRE-migration state: responses contain only
customer_id. After migration, account_id will also be present.
"""
from app import get_account


def test_get_account_returns_customer_id():
    result = get_account("cust-001")
    assert "customer_id" in result


def test_get_account_value_matches_input():
    result = get_account("cust-abc")
    assert result["customer_id"] == "cust-abc"


def test_get_account_no_account_id_pre_migration():
    """Pre-migration: account_id is NOT yet in the response."""
    result = get_account("cust-xyz")
    assert "account_id" not in result
