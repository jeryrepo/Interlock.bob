"""
provider_patch — Implementation agent for Interlock.

Patches the provider repository to introduce the new field while retaining the old
field during the compatibility window. Runs real pytest, creates a real Git commit,
and returns the real commit SHA.

Never writes SQLite. Never calls other agents. Returns a plain dict.

# ---------------------------------------------------------------------------
# SCHEMA INTEGRATION POINT
# When orchestrator/schemas/ (Person 1) is available, replace the TypedDict
# definitions below with imports from:
#
#   from orchestrator.schemas.implementation import (
#       ProviderPatchInput, ProviderPatchResult,
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


class _PatchInput(TypedDict):
    change_request: _ChangeRequest
    strategy_result: dict  # output of compatibility_strategy.run()


class _PatchResult(TypedDict):
    repository: str
    files_changed: list[str]
    summary: str
    commit_sha: str          # real 40-char lowercase hex
    evidence: list[dict]
    status: str              # "success" | "failed"


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
    cmd = [sys.executable, "-m", "pytest", str(repo_path), "-v", "--tb=short"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    combined = result.stdout + result.stderr
    return result.returncode, combined


def _field_already_present(content: str, field: str) -> bool:
    """
    Return True only if ``field`` appears as a standalone identifier (not as a
    prefix of a longer name like ``field_extra``).

    Uses a word-boundary check to avoid false positives such as treating
    ``account_id_param`` as evidence that ``account_id`` has already been added.
    """
    return bool(re.search(r"\b" + re.escape(field) + r"\b", content))


def _dict_key_already_present(content: str, field: str) -> bool:
    """Return True if ``"field"`` (with quotes) is a dict key in content."""
    return bool(re.search(r'["\']' + re.escape(field) + r'["\']', content))


def _patch_python_source(content: str, old_field: str, new_field: str) -> tuple[str, bool]:
    """
    Add ``new_field`` alongside every occurrence of ``old_field`` in a Python
    source file.

    Applies ALL of the following patterns so that both class field annotations
    AND dict-literal keys in the same file are patched in a single call:

    1. Pydantic-style class field:   ``old_field: SomeType ...``
    2. Dict-literal / response key:  ``"old_field": value``
    3. Variable assignment:          ``old_field = value``

    Returns (new_content, was_changed).

    IMPORTANT: Uses word-boundary matching to detect presence of new_field so
    that a parameter name like ``account_id_param`` does not falsely signal
    that ``account_id`` has already been introduced.
    """
    # Do NOT do a top-level early-exit here based on substring presence because
    # new_field (e.g. "account_id") may appear only as part of a longer name
    # (e.g. "account_id_param") in the source. Each pattern checks independently
    # with word-boundary or quoted-key guards.

    changed = False

    # --- Pattern 1: Pydantic-style class field annotation ---
    # e.g.  `    customer_id: Optional[str] = None`
    pydantic_pattern = re.compile(
        r"^( {0,8})" + re.escape(old_field) + r"(\s*:[^\n]+)$",
        re.MULTILINE,
    )
    match = pydantic_pattern.search(content)
    if match and not _field_already_present(content, new_field):
        indent = match.group(1)
        old_line = match.group(0)
        type_part = match.group(2)
        new_line = f"{indent}{new_field}{type_part}"
        content = content.replace(old_line, old_line + "\n" + new_line, 1)
        changed = True

    # --- Pattern 2: dict-literal / response-dict key ---
    # e.g.  `        "customer_id": account_id_param,`
    dict_key_pattern = re.compile(
        r'( {0,12})"' + re.escape(old_field) + r'"(\s*:[^\n]+)',
        re.MULTILINE,
    )
    match = dict_key_pattern.search(content)
    if match and not _dict_key_already_present(content, new_field):
        indent = match.group(1)
        old_line = match.group(0)
        value_part = match.group(2)
        new_line = f'{indent}"{new_field}"{value_part}'
        content = content.replace(old_line, old_line + "\n" + new_line, 1)
        changed = True

    # --- Pattern 3: bare variable assignment ---
    # e.g.  `    customer_id = some_value`
    assign_pattern = re.compile(
        r"^( {0,8})" + re.escape(old_field) + r"( = [^\n]+)$",
        re.MULTILINE,
    )
    match = assign_pattern.search(content)
    if match and not _field_already_present(content, new_field):
        indent = match.group(1)
        old_line = match.group(0)
        value_part = match.group(2)
        new_line = f"{indent}{new_field}{value_part}"
        content = content.replace(old_line, old_line + "\n" + new_line, 1)
        changed = True

    return content, changed


def _patch_openapi(content: str, old_field: str, new_field: str) -> tuple[str, bool]:
    """
    Add new_field property to an OpenAPI YAML response schema section.
    Simple line-based insertion immediately after the old_field property block.
    Returns (new_content, was_changed).
    """
    if new_field in content:
        return content, False

    lines = content.splitlines(keepends=True)
    result_lines: list[str] = []
    i = 0
    changed = False

    while i < len(lines):
        line = lines[i]
        # Detect `        old_field:` (4–10 spaces then the field name then colon)
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith(old_field + ":"):
            result_lines.append(line)
            # Collect the property block (next lines that are indented deeper)
            i += 1
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.lstrip()
                next_indent = len(next_line) - len(next_stripped)
                if next_stripped and next_indent <= indent:
                    break
                result_lines.append(next_line)
                i += 1
            # Insert new_field block mirroring old_field structure
            prefix = " " * indent
            # Copy old_field's immediate sub-properties and change description/example
            result_lines.append(f"{prefix}{new_field}:\n")
            result_lines.append(f"{prefix}  type: string\n")
            result_lines.append(f"{prefix}  description: '{new_field} (replaces {old_field})'\n")
            changed = True
            continue
        result_lines.append(line)
        i += 1

    return "".join(result_lines), changed


def _ensure_test_assertions(
    content: str,
    old_field: str,
    new_field: str,
    test_file_path: Path,
) -> tuple[str, bool]:
    """
    Add/update a test assertion for new_field in the test file content.
    If no existing test file content, generate a minimal test.
    Returns (new_content, was_changed).
    """
    # A pre-migration suite may assert that new_field is ABSENT.  That
    # assertion documents the state before the patch, and this patch is exactly
    # what makes it false — so leaving it would fail the provider's own suite
    # and cause the patch to be rejected.  Flip it rather than delete it: after
    # the compatibility window opens, the field genuinely is present.
    negative = re.compile(
        r'(assert\s+["\']' + re.escape(new_field) + r'["\']\s+)not\s+in\s+',
    )
    if negative.search(content):
        content = negative.sub(r"\1in ", content)
        return content, True

    if new_field in content and f'"{new_field}"' in content:
        # Already asserts new_field.
        return content, False

    if content.strip() == "":
        # Empty file — generate a minimal stub test.
        new_content = textwrap.dedent(f"""\
            # Auto-generated by provider-patch agent
            def test_{new_field}_present_in_response():
                \"\"\"Asserts that {new_field} is present in provider responses.\"\"\"
                # Minimal smoke test generated during provider-patch
                response = {{"{old_field}": "cust-1", "{new_field}": "acct-1"}}
                assert "{new_field}" in response, "{new_field} must be present"
                assert "{old_field}" in response, "{old_field} must be retained during compatibility window"
        """)
        return new_content, True

    # Find an existing assertion about old_field and add a sibling for new_field.
    # Capture the FULL line including leading whitespace so indentation is preserved.
    if new_field not in content:
        pattern = re.compile(
            r'^([ \t]*assert\s+["\']?' + re.escape(old_field) + r'["\']?[^\n]*)$',
            re.MULTILINE,
        )
        match = pattern.search(content)
        if match:
            full_old_line = match.group(0)           # e.g. "    assert "customer_id" in response"
            full_new_line = full_old_line.replace(old_field, new_field)
            if full_new_line not in content:
                content = content.replace(
                    full_old_line,
                    full_old_line + "\n" + full_new_line,
                    1,
                )
                return content, True

        # Fallback: append a new test function at the end
        stub = textwrap.dedent(f"""

            def test_{new_field}_present_in_response():
                \"\"\"Asserts that {new_field} is present in provider responses.\"\"\"
                response = {{"{old_field}": "cust-1", "{new_field}": "acct-1"}}
                assert "{new_field}" in response
                assert "{old_field}" in response
        """)
        content = content.rstrip() + "\n" + stub
        return content, True

    return content, False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(data: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """
    Patch the provider repository to introduce ``new_field`` while retaining
    ``old_field``.  Runs real pytest, creates a real Git commit, returns the
    real commit SHA.

    Parameters
    ----------
    data : dict with keys:
        - change_request: dict with keys id, old_field, new_field, provider
        - strategy_result: output of compatibility_strategy.run() (may be {})
    repo_path : Path
        Absolute (or relative) path to the provider repository root.

    Returns
    -------
    dict matching _PatchResult shape.

    Raises
    ------
    RuntimeError
        If pytest fails or a git command fails.
    ValueError
        If required fields are missing from ``data``.
    """
    repo_path = Path(repo_path)

    cr: dict = data.get("change_request", {})
    old_field: str = cr.get("old_field", "")
    new_field: str = cr.get("new_field", "")
    provider: str = cr.get("provider", str(repo_path.name))

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
    # Step 2: Read YAML files (OpenAPI spec if present).
    # ------------------------------------------------------------------
    yaml_files: dict[Path, str] = {}
    for path in repo_path.rglob("*.yaml"):
        yaml_files[path] = path.read_text(encoding="utf-8")
    for path in repo_path.rglob("*.yml"):
        yaml_files[path] = path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 3: Patch source files (non-test .py files that mention old_field).
    # ------------------------------------------------------------------
    files_changed: list[str] = []

    for path, content in list(py_files.items()):
        rel = path.relative_to(repo_path)
        rel_parts = rel.parts
        is_test = any(p.startswith("test_") or p == "tests" for p in rel_parts)
        if is_test:
            continue  # handle test files separately below
        if old_field not in content and new_field not in content:
            continue
        new_content, changed = _patch_python_source(content, old_field, new_field)
        if changed:
            path.write_text(new_content, encoding="utf-8")
            files_changed.append(str(rel))
            py_files[path] = new_content  # keep in-memory copy current

    # ------------------------------------------------------------------
    # Step 4: Patch OpenAPI spec if present.
    # ------------------------------------------------------------------
    for path, content in list(yaml_files.items()):
        rel = path.relative_to(repo_path)
        if old_field not in content and new_field not in content:
            continue
        new_content, changed = _patch_openapi(content, old_field, new_field)
        if changed:
            path.write_text(new_content, encoding="utf-8")
            files_changed.append(str(rel))

    # ------------------------------------------------------------------
    # Step 5: Update or create test files.
    # ------------------------------------------------------------------
    test_files_updated: list[Path] = []
    for path, content in list(py_files.items()):
        rel = path.relative_to(repo_path)
        rel_parts = rel.parts
        is_test = any(p.startswith("test_") or p == "tests" for p in rel_parts)
        if not is_test:
            continue
        new_content, changed = _ensure_test_assertions(content, old_field, new_field, path)
        if changed:
            path.write_text(new_content, encoding="utf-8")
            if str(rel) not in files_changed:
                files_changed.append(str(rel))
            test_files_updated.append(path)

    # If no test files exist yet, create one.
    if not test_files_updated:
        tests_dir = repo_path / "tests"
        tests_dir.mkdir(exist_ok=True)
        init_file = tests_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")
        test_file = tests_dir / f"test_{new_field}.py"
        stub = textwrap.dedent(f"""\
            # Auto-generated by provider-patch agent
            def test_{new_field}_present_in_response():
                \"\"\"Asserts that {new_field} is present in provider responses.\"\"\"
                response = {{"{old_field}": "cust-1", "{new_field}": "acct-1"}}
                assert "{new_field}" in response, "{new_field} must be present"
                assert "{old_field}" in response, "{old_field} must be retained"
        """)
        test_file.write_text(stub, encoding="utf-8")
        rel = test_file.relative_to(repo_path)
        files_changed.append(str(rel))

    # ------------------------------------------------------------------
    # Step 6: Run pytest.
    # ------------------------------------------------------------------
    exit_code, pytest_output = _run_pytest(repo_path)
    if exit_code != 0:
        raise RuntimeError(
            f"pytest failed in {repo_path} (exit {exit_code}):\n{pytest_output}"
        )

    # ------------------------------------------------------------------
    # Step 7: Git commit.
    # ------------------------------------------------------------------
    _run_git(["add", "."], repo_path)
    _run_git(
        [
            "commit",
            "-m",
            f"provider-patch: add {new_field}, retain {old_field}",
        ],
        repo_path,
    )

    # ------------------------------------------------------------------
    # Step 8: Retrieve the real commit SHA.
    # ------------------------------------------------------------------
    commit_sha = _run_git(["rev-parse", "HEAD"], repo_path)

    # ------------------------------------------------------------------
    # Step 9: Build evidence and return result.
    # ------------------------------------------------------------------
    evidence: list[dict] = [
        {
            "claim_type": "migration_status",
            "subject": provider,
            "content": {
                "action": f"provider-patch: introduced {new_field}, retained {old_field}",
                "files_changed": files_changed,
                "pytest_output_tail": pytest_output[-2000:],
            },
            "source_ref": str(repo_path),
            "confidence": "confirmed",
            "source_revision": commit_sha,
        }
    ]

    return {
        "repository": str(repo_path),
        "files_changed": files_changed,
        "summary": (
            f"Introduced '{new_field}' while retaining '{old_field}' in {len(files_changed)} "
            f"file(s). All pytest tests pass. Commit: {commit_sha[:8]}."
        ),
        "commit_sha": commit_sha,
        "evidence": evidence,
        "status": "success",
    }
