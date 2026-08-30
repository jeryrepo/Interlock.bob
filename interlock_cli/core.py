"""
interlock_cli/core.py
======================
The verbs, as plain functions returning plain dicts.

Kept separate from `cli.py` so the same logic serves three surfaces without
duplication: the terminal, the MCP server that IBM Bob and other coding agents
call, and the GitHub Action. Each is a thin shell over these functions.

Everything here runs **in-process against a local SQLite ledger** — no server
required. That is what makes `interlock gate` usable in a pre-push hook or a CI
step, where standing up uvicorn would be absurd.

The one rule these functions must never break: the verdict comes from
`gate.evaluate_gate()` and nowhere else. The CLI does not re-derive, cache, or
second-guess it (AGENTS.md invariant 1).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

import orchestrator.gate as gate
import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.agent_runner import run_workflow
from orchestrator.schemas import ChangeSpec

_SPEC_ADAPTER = TypeAdapter(ChangeSpec)

# Exit codes. 0 and 1 carry meaning for CI: a NOT_PROVEN_SAFE gate must fail the
# build, and it must be distinguishable from the tool itself erroring.
EXIT_OK = 0
EXIT_NOT_PROVEN_SAFE = 1
EXIT_ERROR = 2

# The human gates, and the state each one may be approved from. Mirrors
# `_ALLOWED_GATES` and `expected_states` in orchestrator/main.py: the terminal
# and the API must not disagree about what a valid approval is.
_GATE_STATES: dict[str, str] = {
    "coordinate": "COORDINATE",
    "legacy_removal": "APPROVE",
}
ALLOWED_GATES = frozenset(_GATE_STATES)


# Connections opened by CLI commands, so they can be closed deterministically.
#
# A real CLI process exits and the OS reclaims the handle, so this exists for
# in-process callers: pytest's CliRunner retains tracebacks, those frames keep
# `conn` alive, and on Windows an open SQLite handle blocks deletion of the
# temp directory holding it. The failure surfaces confusingly, as a PermissionError
# cleaning up a *previous* test's directory.
_OPEN_LEDGERS: list[sqlite3.Connection] = []


def open_ledger(db_path: str) -> sqlite3.Connection:
    conn = ledger.init_db(db_path)
    _OPEN_LEDGERS.append(conn)
    return conn


def close_ledgers() -> None:
    """Close every ledger opened through open_ledger(). Safe to call twice."""
    while _OPEN_LEDGERS:
        conn = _OPEN_LEDGERS.pop()
        try:
            conn.close()
        except sqlite3.Error:
            pass


class NothingDiscovered(RuntimeError):
    """
    Discovery ran and found no component referencing the symbol.

    Raised in place of the `InvalidTransition` that the state machine throws
    when it is asked to leave DISCOVERY with an empty dependency graph. That
    exception is correct — a change with no known consumers cannot be planned —
    but it reaches the terminal as a traceback, and a traceback is not an
    answer. This carries what the caller needs to say something useful instead.

    Almost always one of: the components root is pointed a level too high or
    too low, the symbol is spelled differently in the code, or the symbol lives
    only inside the provider and genuinely has no cross-component consumers.
    """

    def __init__(self, change_id: str, spec: dict):
        self.change_id = change_id
        self.spec = spec
        self.provider = spec.get("provider", "")
        self.components_root = spec.get("components_root", "")
        self.symbol = spec.get("old_field") or spec.get("old_symbol") or ""
        super().__init__(
            f"discovery found no component referencing {self.symbol!r} under "
            f"{self.components_root!r}"
        )


def list_components(
    components_root: str, detect: bool = False
) -> list[dict[str, Any]]:
    """
    The components Interlock would see under *components_root*.

    Interlock's structural assumption in one function: **every immediate
    subdirectory is a component**, and there is no exclusion mechanism. So
    `docs/` and `.github/` are components too, and a stray mention of the
    symbol in either becomes a dependency edge the gate then requires you to
    migrate. Printing this before anything runs is the cheapest way to notice.
    """
    from agents.discovery.repo_map import component_dirs
    from orchestrator.manifest import MANIFEST_FILENAME
    from orchestrator import toolchain

    root = Path(components_root)
    if not root.is_dir():
        return []

    found: list[dict[str, Any]] = []
    for directory in component_dirs(root):
        declared = (directory / MANIFEST_FILENAME).is_file()
        entry: dict[str, Any] = {"name": directory.name, "has_manifest": declared}
        if detect:
            # Only when asked. Detection is a bounded walk per component, which
            # is cheap but pointless for callers that just need the names -
            # `provider_problem` and `doctor` among them.
            guess = toolchain.detect(directory)
            entry["detected"] = guess.as_dict()
            entry["needs_manifest"] = not declared and toolchain.needs_manifest(guess)
        found.append(entry)
    return found


# File extensions that make a directory look like code rather than assets or
# documentation. Only used for the "which directory is your components root"
# guess, never for analysis - the scanners have their own, stricter lists.
_SOURCE_HINTS = frozenset({
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt", ".kts",
    ".scala", ".cs", ".go", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".rs",
    ".swift", ".ex", ".exs", ".sql",
})


def _holds_source(directory: Path, budget: int = 600) -> bool:
    """
    True if *directory* contains source code, giving up after *budget* entries.

    Bounded on purpose. This runs over directories the user has not vouched
    for, and an unbounded walk into `node_modules` would make the suggestion
    slower than the analysis it is meant to save.
    """
    from agents.discovery.repo_map import _SKIP_DIRS

    stack, seen = [directory], 0
    while stack and seen < budget:
        try:
            entries = list(stack.pop().iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen >= budget:
                break
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix in _SOURCE_HINTS:
                return True
    return False


def suggest_roots(
    start: str, max_depth: int = 2, limit: int = 4, provider: str | None = None
) -> list[dict[str, Any]]:
    """
    Directories under *start* that look like a components root, best first.

    Interlock's one structural requirement is a directory whose immediate
    subdirectories are the components. Every real repository puts that
    somewhere different - `services/`, `packages/`, `apps/`, `src/`, or the
    root itself - and getting it wrong produces either no components or one
    enormous component, neither of which says what to do about it.

    A candidate scores by how many of its immediate subdirectories actually
    contain source. That is a guess, deliberately shown to the user rather than
    acted on: choosing the wrong root silently is the failure this is meant to
    prevent, so it must not be automated away.
    """
    from agents.discovery.repo_map import _SKIP_DIRS

    root = Path(start)
    if not root.is_dir():
        return []

    candidates: list[dict[str, Any]] = []
    frontier = [(root, 0)]
    while frontier:
        directory, depth = frontier.pop(0)
        children = [
            d for d in sorted(directory.iterdir())
            if d.is_dir() and d.name not in _SKIP_DIRS and not d.name.startswith(".")
        ]
        with_source = [d for d in children if _holds_source(d)]
        if len(with_source) >= 2:
            names_here = [d.name for d in with_source]
            candidates.append({
                "path": str(directory),
                "components": len(with_source),
                "names": names_here[:8],
                # A candidate that actually contains the named provider is
                # almost certainly the one meant, whatever its size.
                "has_provider": provider is not None and provider in names_here,
            })
        if depth < max_depth:
            frontier.extend((d, depth + 1) for d in children)

    candidates.sort(
        key=lambda c: (not c["has_provider"], -c["components"], len(c["path"]))
    )
    return candidates[:limit]



def provider_problem(provider: str, components_root: str) -> str | None:
    """
    Why *provider* cannot be used, or None if it is fine.

    The provider name is used two incompatible ways at once: as a path segment
    (`workspace / provider`) and as a directory-name comparison
    (`component == data["provider"]`). A value that is not an immediate
    subdirectory therefore fails in a way that reads as someone else's bug —
    `services/api` resolves as a path, so the agents run, but the component's
    own name is `api`, so the provider is classified as a *consumer* and then
    fails for still referencing the old symbol. Checking up front turns the
    most confusing outcome into the clearest one.
    """
    root = Path(components_root)
    if not root.is_dir():
        return f"components root does not exist: {root}"
    names = [d["name"] for d in list_components(components_root)]
    if provider in names:
        return None
    if not names:
        return (
            f"{root} has no subdirectories, so it contains no components. "
            f"Interlock treats each immediate subdirectory as one component."
        )
    hint = ""
    if "/" in provider or "\\" in provider:
        hint = " It must be a bare directory name, not a path."
    return (
        f"provider {provider!r} is not a component under {root}.{hint} "
        f"Found: {', '.join(names)}"
    )



# ---------------------------------------------------------------------------
# MCP client configuration
# ---------------------------------------------------------------------------

# Tools an MCP client may call without a per-call prompt. Read-only on purpose:
# `interlock_check`, `interlock_start` and `interlock_approve_coordination`
# execute each component's declared test command, so they stay behind a prompt.
MCP_AUTO_ALLOWED: tuple[str, ...] = (
    "interlock_gate",
    "interlock_status",
    "interlock_evidence",
    "interlock_dependency_graph",
    "interlock_list_changes",
)


def init_mcp(
    target: str,
    components_root: str | None = None,
    db_path: str | None = None,
    workspace: str | None = None,
    python: str | None = None,
    scope: str = "project",
    home: str | Path | None = None,
) -> dict[str, Any]:
    """
    Write MCP configuration so agents in another repository call Interlock
    installed HERE.

    The configs this repository ships point at a relative `.venv`, so they work
    only when Bob opens Interlock's own checkout. The product runs the other
    direction: a developer inside their own repository asking "is this change
    safe". That requires an entry with absolute paths — this interpreter, their
    components root — because the MCP client, not Interlock, decides which
    working directory the server starts in.

    Two scopes, matching where IBM Bob actually looks (verified against the
    Bob 2.0 application bundle — its docs name exactly these two files):

    - ``project``: `.bob/mcp.json` and `.mcp.json` inside *target*, for Bob and
      for Claude Code / Cursor / Copilot respectively. Applies to that
      repository only, and overrides global for same-named servers.
    - ``global``: `~/.bob/settings/mcp.json` — NOT `~/.bob/mcp.json`, which Bob
      ignores — so the tools exist in every workspace Bob opens. Ledger and
      workspace default to `~/.interlock/`, and the components root defaults to
      the bundled fixtures so `interlock_check` works out of the box; agents
      pass `components_root` per call for real repositories.

    Non-destructive: an existing file keeps every other server entry, and only
    the `interlock` entry is replaced. A file that does not parse is skipped and
    reported, never overwritten — it may be a hand-maintained configuration with
    a typo, and the other entries in it are not Interlock's to destroy.
    """
    import importlib.util
    import json
    import sys

    report: dict[str, Any] = {
        "written": [],
        "skipped": [],
        "replaced": [],
        "components": [],
        "suggested_roots": [],
        "problems": [],
    }

    if scope == "global":
        base_dir = Path(home).resolve() if home else Path.home()
        # For a machine-wide entry the only universally-valid default root is
        # the demo fixtures; every stdio tool accepts components_root per call.
        bundled = Path(__file__).resolve().parents[1] / "fixtures"
        if components_root:
            comp_root = Path(components_root).resolve()
        elif bundled.is_dir():
            comp_root = bundled
        else:
            comp_root = None
        state_dir = base_dir / ".interlock"
    else:
        target_dir = Path(target)
        if not target_dir.is_dir():
            report["problems"].append(f"target is not a directory: {target}")
            return report
        base_dir = target_dir = target_dir.resolve()
        # A relative components root is relative to the repository being
        # configured, not the shell's working directory — `interlock init
        # ../shop --components-root services` means shop/services. (Joining an
        # absolute path keeps it as-is.)
        comp_root = (target_dir / components_root).resolve() if components_root else target_dir
        state_dir = target_dir / ".interlock"

    if comp_root is not None and not comp_root.is_dir():
        report["problems"].append(f"components root does not exist: {comp_root}")
        return report

    interpreter = python or sys.executable
    db_file = (base_dir / db_path).resolve() if db_path else state_dir / "interlock.db"
    work_dir = (base_dir / workspace).resolve() if workspace else state_dir / "work"

    report.update(
        {
            "target": str(base_dir),
            "scope": scope,
            "python": interpreter,
            "components_root": str(comp_root) if comp_root else None,
            "db_path": str(db_file),
            "workspace": str(work_dir),
            # The server itself needs the `mcp` SDK — the optional [mcp] extra.
            "mcp_sdk_installed": importlib.util.find_spec("mcp") is not None,
        }
    )

    env = {
        "INTERLOCK_DB_PATH": str(db_file),
        "INTERLOCK_WORKSPACE": str(work_dir),
    }
    if comp_root is not None:
        env["INTERLOCK_COMPONENTS_ROOT"] = str(comp_root)
    launch: dict[str, Any] = {
        "command": interpreter,
        "args": ["-m", "interlock_mcp.server"],
        "env": env,
    }
    bob_entry = {
        **launch,
        "alwaysAllow": list(MCP_AUTO_ALLOWED),
        "disabled": False,
    }
    if scope == "global":
        entries: tuple[tuple[Path, dict[str, Any]], ...] = (
            (base_dir / ".bob" / "settings" / "mcp.json", bob_entry),
        )
    else:
        entries = (
            (base_dir / ".bob" / "mcp.json", bob_entry),
            (base_dir / ".mcp.json", {"type": "stdio", **launch}),
        )

    for config_path, entry in entries:
        data: dict[str, Any] = {}
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
            except ValueError:
                data = None  # type: ignore[assignment]
            if not isinstance(data, dict) or not isinstance(
                data.get("mcpServers", {}), dict
            ):
                report["skipped"].append(
                    {
                        "path": str(config_path),
                        "reason": "existing file is not a JSON object with an "
                        "mcpServers object; not overwriting it",
                    }
                )
                continue
        servers = data.setdefault("mcpServers", {})
        if "interlock" in servers:
            report["replaced"].append(str(config_path))
        servers["interlock"] = entry
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        report["written"].append(str(config_path))

    if comp_root is not None:
        report["components"] = [c["name"] for c in list_components(str(comp_root))]
    if not report["components"] and scope == "project":
        report["suggested_roots"] = suggest_roots(str(base_dir))
    # The generated entry points the ledger here; creating the directory now
    # (ledger.init_db also does) means no first tool call meets a missing path.
    db_file.parent.mkdir(parents=True, exist_ok=True)
    return report


def mcp_client_status(
    cwd: str | Path | None = None, home: str | Path | None = None
) -> dict[str, Any]:
    """
    Where IBM Bob would find Interlock from *cwd*, and whether that collides.

    Bob merges two scopes and deduplicates by server name: a workspace
    `.bob/mcp.json` entry called `interlock` OVERRIDES the global one at
    `~/.bob/settings/mcp.json` — Bob runs one server, not two, even though its
    panel lists a row per scope. This function exists so both confusing
    renderings of that — "No MCP servers found" and "why are there two rows" —
    are answerable from the terminal, without opening Bob's settings.

    It also flags `~/.bob/mcp.json`: the obvious place to put a global config,
    and one Bob silently ignores (verified against the Bob 2.0 bundle).
    """
    import json

    home_dir = Path(home).resolve() if home else Path.home()
    cwd_dir = Path(cwd).resolve() if cwd else Path.cwd()

    def describe(path: Path) -> dict[str, Any]:
        info: dict[str, Any] = {
            "path": str(path),
            "present": path.is_file(),
            "interlock": False,
            "problem": None,
        }
        if not info["present"]:
            return info
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            info["problem"] = "not valid JSON"
            return info
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        entry = servers.get("interlock") if isinstance(servers, dict) else None
        if not isinstance(entry, dict):
            return info
        info["interlock"] = True
        command = entry.get("command") or ""
        if command and not Path(command).is_file():
            info["problem"] = f"command does not exist: {command}"
        return info

    global_info = describe(home_dir / ".bob" / "settings" / "mcp.json")
    workspace_info = describe(cwd_dir / ".bob" / "mcp.json")
    misplaced = (home_dir / ".bob" / "mcp.json").is_file()

    g, w = global_info["interlock"], workspace_info["interlock"]
    if g and w:
        summary = (
            "workspace + global: the workspace entry overrides — Bob runs ONE "
            "interlock server; a row per scope in its panel is override, not "
            "duplication"
        )
    elif g:
        summary = "global only: interlock is available in every workspace Bob opens"
    elif w:
        summary = (
            "workspace only: removing this folder from the workspace removes "
            "the tools — `interlock init --global` adds a fallback"
        )
    else:
        summary = (
            "not configured: run `interlock init <repo>` or `interlock init --global`"
        )

    return {
        "global": global_info,
        "workspace": workspace_info,
        "misplaced_global": misplaced,
        "configured": g or w,
        "summary": summary,
    }


def build_spec(
    kind: str,
    provider: str,
    old: str,
    new: str,
    components_root: str,
    topic: str | None = None,
    webhook_path: str | None = None,
    endpoint: str | None = None,
    implementation: str = "builtin",
) -> dict[str, Any]:
    """
    Assemble and validate a ChangeSpec from flat CLI arguments.

    Validation happens here rather than at use time so a typo in `--kind` fails
    immediately with a clear message instead of surfacing as a mysteriously
    empty agent registry three phases later.
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "provider": provider,
        "components_root": components_root,
        "implementation": implementation,
    }
    if kind == "transport_migration":
        payload.update(
            {
                "topic": topic or "events",
                "webhook_path": webhook_path or "/hooks",
                "old_symbol": old,
                "new_symbol": new,
            }
        )
    else:
        payload.update({"old_field": old, "new_field": new})
        if endpoint:
            payload["endpoint"] = endpoint
    return _SPEC_ADAPTER.validate_python(payload).model_dump()


