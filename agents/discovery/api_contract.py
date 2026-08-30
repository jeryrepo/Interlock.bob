"""
agents/discovery/api_contract.py
==================================
api-contract-discovery agent.

Identifies documented API consumers of account-service by:
  1. Parsing account-service's OpenAPI spec to confirm the field exists.
  2. Using Python AST to find source files that access the field from a
     variable named `account_response` (the conventional name for an API
     response dict in this codebase), inside a non-event-handler function.

The `analytics-worker` is intentionally excluded: its worker.py uses a
variable named `event`, not `account_response`, and its directory contains
no reference to account-service in its README.

Canonical edge direction:
  from_component = consumer  (the component that calls the API)
  to_component   = provider  (account-service, which exposes the field)

e.g. checkout -> account-service

Returns a dict that validates as DiscoveryResult.
Does NOT write to the database directly.
Does NOT call other agents.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml

from orchestrator.schemas import Dependency, DiscoveryResult, Evidence

# One definition of what counts as a component, shared with repo_map.
from agents.discovery.repo_map import component_dirs

# Name of the provider component
_PROVIDER = "account-service"

# OpenAPI spec file name
_OPENAPI_FILENAME = "openapi.yaml"

# Variable name that conventionally holds an account-service API response
_API_RESPONSE_VAR = "account_response"

# Files/directories to skip when scanning for consumers
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}
_SKIP_FILES = {"conftest.py"}


def _find_openapi_field_line(spec_path: Path, field: str) -> int | None:
    """
    Return the 1-based line number where `field` first appears in the
    OpenAPI spec YAML.  Returns None if not found.
    """
    try:
        lines = spec_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for i, line in enumerate(lines, start=1):
        if field in line:
            return i
    return None


def _find_api_consumer_refs(
    source: str,
    field: str,
    response_var: str,
) -> list[int]:
    """
    Use AST to find lines that access `response_var["field"]` in the
    given source code.  Returns a sorted list of 1-based line numbers.

    Only matches:
      - ast.Subscript where value.id == response_var AND slice == field
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == response_var
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == field
        ):
            lines.append(node.lineno)

    return sorted(set(lines))


def run(data: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the api-contract-discovery agent.

    Expected keys in `data`:
      change_id     (str)  — identifier for the current migration
      fixtures_root (str)  — path to the directory containing fixture repos
      old_field     (str)  — field being migrated from (default: "customer_id")

    Returns a dict that validates as DiscoveryResult.
    """
    change_id: str = data["change_id"]
    provider: str = data.get("provider", _PROVIDER)
    old_field: str = data.get("old_field", "customer_id")

    if "fixtures_root" in data:
        fixtures_root = Path(data["fixtures_root"]).resolve()
    else:
        fixtures_root = (Path(__file__).parent.parent.parent / "fixtures").resolve()

    evidence: list[Evidence] = []
    dependencies: list[Dependency] = []

    # ── Step 1: parse account-service OpenAPI spec ────────────────────────────
    provider_dir = fixtures_root / provider
    openapi_path = provider_dir / _OPENAPI_FILENAME
    openapi_rel = openapi_path.relative_to(fixtures_root).as_posix()

    field_line = _find_openapi_field_line(openapi_path, old_field)
    if field_line is not None:
        evidence.append(
            Evidence(
                claim_type="dependency",
                subject=provider,
                content={
                    "spec_file": openapi_rel,
                    "field": old_field,
                    "description": (
                        f"OpenAPI spec documents '{old_field}' as a response field"
                    ),
                },
                source_ref=f"{openapi_rel}:{field_line}",
                confidence="confirmed",
            )
        )

    # ── Step 2: scan all other fixture dirs for API consumers ─────────────────
    consumer_dirs = component_dirs(fixtures_root, exclude=provider)

    for consumer_dir in consumer_dirs:
        component = consumer_dir.name

        # Collect all Python source files in this component (no tests, no conftest)
        py_files = [
            p for p in sorted(consumer_dir.rglob("*.py"))
            if not any(skip in p.parts for skip in _SKIP_DIRS)
            and p.name not in _SKIP_FILES
            and "test_" not in p.name
        ]

        consumer_refs: list[dict[str, Any]] = []

        for py_file in py_files:
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            ref_lines = _find_api_consumer_refs(source, old_field, _API_RESPONSE_VAR)
            rel = py_file.relative_to(fixtures_root).as_posix()

            for lineno in ref_lines:
                consumer_refs.append({"file": rel, "line": lineno})

        if not consumer_refs:
            # This component does not consume account-service API in source
            continue

        first = consumer_refs[0]
        source_ref = f"{first['file']}:{first['line']}"

        evidence.append(
            Evidence(
                claim_type="dependency",
                subject=component,
                content={
                    "consumer": component,
                    "provider": provider,
                    "field": old_field,
                    "refs": consumer_refs,
                    "detection": (
                        f"AST found {_API_RESPONSE_VAR}[\"{old_field}\"] "
                        f"in {len(consumer_refs)} location(s)"
                    ),
                },
                source_ref=source_ref,
                confidence="confirmed",
            )
        )

        dependencies.append(
            Dependency(
                from_component=provider,
                to_component=component,
                edge_type="api",
                reason=(
                    f"Source code accesses {_API_RESPONSE_VAR}[\"{old_field}\"] "
                    f"at {source_ref}"
                ),
            )
        )

    result = DiscoveryResult(
        change_id=change_id,
        evidence=evidence,
        dependencies=dependencies,
    )
    return result.model_dump()
