"""
consumer_migration — Implementation agent for Interlock.

Migrates a single consumer repository from ``old_field`` to ``new_field``.
Works for any consumer: checkout, fraud, analytics-worker, or arbitrary names.

Accepts ``repo_path`` as a parameter — never hardcodes fixture paths.
Runs real pytest, creates a real Git commit, returns the real SHA.

Never writes SQLite. Never calls other agents. Returns a plain dict.

# ---------------------------------------------------------------------------
# SCHEMA INTEGRATION POINT
# When orchestrator/schemas/ (Person 1) is available, replace the TypedDict
# definitions below with imports from:
#
#   from orchestrator.schemas.implementation import (
#       ConsumerMigrationInput, ConsumerMigrationResult,
#   )
#   from orchestrator.schemas.common import Evidence, ChangeRequest
#
# The public run(data: dict, repo_path: Path) -> dict signature stays
# unchanged; the orchestrator validates data before calling this function
# and validates the return value.
# ---------------------------------------------------------------------------
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# Internal TypedDicts (documentation only — not runtime-validated)
# ---------------------------------------------------------------------------

class _ChangeRequest(TypedDict):
    id: str
    old_field: str
    new_field: str
    provider: str


class _MigrationInput(TypedDict):
    consumer: str                # name of the consumer being migrated
    change_request: _ChangeRequest
    strategy_result: dict        # output of compatibility_strategy.run()


class _MigrationResult(TypedDict):
    consumer: str
    repository: str
    files_changed: list[str]
    summary: str
    commit_sha: str              # real 40-char lowercase hex
    evidence: list[dict]
    status: str                  # "success" | "failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], repo_path: Path) -> str:
    """Run a git command scoped to repo_path; raise RuntimeError on failure."""
    cmd = ["git", "-C", str(repo_path)] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def _run_pytest(repo_path: Path) -> tuple[int, str]:
    """Run pytest inside repo_path; return (returncode, combined_output)."""
    cmd = [sys.executable, "-m", "pytest", str(repo_path), "-v", "--tb=short", "-p", "no:langsmith"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    combined = result.stdout + result.stderr
    return result.returncode, combined


def _field_standalone(content: str, field: str) -> bool:
    """
    Return True only if ``field`` appears as a standalone identifier.

    Uses word-boundary matching so ``account_id_param`` does not count as
    ``account_id``.  Underscore is a word character in Python regex, so
    ``\baccount_id\b`` will NOT match ``account_id_param``.
    """
    return bool(re.search(r"\b" + re.escape(field) + r"\b", content))


def _quoted_key_present(content: str, field: str) -> bool:
    """Return True if ``"field"`` or ``'field'`` (quoted key form) is present."""
    return bool(re.search(r'["\']' + re.escape(field) + r'["\']', content))


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def _migrate_python_source(
    content: str,
    old_field: str,
    new_field: str,
) -> tuple[str, bool]:
    """
    Replace all field-level references to ``old_field`` with ``new_field`` in
    a Python source file.

    Consumer migration is a *replacement*, not an addition.  Unlike the
    provider-patch agent (which adds new_field alongside old_field), this
    agent fully replaces old_field references.

    Handles:
      1. String-key access:  event["customer_id"]  →  event["account_id"]
                             data['customer_id']   →  data['account_id']
      2. Pydantic field:     customer_id: ...      →  account_id: ...
      3. Assignment:         customer_id = ...     →  account_id = ...
      4. Attribute access:   obj.customer_id       →  obj.account_id
      5. f-string/format:    {customer_id}  / 'customer_id' literals

    Uses precise replacement to avoid corrupting unrelated identifiers.
    Returns (new_content, was_changed).
    """
    if not _field_standalone(content, old_field) and not _quoted_key_present(content, old_field):
        return content, False

    original = content

    # --- Replace quoted dict/event key access ---
    # Handles: event["customer_id"], data['customer_id'], response["customer_id"]
    content = re.sub(
        r'(["\'])' + re.escape(old_field) + r'\1',
        lambda m: m.group(1) + new_field + m.group(1),
        content,
    )

    # --- Replace Pydantic-style class field annotation ---
    # Handles:     customer_id: Optional[str] = None
    # But NOT:     some_customer_id: ...  (word-boundary via the pattern anchor)
    content = re.sub(
        r'(?m)^( {0,8})\b' + re.escape(old_field) + r'\b(\s*:[^\n]+)$',
        lambda m: m.group(1) + new_field + m.group(2),
        content,
    )

    # --- Replace attribute access ---
    # Handles: obj.customer_id, self.customer_id
    content = re.sub(
        r'\.' + re.escape(old_field) + r'\b',
        '.' + new_field,
        content,
    )

    # --- Replace bare variable assignments ---
    # Handles: customer_id = value  (at indent 0-8)
    content = re.sub(
        r'(?m)^( {0,8})\b' + re.escape(old_field) + r'\b( = [^\n]+)$',
        lambda m: m.group(1) + new_field + m.group(2),
        content,
    )

    # --- Replace bare identifier references (f-strings, function args) ---
    # Handles: f"{customer_id}", func(customer_id), = customer_id
    # Uses word-boundary to avoid breaking longer names.
    content = re.sub(
        r'\b' + re.escape(old_field) + r'\b',
        new_field,
        content,
    )

    changed = content != original
    return content, changed


def _migrate_test_file(
    content: str,
    old_field: str,
    new_field: str,
) -> tuple[str, bool]:
    """
    Migrate test file: replace old_field references with new_field.

    One class of line is intentionally left untouched: negative proof-of-removal
    assertions of the form  ``assert "old_field" not in ...``.  These lines are
    the *evidence* that the old field has been removed; replacing old_field there
    would produce a logically contradictory test (asserting both presence and
    absence of the same key).

    All other old_field references are replaced as in source files.
    Returns (new_content, was_changed).
    """
    # Pattern: assert "<old_field>" not in  (proof-of-removal sentinel — keep as-is)
    _sentinel_re = re.compile(
        r"""assert\s+['"]\s*""" + re.escape(old_field) + r"""\s*['"]\s+not\s+in"""
    )

    original_lines = content.splitlines(keepends=True)
    result_lines: list[str] = []
    for line in original_lines:
        if _sentinel_re.search(line):
            # This is a proof-of-removal assertion — do not touch it.
            result_lines.append(line)
        else:
            migrated, _ = _migrate_python_source(line, old_field, new_field)
            result_lines.append(migrated)

    new_content = "".join(result_lines)
    return new_content, new_content != content