def start(conn: sqlite3.Connection, description: str, spec: dict) -> dict[str, Any]:
    """
    Create a change and run agents up to the first human gate.

    An empty dependency graph is translated into `NothingDiscovered`. The state
    machine refuses to leave DISCOVERY without at least one edge, and nothing
    between there and here caught the resulting `InvalidTransition`, so the
    most likely first run against an unfamiliar repository ended in a
    traceback rather than a diagnosis.
    """
    change_id = str(uuid.uuid4())
    ledger.create_change(conn, change_id, description)
    ledger.set_change_spec(conn, change_id, spec["kind"], spec)
    try:
        run_workflow(conn, change_id)
    except sm.InvalidTransition as exc:
        if not ledger.get_dependencies(conn, change_id):
            raise NothingDiscovered(change_id, spec) from exc
        raise
    return status(conn, change_id)


def approve(
    conn: sqlite3.Connection, change_id: str, gate_name: str, approved_by: str
) -> dict[str, Any]:
    """
    Record a human approval and continue the workflow.

    `legacy_removal` is re-checked against the gate here exactly as the API does
    it: a human must not be able to approve past an unverified consumer just
    because they used the terminal instead of the browser.

    The gate name and the current state are validated for the same reason. The
    API rejects an unknown gate with 400 and a wrong-state approval with 409;
    the CLI used to accept both, writing an approval row for a gate that does
    not exist and then advancing the state machine, which surfaced as an
    uncaught InvalidTransition traceback rather than a usable error.
    """
    if gate_name not in ALLOWED_GATES:
        raise ValueError(
            f"Unknown gate '{gate_name}'. Allowed: {sorted(ALLOWED_GATES)}."
        )

    # The safety verdict is checked BEFORE the state, deliberately. Both refuse
    # the approval, but they are not interchangeable: an unverified change must
    # always be refused *as unverified* (PermissionError, EXIT_NOT_PROVEN_SAFE)
    # so callers and CI cannot mistake "not proven safe" for a mere sequencing
    # complaint. Checking state first would mask the real reason.
    if gate_name == "legacy_removal":
        decision = gate.evaluate_gate(conn, change_id)
        if decision.result != "VERIFIED":
            raise PermissionError(
                f"gate is {decision.result}: {decision.reason}"
            )

    expected = _GATE_STATES[gate_name]
    current = sm.get_state(conn, change_id)
    if current != expected:
        raise ValueError(
            f"Gate '{gate_name}' requires state '{expected}', "
            f"but change is in '{current}'."
        )

    ledger.record_approval(conn, change_id, gate_name, approved_by)
    sm.advance(conn, change_id)
    if gate_name == "coordinate":
        run_workflow(conn, change_id)
    return status(conn, change_id)


