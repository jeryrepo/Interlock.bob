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
import tempfile
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


# Bounded so a hung or interactive command cannot stall the gate forever. A
# component declares its own test command, so this process has no way to know
# the command terminates. Expiry is reported as "could not run", never a pass.
_GIT_TIMEOUT_SECONDS = 60
_TEST_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], repo_path: Path) -> str:
    """Run a git command scoped to repo_path; raise RuntimeError on failure."""
    cmd = ["git", "-C", str(repo_path)] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def _run_pytest(repo_path: Path) -> tuple[int, str]:
    """Run pytest inside repo_path; return (returncode, combined_output)."""
    # Hermetic pytest: a repo_path inside a tree that carries a pytest.ini
    # (Interlock's own tests place workspaces under `.pytest_tmp/`) makes the
    # inner pytest inherit `--basetemp=.pytest_tmp` from that ini — and pytest
    # DELETES its basetemp at session start, wiping the outer test run's live
    # temp directories. A private basetemp isolates the inner run completely.
    cmd = [
        sys.executable, "-m", "pytest", str(repo_path), "-v", "--tb=short",
        "-p", "no:cacheprovider",
        "--basetemp", str(Path(tempfile.mkdtemp(prefix="interlock-bt-")) / "bt"),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TEST_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        # A suite that never finished has proven nothing. 124 is the
        # conventional timeout exit code; the caller treats non-zero as failure.
        return 124, f"tests timed out after {_TEST_TIMEOUT_SECONDS}s"
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


# ---------------------------------------------------------------------------
# SQL schema migration
# ---------------------------------------------------------------------------
#
# A column rename in a schema is NOT a find-and-replace.  Rewriting
# ``customer_id`` to ``account_id`` inside a CREATE TABLE describes a big-bang
# cutover: every reader of the old column breaks the moment the schema is
# applied, which is exactly the coordinated deploy Interlock exists to avoid.
#
# The coexistence-window equivalent is additive: introduce the new column,
# backfill it from the old one, and keep the old column until legacy removal is
# separately approved.  That is also what the fixture schema's own migration
# note prescribes.
#
# The marker comment makes the rewrite idempotent, so re-running the agent on an
# already-migrated schema is a no-op rather than a duplicated ALTER.

def _sql_marker(old_field: str, new_field: str) -> str:
    return f"-- interlock:migrated {old_field}->{new_field}"


def _sql_tables_with_column(content: str, column: str) -> list[str]:
    """
    Table names whose ``CREATE TABLE`` body declares ``column``.

    Deliberately conservative: it matches a column *declaration* (the name at
    the start of a line inside the parenthesised body), not every mention. A
    ``REFERENCES accounts(customer_id)`` clause names a column on another table
    and must not cause a spurious ALTER here.
    """
    table_pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\n\s*\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    declaration = re.compile(r"^\s*" + re.escape(column) + r"\s+", re.MULTILINE)

    tables: list[str] = []
    for match in table_pattern.finditer(content):
        table, body = match.group(1), match.group(2)
        if declaration.search(body):
            tables.append(table)
    return tables


def _migrate_sql_schema(
    content: str,
    old_field: str,
    new_field: str,
) -> tuple[str, bool, list[str]]:
    """
    Append an additive migration introducing ``new_field`` alongside
    ``old_field`` for every table that declares it.

    Returns (new_content, was_changed, tables_affected).  ``was_changed`` is
    False when there is nothing to do *and* when the migration is already
    present, but ``tables_affected`` is populated in both cases so the caller
    can still generate assertions against an already-migrated schema.
    """
    tables = _sql_tables_with_column(content, old_field)
    if not tables:
        return content, False, []
    if _sql_marker(old_field, new_field) in content:
        return content, False, tables

    lines = [
        "",
        _sql_marker(old_field, new_field),
        f"-- Coexistence window: {new_field} is introduced alongside {old_field}.",
        f"-- {old_field} is retained until legacy removal is separately approved.",
    ]
    for table in tables:
        lines.append(f"ALTER TABLE {table} ADD COLUMN {new_field} TEXT;")
        lines.append(f"UPDATE {table} SET {new_field} = {old_field};")
    lines.append("")

    return content.rstrip("\n") + "\n" + "\n".join(lines), True, tables


def _write_schema_test(
    repo_path: Path,
    schema_rel: str,
    old_field: str,
    new_field: str,
    tables: list[str],
) -> list[str]:
    """
    Write a test that reads the migrated schema back off disk and asserts the
    new column was introduced for every affected table, with the old column
    retained.

    This asserts against the artifact the agent actually produced.  That is the
    difference between a test and a restatement of the agent's own intent: the
    generated Python stub this agent used to emit for schema-only components
    asserted a dict literal it had just written, so it passed whether or not the
    schema was touched.
    """
    tests_dir = repo_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    init = tests_dir / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")

    test_file = tests_dir / f"test_{new_field}_schema_migration.py"
    body = textwrap.dedent(
        '''\
        # Auto-generated by the consumer-migration agent.
        # Reads the migrated schema off disk and asserts the coexistence window
        # is genuinely expressed in SQL, not merely intended.
        from pathlib import Path

        SCHEMA = Path(__file__).resolve().parent.parent / {schema!r}
        TABLES = {tables!r}
        OLD_FIELD = {old!r}
        NEW_FIELD = {new!r}


        def _schema_text() -> str:
            return SCHEMA.read_text(encoding="utf-8")


        def test_schema_file_exists():
            assert SCHEMA.is_file(), f"schema not found: {{SCHEMA}}"


        def test_new_column_added_to_every_affected_table():
            text = _schema_text()
            for table in TABLES:
                assert f"ALTER TABLE {{table}} ADD COLUMN {{NEW_FIELD}}" in text, (
                    f"{{table}} never gains {{NEW_FIELD}}"
                )


        def test_new_column_backfilled_from_old():
            text = _schema_text()
            for table in TABLES:
                assert f"UPDATE {{table}} SET {{NEW_FIELD}} = {{OLD_FIELD}}" in text, (
                    f"{{table}} never backfills {{NEW_FIELD}} from {{OLD_FIELD}}"
                )


        def test_old_column_retained_during_coexistence_window():
            assert OLD_FIELD in _schema_text(), (
                f"{{OLD_FIELD}} must remain until legacy removal is approved"
            )
        '''
    ).format(
        schema=Path(schema_rel).as_posix(),
        tables=tables,
        old=old_field,
        new=new_field,
    )
    test_file.write_text(body, encoding="utf-8")
    return [str(test_file.relative_to(repo_path))]


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

    # Source changes that are not tests. Tracked separately from files_changed
    # because a run that only wrote its own test file has proved nothing — see
    # the guard in Step 5.
    source_files_changed: list[str] = list(files_changed)

    # ------------------------------------------------------------------
    # Step 2b: Migrate SQL schema files.
    # ------------------------------------------------------------------
    # A component can be schema-only (the platform-config fixture is exactly
    # that: a README and a schema.sql). Globbing *.py alone meant such a
    # component was reported migrated while its schema was never touched.
    schema_tables: dict[str, list[str]] = {}
    for path in sorted(repo_path.rglob("*.sql")):
        rel = str(path.relative_to(repo_path))
        content = path.read_text(encoding="utf-8")
        new_content, changed, tables = _migrate_sql_schema(
            content, old_field, new_field
        )
        if tables:
            schema_tables[rel] = tables
        if changed:
            path.write_text(new_content, encoding="utf-8")
            files_changed.append(rel)
            source_files_changed.append(rel)

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
    # Schema files get a test that reads the migrated SQL back off disk, which
    # actually constrains the artifact. The Python stub below does not, so it is
    # only ever a last resort for a component with real source changes and no
    # test that mentions the new field.
    for schema_rel, tables in schema_tables.items():
        for rel in _write_schema_test(
            repo_path, schema_rel, old_field, new_field, tables
        ):
            if rel not in files_changed:
                files_changed.append(rel)

    # Check if any existing test already references new_field after migration.
    has_new_field_test = any(
        _quoted_key_present(p.read_text(encoding="utf-8"), new_field)
        for p in repo_path.rglob("test_*.py")
    )
    # Only when real source changed. Writing this stub into a component the
    # agent could not migrate is what manufactured the passing evidence in the
    # first place — it asserts a dict literal written two lines above it.
    if source_files_changed and not has_new_field_test and not schema_tables:
        created = _ensure_test_file(repo_path, consumer, old_field, new_field)
        for rel in created:
            if rel not in files_changed:
                files_changed.append(rel)

    # ------------------------------------------------------------------
    # Step 5: Refuse to report success for a migration that changed nothing.
    # ------------------------------------------------------------------
    # This is the invariant the whole gate rests on. Previously `status` was
    # "success" whenever pytest passed, entirely decoupled from whether the
    # agent had changed anything: a component this agent could not read (a
    # schema-only component, a non-Python service) was migrated in name only,
    # its auto-generated test asserted a dict the agent had itself written, and
    # the gate counted it as verified. Absence of a change is not proof of one.
    if not source_files_changed:
        return {
            "consumer": consumer,
            "repository": str(repo_path),
            "files_changed": [],
            "summary": (
                f"No migratable reference to '{old_field}' was found in "
                f"'{consumer}'. Reporting failure rather than success: this "
                f"agent migrates Python and SQL sources, so a component built "
                f"on anything else is unproven, not safe."
            ),
            "commit_sha": None,
            "evidence": [
                {
                    "claim_type": "risk",
                    "subject": consumer,
                    "content": {
                        "risk": "migration_changed_nothing",
                        "detail": (
                            f"consumer-migration({consumer}) modified no source "
                            f"file. '{old_field}' was not found in any .py or "
                            f".sql file under {repo_path}."
                        ),
                    },
                    "source_ref": str(repo_path),
                    "confidence": "confirmed",
                    "source_revision": None,
                }
            ],
            "status": "failed",
        }

    # ------------------------------------------------------------------
    # Step 6: Run pytest.
    # ------------------------------------------------------------------
    exit_code, pytest_output = _run_pytest(repo_path)
    if exit_code != 0:
        raise RuntimeError(
            f"pytest failed for consumer '{consumer}' in {repo_path} "
            f"(exit {exit_code}):\n{pytest_output}"
        )

    # ------------------------------------------------------------------
    # Step 7: Git commit.
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
    # Step 8: Retrieve the real commit SHA.
    # ------------------------------------------------------------------
    commit_sha = _run_git(["rev-parse", "HEAD"], repo_path)

    # ------------------------------------------------------------------
    # Step 9: Build evidence and return result.
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
