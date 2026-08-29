"""
checkout — consumer fixture for Interlock demo.

Calls account-service and uses customer_id from the API response
to associate orders with customers.

This is a documented API consumer: it references account-service's
OpenAPI spec and reads the customer_id field from the response.
After migration, customer_id will be replaced by account_id.
"""
from __future__ import annotations


def process_order(account_response: dict, item: str, quantity: int = 1) -> dict:
    """
    Create an order for the customer identified in account_response.

    Reads account_response["customer_id"] — documented in account-service
    OpenAPI spec as the customer identifier field (pre-migration).
    """
    cid = account_response["customer_id"]
    return {
        "order_customer": cid,
        "item": item,
        "quantity": quantity,
        "status": "pending",
    }