def gate_status(conn: sqlite3.Connection, change_id: str) -> dict[str, Any]:
    """
    The deterministic verdict, read from the one place that computes it.

    Returns the recorded decision when there is one, otherwise a live preview
    marked `decided: false` — the same contract the HTTP projection uses, so a
    caller cannot mistake a preview for a settled verdict.
    """
    recorded = ledger.get_latest_gate_decision(conn, change_id)
    decision = gate.evaluate_gate(conn, change_id)
    return {
        "change_id": change_id,
        "decided": recorded is not None,
        "result": recorded["result"] if recorded else decision.result,
        "reason": recorded["reason"] if recorded else decision.reason,
        "required_consumers": decision.required_consumers,
        "unresolved": decision.unresolved,
        "work_items": [
            {
                "component": w["component"],
                "step_kind": w["step_kind"],
                "status": w["status"],
            }
            for w in ledger.get_work_items(conn, change_id)
        ],
    }


def status(conn: sqlite3.Connection, change_id: str) -> dict[str, Any]:
    change = ledger.get_change(conn, change_id)
    if change is None:
        raise KeyError(f"no such change: {change_id}")
    spec_row = ledger.get_change_spec(conn, change_id)
    return {
        "change_id": change_id,
        "description": change["description"],
        "state": change["status"],
        "kind": spec_row["kind"] if spec_row else None,
        "gate": gate_status(conn, change_id),
    }


