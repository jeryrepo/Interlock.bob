"""
tests/discovery/conftest.py
==============================
Shared pytest fixtures for discovery agent tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_root() -> Path:
    """Absolute path to the fixtures/ directory."""
    return (Path(__file__).parent.parent.parent / "fixtures").resolve()


@pytest.fixture(scope="session")
def base_data(fixtures_root: Path) -> dict:
    """Minimal valid input dict for all discovery agents."""
    return {
        "change_id": "test-discovery-001",
        "fixtures_root": str(fixtures_root),
        "old_field": "customer_id",
    }
