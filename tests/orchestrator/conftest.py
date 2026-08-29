"""tests/orchestrator/conftest.py — shared fixtures."""
import pytest
import orchestrator.ledger as ledger


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema applied."""
    c = ledger.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def change(conn):
    """A pre-created change request in INTAKE state."""
    import uuid
    cid = str(uuid.uuid4())
    return ledger.create_change(conn, cid, "customer_id -> account_id")