def evidence(conn: sqlite3.Connection, change_id: str) -> list[dict[str, Any]]:
    return ledger.get_evidence(conn, change_id)


def graph(conn: sqlite3.Connection, change_id: str) -> dict[str, Any]:
    return gate.build_graph(conn, change_id)


def changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, description, status, updated_at FROM change_request "
        "ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def check(
    conn: sqlite3.Connection,
    description: str,
    spec: dict,
    auto_approve_coordination: bool = True,
) -> dict[str, Any]:
    """
    One-shot: run a change as far as the deterministic gate allows.

    This is the PR-time entry point. Coordination is auto-approved because in
    CI there is no human at a terminal — but `legacy_removal` is NOT, and never
    will be: that is the approval that authorises destroying the old field, and
    it stays human.
    """
    result = start(conn, description, spec)
    change_id = result["change_id"]
    if auto_approve_coordination and result["state"] == "COORDINATE":
        approve(conn, change_id, "coordinate", "interlock-cli")
    return status(conn, change_id)


def discover(
    components_root: str,
    provider: str,
    old: str,
    new: str | None = None,
    kind: str = "field_rename",
) -> dict[str, Any]:
    """
    Run discovery only, against the real source tree, and report what it sees.

    Read-only in the strongest sense available: no workspace copy, no `git
    init`, no ledger, no state machine, nothing written anywhere. The discovery
    agents take their root from `data["fixtures_root"]` and only ever read it,
    so they can be pointed straight at the caller's repository.

    This is what you run first against an unfamiliar codebase. `check` answers
    "is this change safe"; `discover` answers the question that has to come
    first — "does Interlock understand the shape of this repository at all". A
    wrong `--components-root` is invisible in a verdict and obvious here.

    Skipping the workspace copy is not just an optimisation. The copy excludes
    build output but still walks the tree and git-inits every component, which
    is minutes on a large repository — far too slow for the command whose whole
    job is a fast first look.
    """
    import importlib

    from orchestrator.agent_registry import agents_for, make_callable
    from orchestrator.schemas import DiscoveryResult

    root = Path(components_root).resolve()
    components = list_components(str(root), detect=True)
    names = {c["name"] for c in components}

    data: dict[str, Any] = {
        "change_id": "discover",
        "provider": provider,
        "old_field": old,
        "new_field": new or "",
        "fixtures_root": str(root),
        "components_root": str(root),
        "dependencies": [],
        "required_consumers": [],
    }
    context = {
        "change_id": "discover",
        "role": "discover",
        "component": None,
        "repo_path": str(root),
        "base_url": None,
        "data": data,
    }

    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    ran: list[str] = []
    failed: list[dict[str, str]] = []

    for spec in agents_for(kind, "DISCOVERY"):
        try:
            importlib.import_module(spec.import_path)
            raw = make_callable(spec)(context)
            result = DiscoveryResult.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            # One scanner failing must not hide what the others found. A
            # discovery agent raising here is itself worth reporting, which is
            # why the role and the error are kept rather than swallowed.
            failed.append({"agent": spec.role, "error": f"{type(exc).__name__}: {exc}"})
            continue
        ran.append(spec.role)
        for dep in result.dependencies:
            key = (dep.from_component, dep.to_component, dep.edge_type)
            edges.setdefault(key, {
                "from": dep.from_component,
                "to": dep.to_component,
                "edge_type": dep.edge_type,
                "reason": dep.reason,
                "found_by": spec.role,
            })

    ordered = sorted(edges.values(), key=lambda e: (e["to"], e["edge_type"]))

    # Grouped per consumer, because the edge list alone does not answer the
    # question that matters. Every consumer picks up an `undocumented` edge the
    # moment repo-map reads the symbol in its source, so "has an undocumented
    # edge" is nearly universal and says nothing. What distinguishes the
    # dangerous consumer is the absence of an API contract: it couples through
    # an event subscription or a shared table, so no contract review would ever
    # have surfaced it, and nobody finds out until production.
    consumers: list[dict[str, Any]] = []
    for name in sorted({e["to"] for e in ordered if e["from"] == provider}):
        mine = [e for e in ordered if e["from"] == provider and e["to"] == name]
        types = sorted({e["edge_type"] for e in mine})
        consumers.append({
            "name": name,
            "edge_types": types,
            "in_api_contract": "api" in types,
            "reasons": [e["reason"] for e in mine if e["reason"]],
        })

    # Only when the answer is unhelpful. On a repository Interlock already
    # understands this would be pure cost, and the suggestion would be noise
    # next to a result that is already correct.
    unhelpful = not components or not ordered or provider not in names
    suggested = suggest_roots(str(root), provider=provider) if unhelpful else []

    return {
        "components_root": str(root),
        "provider": provider,
        "provider_is_a_component": provider in names,
        "suggested_roots": [s for s in suggested if s["path"] != str(root)],
        "kind": kind,
        "old_symbol": old,
        "new_symbol": new or None,
        "components": components,
        "edges": ordered,
        "consumers": consumers,
        "undocumented": [c for c in consumers if not c["in_api_contract"]],
        "agents_run": ran,
        "agents_failed": failed,
    }


