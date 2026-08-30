"""
agents/discovery/repo_map.py
==============================
repo-map discovery agent.

Walks every repository under fixtures_root and produces a structured
inventory of components, source files, OpenAPI specs, schema/migration
files, and field references.

Returns a dict that validates as DiscoveryResult.
Does NOT write to the database directly.
Does NOT call other agents.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from orchestrator.schemas import Dependency, DiscoveryResult, Evidence

# File extension categories.
#
# Source is anything a consumer might be written in, not just Python — the
# language-aware reference finding for the non-Python extensions lives in
# polyglot_source.py; here they only need to be inventoried and text-scanned
# so a TypeScript or Java component does not show up as an empty directory.
_SOURCE_EXTS = {
    ".py",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".java", ".kt", ".kts", ".scala", ".cs", ".go", ".rb", ".php",
}

# Directories that hold other people's code or build output. On a real
# repository, walking node_modules/ turns "scan this service" into "scan the
# npm registry" — thousands of files, false field references, and dependency
# edges pointing at vendored libraries.
_SKIP_DIRS = {
    "node_modules", ".git", ".hg", ".svn",
    ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", "out", "target", "vendor", "coverage",
    ".next", ".nuxt", ".gradle", ".idea", ".vs", "bin", "obj",
}
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
    Return line numbers where `field` is referenced in the AST.

    Three forms, because the symbol being migrated is not always a dict key:

      x["field"]            subscript   -- a renamed data field
      field(...)  / field   bare name   -- a renamed function or transport symbol
      obj.field(...)        attribute   -- the same, reached through a module

    The subscript form alone was enough while the only change kind was a field
    rename. A webhook-to-pub/sub migration moves a *call*, so restricting
    detection to subscripts made those consumers invisible to discovery and the
    dependency graph came back empty.

    Returns an empty list on parse errors.
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
        # Bare identifier: field(...) or a reference to it
        elif isinstance(node, ast.Name) and node.id == field:
            lines.append(node.lineno)
        # Attribute access: transport.field(...)
        elif isinstance(node, ast.Attribute) and node.attr == field:
            lines.append(node.lineno)

    return sorted(set(lines))


def component_dirs(root: Path, exclude: str | None = None) -> list[Path]:
    """
    The immediate subdirectories of *root* that are components.

    Interlock's structural assumption lives here, in one place, so every
    scanner agrees about what a component is. Build output, dependency caches
    and VCS metadata are not components: enumerating them made a real
    repository slow to scan and, worse, turned each one into a candidate
    consumer that the gate would then require to be migrated - with no way to
    exclude it, because there is no ignore file.

    Dotted directories are excluded for the same reason. `.github` holding a
    workflow that mentions the symbol is not a service that breaks.
    """
    return sorted(
        d for d in root.iterdir()
        if d.is_dir()
        and d.name not in _SKIP_DIRS
        and not d.name.startswith(".")
        and d.name != exclude
    )


def _find_field_refs_in_text(text: str, field: str) -> list[int]:
    """
    Return 1-based line numbers where `field` appears as a standalone symbol.

    Word-bounded, not a substring test: a plain `in` check counted
    `customer_id_extra` as a reference to `customer_id`, which manufactured
    dependency edges — and therefore gate requirements — out of unrelated
    identifiers.
    """
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(field) + r"(?![A-Za-z0-9_])"
    )
    return [
        i + 1
        for i, line in enumerate(text.splitlines())
        if pattern.search(line)
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
        if _SKIP_DIRS.intersection(path.relative_to(component_dir).parts[:-1]):
            continue

        rel = path.relative_to(fixtures_root).as_posix()

        # Categorise
        if path.name in _OPENAPI_NAMES:
            openapi_files.append(rel)
        elif _is_schema_file(path):
            schema_files.append(rel)

        if path.suffix in _SOURCE_EXTS:
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
    # The provider comes from the change spec.  The literal default keeps
    # legacy callers working; it is not a claim about which components exist.
    provider: str = data.get("provider", "account-service")
    # Also match the replacement symbol.
    #
    # Searching only for the old symbol assumes discovery runs BEFORE the
    # migration. In external mode the work has already been done by a human or
    # another agent, so a migrated consumer no longer mentions the old symbol at
    # all — and searching for it alone found nothing, leaving the dependency
    # graph empty and the change unable to leave DISCOVERY.
    #
    # A component that references either symbol is a consumer of this contract,
    # whichever side of the migration it currently sits on.
    new_field: str = data.get("new_field") or ""

    # Resolve fixtures_root
    if "fixtures_root" in data:
        fixtures_root = Path(data["fixtures_root"]).resolve()
    else:
        # Default: fixtures/ sibling of the project root
        fixtures_root = (Path(__file__).parent.parent.parent / "fixtures").resolve()

    evidence: list[Evidence] = []
    dependencies: list[Dependency] = []

    # Walk each immediate subdirectory as a component
    components = component_dirs(fixtures_root)

    for component_dir in components:
        summary = _scan_component(component_dir, fixtures_root, old_field)
        if new_field and new_field != old_field:
            extra = _scan_component(component_dir, fixtures_root, new_field)
            seen = {(r["file"], r["line"]) for r in summary["field_refs"]}
            summary["field_refs"] = summary["field_refs"] + [
                r for r in extra["field_refs"]
                if (r["file"], r["line"]) not in seen
            ]
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

        # Emit a dependency edge for each component that references the target
        # field -- except the provider itself.  The provider owning the field is
        # not a dependency on itself, and emitting one puts a self-loop in the
        # graph, which makes the planning agent's cycle check fail and puts the
        # provider in its own required-consumer set.
        if summary["field_refs"] and name != provider:
            dependencies.append(
                Dependency(
                    from_component=provider,
                    to_component=name,
                    edge_type="undocumented",
                    reason=(
                        f"Source inspection found {len(summary['field_refs'])} "
                        f"reference(s) to '{old_field}'"
                        + (f" or '{new_field}'" if new_field and new_field != old_field else "")
                        + f" in {name}"
                    ),
                )
            )

    result = DiscoveryResult(
        change_id=change_id,
        evidence=evidence,
        dependencies=dependencies,
    )
    return result.model_dump()