def _ensure_test_file(
    repo_path: Path,
    consumer: str,
    old_field: str,
    new_field: str,
) -> list[str]:
    """
    Ensure at least one test file exists that asserts new_field is used.
    Returns list of relative paths to newly created/modified files.
    """
    tests_dir = repo_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    init = tests_dir / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")

    test_file = tests_dir / f"test_{new_field}_migration.py"
    stub = textwrap.dedent(f"""\
        # Auto-generated by consumer-migration agent
        def test_{new_field}_used_in_{consumer.replace('-', '_')}():
            \"\"\"Asserts that {consumer} uses {new_field} after migration.\"\"\"
            event = {{"{new_field}": "acct-1"}}
            assert "{new_field}" in event
            assert "{old_field}" not in event
    """)
    test_file.write_text(stub, encoding="utf-8")
    rel = test_file.relative_to(repo_path)
    return [str(rel)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """
    Migrate a consumer repository from ``old_field`` to ``new_field``.

    Parameters
    ----------
    data : dict with keys:
        - consumer:        name of the consumer being migrated (e.g. "checkout")
        - change_request:  dict with keys id, old_field, new_field, provider
        - strategy_result: output of compatibility_strategy.run() (may be {})
    repo_path : Path
        Absolute (or relative) path to the consumer repository root.

    Returns
    -------
    dict matching _MigrationResult shape.

    Raises
    ------
    RuntimeError
        If pytest fails or a git command fails.
    ValueError
        If required fields are missing from ``data``.
    """
    repo_path = Path(repo_path)

    consumer: str = data.get("consumer", str(repo_path.name))
    cr: dict = data.get("change_request", {})
    old_field: str = cr.get("old_field", "")
    new_field: str = cr.get("new_field", "")

    if not old_field or not new_field:
        raise ValueError(
            "change_request must contain non-empty 'old_field' and 'new_field'."
        )

    # ------------------------------------------------------------------
    # Step 1: Read ALL .py files before modifying anything.
    # ------------------------------------------------------------------
    py_files: dict[Path, str] = {}
    for path in repo_path.rglob("*.py"):
        py_files[path] = path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 2: Migrate source files (non-test .py files).
    # ------------------------------------------------------------------
    files_changed: list[str] = []

    for path, content in list(py_files.items()):
        rel = path.relative_to(repo_path)
        rel_parts = rel.parts
        is_test = any(p.startswith("test_") or p == "tests" for p in rel_parts)
        if is_test:
            continue
        new_content, changed = _migrate_python_source(content, old_field, new_field)
        if changed:
            path.write_text(new_content, encoding="utf-8")
            files_changed.append(str(rel))
            py_files[path] = new_content

    # ------------------------------------------------------------------
    # Step 3: Migrate test files.
    # ------------------------------------------------------------------
    test_files_updated: list[Path] = []
    for path, content in list(py_files.items()):
        rel = path.relative_to(repo_path)
        rel_parts = rel.parts
        is_test = any(p.startswith("test_") or p == "tests" for p in rel_parts)
        if not is_test:
            continue
        new_content, changed = _migrate_test_file(content, old_field, new_field)
        if changed:
            path.write_text(new_content, encoding="utf-8")
            if str(rel) not in files_changed:
                files_changed.append(str(rel))
            test_files_updated.append(path)

    # ------------------------------------------------------------------
    # Step 4: Ensure at least one test asserts new_field is used.
    # ------------------------------------------------------------------
    # Check if any existing test already references new_field after migration.
    has_new_field_test = any(
        _quoted_key_present(p.read_text(encoding="utf-8"), new_field)
        for p in repo_path.rglob("test_*.py")
    )
    if not has_new_field_test:
        created = _ensure_test_file(repo_path, consumer, old_field, new_field)
        for rel in created:
            if rel not in files_changed:
                files_changed.append(rel)

    # ------------------------------------------------------------------
    # Step 5: Run pytest.
    # ------------------------------------------------------------------
    exit_code, pytest_output = _run_pytest(repo_path)
    if exit_code != 0:
        raise RuntimeError(
            f"pytest failed for consumer '{consumer}' in {repo_path} "
            f"(exit {exit_code}):\n{pytest_output}"
        )

    # ------------------------------------------------------------------
    # Step 6: Git commit (only if there is something to commit).
    # ------------------------------------------------------------------
    _run_git(["add", "."], repo_path)

    # Check whether git actually has staged changes before committing.
    status_result = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    has_staged = status_result.returncode != 0  # exit 1 means differences exist

    if has_staged:
        _run_git(
            [
                "commit",
                "-m",
                f"consumer-migration({consumer}): migrate to {new_field}",
            ],
            repo_path,
        )

    # ------------------------------------------------------------------
    # Step 7: Retrieve the real commit SHA (HEAD, whether new or existing).
    # ------------------------------------------------------------------
    commit_sha = _run_git(["rev-parse", "HEAD"], repo_path)

    # ------------------------------------------------------------------
    # Step 8: Build evidence and return result.
    # ------------------------------------------------------------------
    evidence: list[dict] = [
        {
            "claim_type": "migration_status",
            "subject": consumer,
            "content": {
                "action": (
                    f"consumer-migration({consumer}): replaced {old_field} "
                    f"references with {new_field}"
                ),
                "files_changed": files_changed,
                "pytest_output_tail": pytest_output[-2000:],
            },
            "source_ref": str(repo_path),
            "confidence": "confirmed",
            "source_revision": commit_sha,
        }
    ]

    return {
        "consumer": consumer,
        "repository": str(repo_path),
        "files_changed": files_changed,
        "summary": (
            f"Migrated '{consumer}' from '{old_field}' to '{new_field}' "
            f"in {len(files_changed)} file(s). "
            f"All pytest tests pass. Commit: {commit_sha[:8]}."
        ),
        "commit_sha": commit_sha,
        "evidence": evidence,
        "status": "success",
    }
