"""
agents/discovery/db_schema.py
===============================
db-schema-discovery agent.

Scans fixture repositories for schema and migration files that reference
the target field (default: "customer_id").  Uses simple line-by-line text
search (appropriate for SQL and config files, which are not Python AST).

Canonical edge direction:
  from_component = provider  (account-service, which owns the field)
  to_component   = consumer  (the component whose schema references the field)

e.g. account-service -> platform-config

Returns a dict that validates as DiscoveryResult.
Does NOT write to the database directly.
Does NOT call other agents.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.schemas import Dependency, DiscoveryResult, Evidence

# The provider that owns the field being migrated
_PROVIDER = "account-service"

# File extension / name patterns that indicate a schema or migration file
_SCHEMA_EXTS = {".sql"}
_SCHEMA_NAME_PATTERNS = (
    "schema",
    "migration",
    "migrate",
    "alembic",
    "versions",
)

# Directories to skip
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}


def _is_schema_file(path: Path) -> bool:
    """Return True if a file is a schema / migration file.

    Python files are never schema/migration files, even if the filename
    happens to contain a pattern word like 'migration'.
    """
    suffix = path.suffix.lower()
    if suffix == ".py":
        return False
    if suffix in _SCHEMA_EXTS:
        return True
    name_lower = path.name.lower()
    return any(p in name_lower for p in _SCHEMA_NAME_PATTERNS)


def _scan_schema_file(path: Path, field: str) -> list[dict[str, Any]]:
    """
    Read the file line by line and return a list of
    {"line": int, "text": str} dicts for every line that contains `field`.
    Returns empty list on read errors.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    return [
        {"line": i + 1, "text": line.strip()}
        for i, line in enumerate(lines)
        if field in line
    ]


def run(data: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the db-schema-discovery agent.

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

    # Walk every component directory
    component_dirs = sorted(
        p for p in fixtures_root.iterdir() if p.is_dir()
    )

    for component_dir in component_dirs:
        component = component_dir.name

        # Find all schema/migration files in this component
        schema_files = [
            p for p in sorted(component_dir.rglob("*"))
            if p.is_file()
            and _is_schema_file(p)
            and not any(skip in p.parts for skip in _SKIP_DIRS)
        ]

        if not schema_files:
            continue

        all_refs: list[dict[str, Any]] = []

        for schema_file in schema_files:
            rel = schema_file.relative_to(fixtures_root).as_posix()
            hits = _scan_schema_file(schema_file, old_field)
            for hit in hits:
                all_refs.append(
                    {
                        "file": rel,
                        "line": hit["line"],
                        "text": hit["text"],
                    }
                )

        if not all_refs:
            # Schema files exist but no field references found
            continue

        first = all_refs[0]
        source_ref = f"{first['file']}:{first['line']}"

        evidence.append(
            Evidence(
                claim_type="dependency",
                subject=component,
                content={
                    "component": component,
                    "provider": _PROVIDER,
                    "field": old_field,
                    "schema_files": [
                        p.relative_to(fixtures_root).as_posix()
                        for p in schema_files
                    ],
                    "refs": all_refs,
                    "detection": (
                        f"Schema file contains {len(all_refs)} reference(s) "
                        f"to '{old_field}'"
                    ),
                },
                source_ref=source_ref,
                confidence="confirmed",
            )
        )

        dependencies.append(
            Dependency(
                from_component=_PROVIDER,
                to_component=component,
                edge_type="db",
                documentation_status="documented",
                reason=(
                    f"Schema file references '{old_field}' at {source_ref}"
                ),
            )
        )

    result = DiscoveryResult(
        change_id=change_id,
        evidence=evidence,
        dependencies=dependencies,
    )
    return result.model_dump()
