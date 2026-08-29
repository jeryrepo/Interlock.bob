"""
checkout — consumer fixture for Interlock demo.

Calls account-service and uses account_id from the API response
to associate orders with customers.
"""
from __future__ import annotations


def process_order(account_response: dict, item: str, quantity: int = 1) -> dict:
    """
    Create an order for the customer identified in account_response.

    account_response is expected to contain 'account_id' (pre-migration)
    which will be replaced by 'account_id' after migration.
    """
    cid = account_response["account_id"]
    return {
        "order_customer": cid,
        "item": item,
        "quantity": quantity,
        "status": "pending",
    }
