"""
tests/verification/conftest.py
================================
Pytest configuration for the verification test suite.

Adds an automatic skip for tests marked @pytest.mark.docker when Docker
is not available or not running.  This ensures the main test suite
(pytest tests/) passes cleanly without Docker Desktop.

To run Docker-marked tests explicitly:
    pytest -m docker tests/verification/
"""

from __future__ import annotations

import subprocess

import pytest


def _docker_available() -> bool:
    """Return True if 'docker info' exits 0 (Docker daemon is reachable)."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config, items):
    """
    Automatically skip @pytest.mark.docker tests when Docker is unavailable.
    """
    if _docker_available():
        return  # Docker is up — let all tests run

    skip_no_docker = pytest.mark.skip(
        reason="Docker daemon not available. Run with 'pytest -m docker' when Docker is running."
    )
    for item in items:
        if item.get_closest_marker("docker"):
            item.add_marker(skip_no_docker)
