"""
agents/discovery/event_contract.py
=====================================
event-contract-discovery agent.  HIGHEST PRIORITY.

Discovers undocumented event consumers by inspecting Python AST for the
following three-condition pattern:

  1. An ast.Subscript where the object being subscripted is an ast.Name
     whose id is exactly "event".
  2. The subscript key is an ast.Constant matching the target field
     (default: "customer_id").
  3. The subscript node is enclosed in an ast.FunctionDef whose name
     contains any of: "event", "handle", "on_", "consume" (case-insensitive).

This combination is grounded in the actual analytics-worker source:
  def process_event(event: dict[str, Any]) -> ...:
      cid = event["customer_id"]   # ← matched: var=event, key=customer_id,
                                   #   function=process_event (contains "event")

checkout.py and fraud.py use `account_response["customer_id"]` — the
variable is named `account_response`, not `event` → NOT matched.

No component name is hardcoded. The consumer identity is derived entirely
from the directory containing the matched file.

Canonical edge direction:
  from_component = provider  (account-service, which emits the event)
  to_component   = consumer  (the event-consuming component)

e.g. account-service -> analytics-worker

Returns a dict that validates as DiscoveryResult.
Does NOT write to the database directly.
Does NOT call other agents.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from orchestrator.schemas import Dependency, DiscoveryResult, Evidence

# The provider that owns the event stream carrying the migrating field
_PROVIDER = "account-service"

# Directories/files to skip during scan
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "tests"}
_SKIP_FILES = {"conftest.py"}

# Function name substrings (lower-cased) that indicate an event handler
_HANDLER_KEYWORDS = ("event", "handle", "on_", "consume")


def _enclosing_function_name(tree: ast.Module, node: ast.AST) -> str | None:
    """
    Walk the AST tree to find the innermost FunctionDef that contains
    the given node.  Returns the function name, or None if not found.

    We build a parent map so we can walk upward from the node.
    """
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent

    current: ast.AST | None = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parent_map.get(id(current))
    return None


def _is_event_handler(func_name: str) -> bool:
    """Return True if the function name looks like an event handler."""
    name_lower = func_name.lower()
    return any(kw in name_lower for kw in _HANDLER_KEYWORDS)


def _find_event_consumer_refs(
    source: str,
    field: str,
) -> list[int]:
    """
    Use AST to find lines where `event["<field>"]` is accessed inside an
    event-handler function.

    Returns a sorted list of 1-based line numbers.  Returns empty list on
    parse errors.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    matched_lines: list[int] = []

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "event"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == field
        ):
            continue

        # Condition 3: enclosing function must look like an event handler
        func_name = _enclosing_function_name(tree, node)
        if func_name is not None and _is_event_handler(func_name):
            matched_lines.append(node.lineno)

    return sorted(set(matched_lines))


def run(data: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the event-contract-discovery agent.

    Expected keys in `data`:
      change_id     (str)  — identifier for the current migration
      fixtures_root (str)  — path to the directory containing fixture repos
      old_field     (str)  — field being migrated from (default: "customer_id")

    Returns a dict that validates as DiscoveryResult.
    """
    change_id: str = data["change_id"]
    old_field: str = data.get("old_field", "customer_id")

    if "fixtures_root" in data:
        fixtures_root = Path(data["fixtures_root"]).resolve()
    else:
        fixtures_root = (Path(__file__).parent.parent.parent / "fixtures").resolve()

    evidence: list[Evidence] = []
    dependencies: list[Dependency] = []

    # Walk every component directory (excluding the provider itself)
    component_dirs = sorted(
        p for p in fixtures_root.iterdir()
        if p.is_dir() and p.name != _PROVIDER
    )

    for component_dir in component_dirs:
        component = component_dir.name

        # Collect source Python files; skip test files and conftest
        py_files = [
            p for p in sorted(component_dir.rglob("*.py"))
            if not any(skip in p.parts for skip in _SKIP_DIRS)
            and p.name not in _SKIP_FILES
            and not p.name.startswith("test_")
        ]

        event_refs: list[dict[str, Any]] = []

        for py_file in py_files:
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            ref_lines = _find_event_consumer_refs(source, old_field)
            rel = py_file.relative_to(fixtures_root).as_posix()

            for lineno in ref_lines:
                event_refs.append({"file": rel, "line": lineno})

        if not event_refs:
            continue

        # This component is an event consumer — emit evidence and dependency
        first = event_refs[0]
        source_ref = f"{first['file']}:{first['line']}"

        evidence.append(
            Evidence(
                claim_type="dependency",
                subject=component,
                content={
                    "consumer": component,
                    "provider": _PROVIDER,
                    "field": old_field,
                    "detection_method": (
                        "AST: event[\"" + old_field + "\"] inside event-handler function"
                    ),
                    "refs": event_refs,
                },
                source_ref=source_ref,
                confidence="confirmed",
            )
        )

        dependencies.append(
            Dependency(
                from_component=_PROVIDER,
                to_component=component,
                edge_type="event",
                documentation_status="undocumented",
                reason=(
                    f"Undocumented event consumer: source accesses "
                    f"event[\"{old_field}\"] at {source_ref}"
                ),
            )
        )

    result = DiscoveryResult(
        change_id=change_id,
        evidence=evidence,
        dependencies=dependencies,
    )
    return result.model_dump()
