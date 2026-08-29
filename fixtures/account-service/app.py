"""
account-service — provider fixture for Interlock demo.

Exposes a customer account by customer_id.
This is the field that will be migrated: customer_id -> account_id.
"""
from __future__ import annotations

from typing import Optional


class AccountResponse:
    """Response model for the /accounts endpoint."""
    customer_id: Optional[str] = None

    def __init__(self, customer_id: str):
        self.customer_id = customer_id

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "account_id": self.customer_id,
        }


def get_account(customer_id: str) -> dict:
    """Return account data for the given customer_id."""
    account = AccountResponse(customer_id=customer_id)
    return account.to_dict()
