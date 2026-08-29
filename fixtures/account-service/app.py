"""
account-service — provider fixture for Interlock demo.

Exposes customer account data by customer_id.
This is the field that will be migrated: customer_id -> account_id.

PRE-MIGRATION state: responses contain only customer_id.
Post-migration: responses will contain both customer_id (deprecated)
and account_id (new canonical identifier).
"""
from __future__ import annotations

from typing import Optional


class AccountResponse:
    """Response model for the /accounts endpoint (pre-migration)."""

    def __init__(self, customer_id: str):
        self.customer_id: Optional[str] = customer_id

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "account_id": self.customer_id,
        }


def get_account(customer_id: str) -> dict:
    """Return account data for the given customer_id."""
    account = AccountResponse(customer_id=customer_id)
    return account.to_dict()
