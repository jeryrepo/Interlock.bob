"""
account-service — HTTP surface for the Interlock coexistence rehearsal.

Why this file is separate from ``app.py``
-----------------------------------------
``app.py`` holds the account payload logic and is the file the provider-patch
agent rewrites during a migration. Keeping the web layer out of it means:

- ``from app import get_account`` still works with no web framework installed,
  so the fixture's own unit tests stay dependency-free;
- the provider-patch agent's regexes operate on exactly the code they were
  written for, with no HTTP scaffolding to trip over;
- the migration story stays entirely in ``app.py``, where the demo expects it.

This module therefore delegates and does nothing else. The path parameter is
deliberately named ``key`` rather than ``customer_id`` so that migrating the
provider does not incidentally rewrite this file.
"""
from __future__ import annotations

from fastapi import FastAPI

from app import get_account

app = FastAPI(
    title="Account Service",
    description="Provider fixture for the Interlock coexistence rehearsal.",
    version="1.0",
)


@app.get("/health")
def health() -> dict:
    """Liveness probe used by docker compose to gate dependent services."""
    return {"status": "ok"}


@app.get("/accounts/{key}")
def read_account(key: str) -> dict:
    """
    Return the account payload for *key*.

    The response shape is whatever ``app.get_account`` currently produces —
    pre-migration that is ``customer_id`` alone; during the coexistence window
    it is both fields. The rehearsal asserts against this endpoint to prove
    old and new consumers can be served simultaneously.
    """
    return get_account(key)
