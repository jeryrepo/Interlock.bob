"""
Shared pytest fixtures.

The autouse fixture below closes any SQLite ledger a CLI command opened during
a test. CliRunner keeps the raised exception, its traceback keeps the frame, and
the frame keeps the connection alive — which on Windows blocks deletion of the
temp directory holding the file, so a later run fails while cleaning up an
earlier one's directory.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _close_cli_ledgers():
    yield
    try:
        from interlock_cli import core
    except ImportError:
        return
    core.close_ledgers()
