"""
agents/implementation/external_change.py
=========================================
The external-change agent: verifies a migration someone else performed.

Why this exists
---------------
`provider_patch` and `consumer_migration` rewrite Python source with regexes.
That works for a field rename in a Python codebase and nowhere else. You cannot
regex C into Python, and a system that tried would be a bag of shape-matching
special cases, each working on exactly one fixture.

Interlock's value was never the transformation. It is discovery of every
consumer, a deterministic gate, and evidence with real commit SHAs. So for any
transition the built-in rewriters cannot perform, this agent inverts the
relationship: **a human or another coding agent does the work, and Interlock
proves it was done and that nothing was left behind.**

That makes Interlock the safety envelope around IBM Bob rather than a competitor
to it, and it is what makes a C-to-Python migration expressible: Interlock does
not translate the code, it refuses to let the old path be retired until every
consumer is migrated and green.

What counts as proof
--------------------
Two independent checks, both required, both language-agnostic:

1. **The symbols moved.** A consumer must no longer reference the old symbol and
   must reference the new one. The provider is the exception — during the
   coexistence window it must serve *both*, which is the whole point of the
   window.
2. **Its own tests pass**, run via the command its `interlock.toml` declares.

Neither check needs to understand the language. The first is textual; the second
delegates to the component's own toolchain.

What it must NOT do
-------------------
- **Never modify the component.** This agent is read-only by design; that is the
  distinction from `provider_patch`. It reports, it does not rewrite.
- **Never report an unproven migration as done.** No commit, symbols unmoved, or
  failing tests all mean `status="failed"` (AGENTS.md invariant 4).
- Never write to the ledger (invariant 2), call another agent (invariant 3), or
  decide the gate (invariant 1).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.manifest import environment_for, missing_program, resolve_program
from orchestrator.manifest import load as manifest_for

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "dist", "build"}
_SKIP_FILES = {"interlock.toml"}

# Text files worth scanning for the symbols. Deliberately broad: the point is to
# work for languages the built-in rewriters cannot touch.
_SOURCE_SUFFIXES = {
    ".py", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".java", ".kt",
    ".js", ".ts", ".rb", ".php", ".sql", ".yaml", ".yml", ".json", ".toml",
    ".sh", ".mk", "",
}

_OUTPUT_TAIL_CHARS = 2000

# Bounded: the component's declared command is arbitrary.
_TEST_TIMEOUT_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_sources(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.name in _SKIP_FILES:
            continue
        if path.suffix.lower() in _SOURCE_SUFFIXES:
            yield path, rel


def _symbol_hits(root: Path, symbol: str) -> list[str]:
    """Files where *symbol* appears as a standalone token."""
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    hits: list[str] = []
    for path, rel in _iter_sources(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            hits.append(str(rel))
    return hits


def _run_tests(root: Path) -> tuple[int, str, list[str], str]:
    """Run the component's declared test command. Never uses a shell."""
    manifest = manifest_for(root)
    cmd = manifest.command()
    missing = missing_program(cmd)
    if missing:
        # Named explicitly. "tests failed (exit 127)" reads as "your code is
        # broken" when the truth is that the runner is not installed or not on
        # PATH - a distinction the author of the component cannot act on
        # unless it is stated.
        return 127, (
            f"cannot run this component's tests: {missing!r} was not found on "
            f"PATH. Install it, or correct test_command in interlock.toml. "
            f"Command was {cmd!r}."
        ), cmd, manifest.language
    try:
        result = subprocess.run(
            resolve_program(cmd), cwd=str(root), capture_output=True, text=True,
            timeout=_TEST_TIMEOUT_SECONDS, env=environment_for(cmd),
        )
    except subprocess.TimeoutExpired:
        return 124, (
            f"test command {cmd!r} timed out after {_TEST_TIMEOUT_SECONDS}s"
        ), cmd, manifest.language
    except OSError as exc:
        return 127, f"could not execute {cmd!r}: {exc}", cmd, manifest.language
    return result.returncode, result.stdout + result.stderr, cmd, manifest.language


def _head_revision(root: Path) -> str | None:
    """The component's real HEAD, or None. Never invent one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# Agent entry point
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """
    Verify a migration performed outside Interlock.

    Parameters
    ----------
    data : dict with keys:
        - change_id : str (required)
        - old_field : str, the symbol being retired
        - new_field : str, its replacement
        - consumer  : str, optional; defaults to the directory name
        - provider  : str, optional. When it equals the component, BOTH symbols
          are required to be present — that is the coexistence window.
    repo_path : Path
        The component directory. Never modified.

    Returns
    -------
    dict validating against ``orchestrator.schemas.ImplementationResult``, with
    ``status`` in the agent vocabulary (``success`` / ``failed``) that
    ``orchestrator.adapters.implementation`` reads.
    """
    change_id = data["change_id"]
    root = Path(repo_path)
    component = data.get("consumer") or root.name
    old_symbol = data.get("old_field") or ""
    new_symbol = data.get("new_field") or ""
    is_provider = component == data.get("provider")

    if not root.is_dir():
        return _result(change_id, component, False, {
            "detail": f"component directory not found: {root}",
            "outcome": "component_missing",
        })

    old_hits = _symbol_hits(root, old_symbol) if old_symbol else []
    new_hits = _symbol_hits(root, new_symbol) if new_symbol else []

    problems: list[str] = []
    if is_provider:
        # The provider must serve both during the window: dropping the old
        # symbol breaks every un-migrated consumer, and never gaining the new
        # one means there was nothing for consumers to migrate to.
        if old_symbol and not old_hits:
            problems.append(
                f"provider no longer references {old_symbol!r}; un-migrated "
                f"consumers would break during the coexistence window"
            )
        if new_symbol and not new_hits:
            problems.append(f"provider does not yet reference {new_symbol!r}")
    else:
        if old_symbol and old_hits:
            problems.append(
                f"still references {old_symbol!r} in: {', '.join(old_hits[:5])}"
            )
        if new_symbol and not new_hits:
            problems.append(f"does not reference {new_symbol!r}")

    exit_code, output, cmd, language = _run_tests(root)
    if exit_code != 0:
        problems.append(f"tests failed (exit {exit_code})")

    revision = _head_revision(root)
    if revision is None:
        # Without a commit there is nothing to attribute the change to, and the
        # critic's stale-evidence check has nothing to compare against.
        problems.append("no git commit recorded for this component")

    return _result(change_id, component, not problems, {
        "outcome": "migration_verified" if not problems else "migration_not_proven",
        "detail": "; ".join(problems) if problems else (
            f"{component} migrated externally and its own suite passes"
        ),
        "role": "provider" if is_provider else "consumer",
        "language": language,
        "test_command": cmd,
        "exit_code": exit_code,
        "old_symbol_files": old_hits,
        "new_symbol_files": new_hits,
        "output_tail": output[-_OUTPUT_TAIL_CHARS:],
    }, revision)


def _result(
    change_id: str,
    component: str,
    passed: bool,
    content: dict[str, Any],
    revision: str | None = None,
) -> dict[str, Any]:
    """Build the agent-shaped result. Single construction point."""
    return {
        "change_id": change_id,
        "consumer": component,
        "repository": component,
        "files_changed": [],          # this agent verifies; it never edits
        "summary": content["detail"],
        "commit_sha": revision,
        "status": "success" if passed else "failed",
        "evidence": [
            {
                "claim_type": "migration_status",
                "subject": component,
                "content": content,
                "source_ref": component,
                "confidence": "confirmed",
                "source_revision": revision,
            }
        ],
    }
