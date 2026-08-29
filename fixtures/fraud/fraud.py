"""
fraud — consumer fixture for Interlock demo.

Checks whether the customer_id from an account-service response
is on the high-risk list.

This is a documented API consumer: it references account-service's
OpenAPI spec and reads the customer_id field from the response.
After migration, customer_id will be replaced by account_id.
"""
from __future__ import annotations

HIGH_RISK_IDS: set[str] = {"cust-bad", "cust-fraud-001", "cust-blocked"}


def check_fraud(account_response: dict) -> bool:
    """
    Return True if the customer is on the high-risk list.

    Reads account_response["customer_id"] — documented in account-service
    OpenAPI spec as the customer identifier field (pre-migration).
    """
    customer_id = account_response["customer_id"]
    return customer_id in HIGH_RISK_IDS


def get_risk_score(account_response: dict) -> float:
    """Return a numeric risk score (0.0 = safe, 1.0 = high risk)."""
    customer_id = account_response["customer_id"]
    return 1.0 if customer_id in HIGH_RISK_IDS else 0.0
