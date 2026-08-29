"""
agents/discovery/repo_map.py
==============================
repo-map discovery agent.

Walks every repository under fixtures_root and produces a structured
inventory of components, source files, OpenAPI specs, schema/migration
files, and field references.

This agent emits inventory evidence only. Specialized discovery agents own
dependency classification so one reference cannot create duplicate, competing
edges.

Returns a dict that validates as DiscoveryResult.
Does NOT write to the database directly.
Does NOT call other agents.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from orchestrator.schemas import DiscoveryResult, Evidence

# File extension categories
_SOURCE_EXTS = {".py"}
_OPENAPI_NAMES = {"openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml"}
_SCHEMA_EXTS = {".sql"}
_SCHEMA_NAME_PATTERNS = ("schema", "migration", "migrate", "alembic", "versions")
_EVENT_NAME_PATTERNS = ("event", "worker", "consumer", "handler", "listener", "subscriber")


def _is_schema_file(path: Path) -> bool:
    """Return True if a file is likely a schema or migration file."""
    if path.suffix in _SCHEMA_EXTS:
        return True
    name_lower = path.name.lower()
    return any(p in name_lower for p in _SCHEMA_NAME_PATTERNS)


def _is_event_file(path: Path) -> bool:
    """Return True if a file name suggests event handling."""
    name_lower = path.stem.lower()
    return any(p in name_lower for p in _EVENT_NAME_PATTERNS)


def _find_field_refs_in_python(source: str, field: str) -> list[int]:
    """
    Return line numbers where `field` appears as a subscript key in the
    AST of the given source code.  Returns an empty list on parse errors.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        # Subscript access: x["field"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == field
        ):
            lines.append(node.lineno)

    return sorted(set(lines))


def _find_field_refs_in_text(text: str, field: str) -> list[int]:
    """Return 1-based line numbers where `field` appears in plain text."""
    return [
        i + 1
        for i, line in enumerate(text.splitlines())
        if field in line
    ]


def _scan_component(
    component_dir: Path,
    fixtures_root: Path,
    field: str,
) -> dict[str, Any]:
    """
    Walk a single component directory and return a summary dict:
      {
        "name": str,
        "source_files": [str, ...],
        "openapi_files": [str, ...],
        "schema_files": [str, ...],
        "event_files": [str, ...],
        "field_refs": [{"file": str, "line": int}, ...],
      }
    """
    name = component_dir.name
    source_files: list[str] = []
    openapi_files: list[str] = []
    schema_files: list[str] = []
    event_files: list[str] = []
    field_refs: list[dict[str, Any]] = []

    for path in sorted(component_dir.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(fixtures_root).as_posix()

        # Categorise
        if path.name in _OPENAPI_NAMES:
            openapi_files.append(rel)
        elif _is_schema_file(path):
            schema_files.append(rel)

        if path.suffix == ".py":
            source_files.append(rel)
            if _is_event_file(path):
                event_files.append(rel)

        # Skip binary and compiled files
        if path.suffix in {".pyc", ".pyo", ".so", ".dll", ".exe"}:
            continue

        # Scan for field references
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if path.suffix == ".py":
            ref_lines = _find_field_refs_in_python(text, field)
        else:
            ref_lines = _find_field_refs_in_text(text, field)

        for lineno in ref_lines:
            field_refs.append({"file": rel, "line": lineno})

    return {
        "name": name,
        "source_files": source_files,
        "openapi_files": openapi_files,
        "schema_files": schema_files,
        "event_files": event_files,
        "field_refs": field_refs,
    }


def run(data: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the repo-map discovery agent.

    Expected keys in `data`:
      change_id    (str)  — identifier for the current migration
      fixtures_root (str) — path to the directory containing fixture repos
                            (defaults to "fixtures/" relative to project root)
      old_field    (str)  — field being migrated from (default: "customer_id")

    Returns a dict that validates as DiscoveryResult.
    """
    change_id: str = data["change_id"]
    old_field: str = data.get("old_field", "customer_id")

    # Resolve fixtures_root
    if "fixtures_root" in data:
        fixtures_root = Path(data["fixtures_root"]).resolve()
    else:
        # Default: fixtures/ sibling of the project root
        fixtures_root = (Path(__file__).parent.parent.parent / "fixtures").resolve()

    evidence: list[Evidence] = []
    dependencies: list[Dependency] = []

    # Walk each immediate subdirectory as a component
    component_dirs = sorted(
        p for p in fixtures_root.iterdir() if p.is_dir()
    )

    for component_dir in component_dirs:
        summary = _scan_component(component_dir, fixtures_root, old_field)
        name = summary["name"]

        # Choose a representative source_ref for this component:
        # prefer first field_ref, fall back to first source file, then the dir itself
        if summary["field_refs"]:
            first_ref = summary["field_refs"][0]
            source_ref = f"{first_ref['file']}:{first_ref['line']}"
        elif summary["source_files"]:
            source_ref = summary["source_files"][0]
        else:
            source_ref = component_dir.relative_to(fixtures_root).as_posix()

        # Emit one Evidence per component
        evidence.append(
            Evidence(
                claim_type="dependency",
                subject=name,
                content={
                    "source_files": summary["source_files"],
                    "openapi_files": summary["openapi_files"],
                    "schema_files": summary["schema_files"],
                    "event_files": summary["event_files"],
                    "field_refs": summary["field_refs"],
                },
                source_ref=source_ref,
                confidence="confirmed",
            )
        )

        # repo-map inventories references; the specialised discovery agents own
        # dependency classification so the ledger never contains competing
        # duplicate edges whose type depends on execution order.

    result = DiscoveryResult(
        change_id=change_id,
        evidence=evidence,
        dependencies=dependencies,
    )
    return result.model_dump()
