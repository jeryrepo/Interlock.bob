"""
Shared pytest fixtures for tests/implementation/.

Every fixture creates an isolated temporary Git repository using pytest's
``tmp_path`` so that NO commits ever land on the feature/planning branch.

Fixture repositories mimic the expected fixture structure so that the agents
can operate against them identically to how they would operate against the
real fixtures/ directories once Person 2's work lands.

Git identity is configured locally within each temp repo so commits succeed
in any CI/developer environment without touching global git config.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str:
    """Run git command in cwd; raise if it fails."""
    result = subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialise a bare git repo with local identity."""
    _git(["init"], path)
    _git(["config", "user.email", "test@interlock.dev"], path)
    _git(["config", "user.name", "Interlock Test"], path)


def _initial_commit(path: Path, message: str = "initial") -> str:
    """Stage everything and create the initial commit; return SHA."""
    _git(["add", "."], path)
    _git(["commit", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path)


# ---------------------------------------------------------------------------
# Provider repo fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_provider_repo(tmp_path: Path) -> Path:
    """
    Minimal account-service-like repository under tmp_path.

    Structure
    ---------
    account-service/
        app.py              — Pydantic model with customer_id field
        openapi.yaml        — OpenAPI spec with customer_id in response schema
        tests/
            __init__.py
            test_app.py     — existing test that asserts customer_id

    An initial git commit is created so HEAD is valid.
    The fixture yields the repo root path.
    Cleanup is automatic (tmp_path lifecycle).
    """
    repo = tmp_path / "account-service"
    repo.mkdir()
    _init_repo(repo)

    # app.py — Pydantic model style
    (repo / "app.py").write_text(textwrap.dedent("""\
        from typing import Optional

        class AccountResponse:
            customer_id: Optional[str] = None

        def get_account(account_id_param: str) -> dict:
            return {
                "customer_id": account_id_param,
            }
    """), encoding="utf-8")

    # openapi.yaml
    (repo / "openapi.yaml").write_text(textwrap.dedent("""\
        openapi: "3.0.0"
        info:
          title: Account Service
          version: "1.0"
        paths:
          /accounts/{id}:
            get:
              responses:
                "200":
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          customer_id:
                            type: string
                            description: 'Legacy customer identifier'
    """), encoding="utf-8")

    # tests/
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_app.py").write_text(textwrap.dedent("""\
        from app import get_account

        def test_customer_id_present():
            response = get_account("cust-123")
            assert "customer_id" in response
    """), encoding="utf-8")

    _initial_commit(repo)
    return repo


# ---------------------------------------------------------------------------
# Provider repo fixture — deliberately broken test (for failure-gate test)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_provider_repo_broken_tests(tmp_path: Path) -> Path:
    """
    Like tmp_provider_repo but the existing test is intentionally broken.
    Used to verify that a failing test suite prevents a successful commit.
    """
    repo = tmp_path / "account-service-broken"
    repo.mkdir()
    _init_repo(repo)

    (repo / "app.py").write_text(textwrap.dedent("""\
        class AccountResponse:
            customer_id: str = ""
    """), encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_app.py").write_text(textwrap.dedent("""\
        def test_always_fails():
            assert False, "This test is intentionally broken"
    """), encoding="utf-8")

    _initial_commit(repo)
    return repo


# ---------------------------------------------------------------------------
# Standard change-request payload
# ---------------------------------------------------------------------------

@pytest.fixture
def patch_data() -> dict:
    """Minimal change_request dict for provider-patch tests."""
    return {
        "change_request": {
            "id": "cr-001",
            "old_field": "customer_id",
            "new_field": "account_id",
            "provider": "account-service",
        },
        "strategy_result": {},
    }
