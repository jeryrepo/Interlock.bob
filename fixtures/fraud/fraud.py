"""
fraud — consumer fixture for Interlock demo.

Checks whether the account_id from an account-service response
is on the high-risk list.
"""
from __future__ import annotations

HIGH_RISK_IDS: set[str] = {"cust-bad", "cust-fraud-001", "cust-blocked"}


def check_fraud(account_response: dict) -> bool:
    """
    Return True if the customer is on the high-risk list.

    Reads 'account_id' from account_response (pre-migration).
    Will be migrated to read 'account_id' instead.
    """
    account_id = account_response["account_id"]
    return account_id in HIGH_RISK_IDS


def get_risk_score(account_response: dict) -> float:
    """Return a numeric risk score (0.0 = safe, 1.0 = high risk)."""
    account_id = account_response["account_id"]
    return 1.0 if account_id in HIGH_RISK_IDS else 0.0