def manifest_plan(components_root: str, write: bool = False) -> dict[str, Any]:
    """
    What `interlock.toml` each component would get, and optionally write them.

    Never overwrites an existing manifest. A hand-written one encodes a
    decision someone made about how their component is tested; a guess derived
    from a build file must not silently replace it.

    Components that need no manifest are reported and skipped: a Python
    component with a pytest layout is already covered by the built-in default,
    and writing a file that restates it is noise a reader has to check.
    """
    from orchestrator import toolchain
    from orchestrator.manifest import MANIFEST_FILENAME

    root = Path(components_root).resolve()
    entries: list[dict[str, Any]] = []
    written: list[str] = []

    for component in list_components(str(root), detect=True):
        directory = root / component["name"]
        found = toolchain.detect(directory)
        path = directory / MANIFEST_FILENAME

        if component["has_manifest"]:
            action = "kept"
        elif not toolchain.needs_manifest(found):
            action = "not needed"
        elif found.test_command is None:
            # A stub is still worth writing: it names the component, records
            # what was inspected, and leaves one blank to fill. The alternative
            # is the author guessing which file Interlock even looks for.
            action = "written (incomplete)" if write else "would write (incomplete)"
        else:
            action = "written" if write else "would write"

        if write and action.startswith("written"):
            try:
                path.write_text(found.to_toml(), encoding="utf-8")
                written.append(str(path))
            except OSError as exc:  # noqa: PERF203 - one bad component must not stop the rest
                action = f"failed: {exc}"

        entries.append({
            "name": component["name"],
            "path": str(path),
            "action": action,
            "detected": found.as_dict(),
            "toml": found.to_toml(),
        })

    return {
        "components_root": str(root),
        "components": entries,
        "written": written,
        "wrote_anything": bool(written),
    }
