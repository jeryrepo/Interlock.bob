"""
interlock_cli/cli.py
=====================
`interlock` — the terminal surface.

A thin shell over `interlock_cli.core`. All the logic lives there so the CLI,
the MCP server and the GitHub Action cannot drift apart.

The command that matters is `interlock check`: it runs a change to the
deterministic gate and **exits non-zero when the verdict is NOT_PROVEN_SAFE**.
That exit code is the whole point — it is what makes Interlock usable in a
pre-push hook or a CI step rather than something you have to remember to look at.

    interlock check --old customer_id --new account_id --provider account-service
    echo $?     # 1 if any consumer is unproven

Run `interlock --help` for the full surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from interlock_cli import core, review as review_mod

def _force_utf8_output() -> None:
    """
    Emit UTF-8 regardless of the console's default codepage.

    The review renderer uses status glyphs, and a Windows console defaults to
    cp1252, which cannot encode them — `interlock review` crashed with a
    UnicodeEncodeError rather than printing. `errors="replace"` means an
    exotic terminal degrades a glyph instead of losing the whole report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_force_utf8_output()

app = typer.Typer(
    name="interlock",
    help="Change-safety control plane. Nothing ships until every consumer is proven safe.",
    add_completion=False,
    no_args_is_help=True,
)

_DB = typer.Option("interlock.db", "--db", help="SQLite ledger path.")
_ROOT = typer.Option("fixtures", "--components-root", help="Directory whose subdirectories are components.")
_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON.")


def _echo(payload, as_json: bool, human) -> None:
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        human(payload)


def _print_gate(g: dict) -> None:
    verdict = g["result"] if g["decided"] else f"{g['result']} (preview, not yet decided)"
    colour = typer.colors.GREEN if g["result"] == "VERIFIED" else typer.colors.RED
    typer.secho(f"\n  {verdict}", fg=colour, bold=True)
    typer.echo(f"  {g['reason']}\n")
    if g["work_items"]:
        for w in g["work_items"]:
            mark = "OK  " if w["status"] == "verified" else "--  "
            typer.echo(f"    {mark}{w['component']:<20} {w['step_kind']:<15} {w['status']}")
        typer.echo("")


def _refuse(headline: str, hints: list[str], as_json: bool, payload: dict) -> None:
    """
    Report that nothing could be proven safe, and exit non-zero.

    Deliberately EXIT_NOT_PROVEN_SAFE rather than EXIT_ERROR. A provider that
    is not a component, or an empty dependency graph, is a usage problem - but
    the honest statement about the change is still "not proven safe", and CI
    must block on it either way. Reserving exit 2 for the tool genuinely
    malfunctioning keeps the two distinguishable.
    """
    if as_json:
        typer.echo(json.dumps({"error": headline, "hints": hints, **payload}, indent=2))
    else:
        typer.secho("\n  NOT_PROVEN_SAFE", fg=typer.colors.RED, bold=True)
        typer.echo(f"  {headline}\n")
        for hint in hints:
            typer.echo(f"    - {hint}")
        typer.echo("")
    raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)


def _check_provider(provider: str, components_root: str, as_json: bool) -> None:
    """Refuse a provider that is not a component, before anything runs."""
    problem = core.provider_problem(provider, components_root)
    if problem is None:
        return
    _refuse(
        problem,
        [
            "--provider must be a bare directory name directly under "
            "--components-root",
            "run `interlock discover` to see what Interlock finds there",
        ],
        as_json,
        {
            "provider": provider,
            "components_root": components_root,
            "components": [c["name"] for c in core.list_components(components_root)],
        },
    )


def _nothing_discovered(exc, as_json: bool) -> None:
    """Explain an empty dependency graph instead of raising InvalidTransition."""
    components = [c["name"] for c in core.list_components(exc.components_root)]
    _refuse(
        f"discovery found no component referencing {exc.symbol!r} under "
        f"{exc.components_root!r}, so there is nothing to prove safe.",
        [
            f"components seen ({len(components)}): "
            + (", ".join(components) or "none - is --components-root correct?"),
            "each immediate subdirectory of --components-root is one component; "
            "point it at the directory that holds your services",
            f"check the spelling of {exc.symbol!r} as it appears in the source",
            "the symbol may exist only inside the provider, in which case no "
            "other component depends on it",
        ],
        as_json,
        {
            "change_id": exc.change_id,
            "components_root": exc.components_root,
            "provider": exc.provider,
            "symbol": exc.symbol,
            "components": components,
        },
    )


def _print_suggestions(result: dict) -> None:
    """Offer the levels of the tree that look more like a components root."""
    suggestions = result.get("suggested_roots") or []
    if not suggestions:
        return
    typer.echo("  Directories that look more like a components root:\n")
    for suggestion in suggestions:
        names = ", ".join(suggestion["names"][:5])
        extra = ", ..." if suggestion["components"] > 5 else ""
        mark = "  <- contains your provider" if suggestion.get("has_provider") else ""
        typer.echo(
            f"    --components-root {suggestion['path']}"
            f"    ({suggestion['components']}: {names}{extra}){mark}"
        )
    typer.echo("")


def _print_toolchain(component: dict) -> None:
    """One component's testing story, on one line."""
    detected = component.get("detected") or {}
    language = detected.get("language", "?")
    command = detected.get("test_command")

    if component["has_manifest"]:
        typer.secho(f"{language:<11} interlock.toml", fg=typer.colors.GREEN)
        return

    if not component.get("needs_manifest"):
        # Python with a pytest layout: the built-in default already runs it,
        # so a manifest would only restate what Interlock does anyway.
        typer.echo(f"{language:<11} pytest (built-in default)")
        return

    if command:
        typer.secho(
            f"{language:<11} needs manifest -> {command}", fg=typer.colors.YELLOW
        )
    else:
        typer.secho(
            f"{language:<11} needs manifest -> NO TEST COMMAND FOUND",
            fg=typer.colors.RED,
        )


def _print_manifest_gap(components: list[dict], components_root: str) -> None:
    """
    Name the components whose tests would not run, and how to fix it in one step.

    Without a manifest a component is tested with `python -m pytest .`, which
    on a Go or Java service exits 5 and is recorded as `tests_could_not_run`.
    The gate is right to refuse, but the reason reads like an Interlock fault
    rather than a missing three-line file - and on a polyglot repository it is
    every component at once.
    """
    missing = [c for c in components if c.get("needs_manifest")]
    if not missing:
        return

    unknown = [c for c in missing if not (c.get("detected") or {}).get("test_command")]
    typer.echo("")
    typer.secho(
        f"  {len(missing)} component(s) need an interlock.toml, or their tests "
        f"cannot run:",
        fg=typer.colors.YELLOW,
    )
    for component in missing:
        detected = component.get("detected") or {}
        command = detected.get("test_command") or "?"
        evidence = detected.get("evidence") or "nothing found"
        typer.echo(
            f"    {component['name']:<24} {command:<24} "
            f"({detected.get('confidence', '?')}: {evidence})"
        )

    notes = [
        (c["name"], note)
        for c in missing
        for note in (c.get("detected") or {}).get("notes", [])
    ]
    if notes:
        typer.echo("")
        for name, note in notes:
            typer.echo(f"    note  {name}: {note}")

    typer.echo("")
    typer.echo("  Write them (review before committing):")
    typer.secho(
        f"    interlock manifest --write --components-root {components_root}",
        fg=typer.colors.CYAN,
    )
    if unknown:
        typer.echo(
            f"  {len(unknown)} have no detectable test command - fill those in by hand."
        )


@app.command()
def discover(
    old: str = typer.Option(..., "--old", help="Symbol to trace."),
    provider: str = typer.Option(..., "--provider", help="Component that owns it."),
    new: Optional[str] = typer.Option(None, "--new", help="Replacement symbol, if it already exists."),
    kind: str = typer.Option("field_rename", "--kind", help="field_rename | api_contract_change | transport_migration"),
    components_root: str = _ROOT,
    as_json: bool = _JSON,
) -> None:
    """
    Show what Interlock sees in a repository. Reads only; changes nothing.

    Run this FIRST against an unfamiliar codebase. `check` answers "is this
    change safe"; `discover` answers the question that comes before it - does
    Interlock understand the shape of this repository at all. A components root
    pointed one level too high is invisible in a verdict and obvious here.

    No workspace copy, no git, no ledger, no approval, nothing written.
    """
    try:
        result = core.discover(components_root, provider, old, new, kind)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"discovery could not run: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    if as_json:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    components = result["components"]
    typer.echo(f"\n  {result['components_root']}")
    typer.echo(f"  tracing '{result['old_symbol']}' from provider '{result['provider']}'\n")

    if not components:
        typer.secho("  no components found", fg=typer.colors.RED)
        typer.echo(
            "\n  Interlock treats each immediate subdirectory of --components-root\n"
            "  as one component. This directory has none.\n"
        )
        _print_suggestions(result)
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)

    typer.secho(f"  {len(components)} component(s)", bold=True)
    for component in components:
        mark = "*" if component["name"] == result["provider"] else " "
        typer.echo(f"    {mark} {component['name']:<24} ", nl=False)
        _print_toolchain(component)

    if not result["provider_is_a_component"]:
        typer.secho(
            f"\n  '{result['provider']}' is not one of them - --provider must be a "
            f"bare directory name from the list above.",
            fg=typer.colors.RED,
        )
        typer.echo("")
        _print_suggestions(result)

    typer.echo("")
    if not result["edges"]:
        typer.secho("  no dependency edges", fg=typer.colors.YELLOW)
        typer.echo(
            "\n  Nothing outside the provider references that symbol. Either the\n"
            "  spelling differs in the source, the components root is pointed at\n"
            "  the wrong level, or the symbol genuinely has no consumers.\n"
            "  `interlock check` would refuse this change for the same reason.\n"
        )
        _print_suggestions(result)
        return

    typer.secho(
        f"  {len(result['consumers'])} consumer(s) of '{result['old_symbol']}'", bold=True
    )
    for consumer in result["consumers"]:
        flag = "" if consumer["in_api_contract"] else "  <- no API contract"
        colour = None if consumer["in_api_contract"] else typer.colors.YELLOW
        typer.echo(
            f"    {consumer['name']:<28} {','.join(consumer['edge_types']):<24}", nl=False
        )
        typer.secho(flag, fg=colour)

    hidden = result["undocumented"]
    if hidden:
        typer.echo("")
        typer.secho(
            f"  {len(hidden)} consumer(s) appear in no API contract: "
            + ", ".join(c["name"] for c in hidden),
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "  They couple through events or a shared schema, so no contract\n"
            "  review would surface them. These are the ones that break."
        )

    _print_manifest_gap(components, result["components_root"])

    if result["agents_failed"]:
        typer.echo("")
        for failure in result["agents_failed"]:
            typer.secho(f"  {failure['agent']} failed: {failure['error']}",
                        fg=typer.colors.RED)
    typer.echo("")


@app.command()
def manifest(
    components_root: str = _ROOT,
    write: bool = typer.Option(
        False, "--write", help="Create the files. Without this, only previews them."
    ),
    as_json: bool = _JSON,
) -> None:
    """
    Propose an `interlock.toml` for each component, from its build files.

    Interlock tests a component with whatever `interlock.toml` declares, and
    falls back to `python -m pytest .` when there is none - which on a Go or
    Java service collects nothing and is recorded as `tests_could_not_run`.
    This reads the markers already in the repository (`go.mod`, `package.json`,
    `pom.xml`, `Cargo.toml`, a Makefile with a `test:` target) and writes the
    manifest those imply.

    A guess, deliberately shown before it is used. Nothing here reaches the
    gate: run without `--write` to see what it would do, and read the files
    before committing them. An existing manifest is never overwritten.
    """
    try:
        plan = core.manifest_plan(components_root, write=write)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"could not inspect components: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    if as_json:
        typer.echo(json.dumps(plan, indent=2, default=str))
        return

    components = plan["components"]
    typer.echo(f"\n  {plan['components_root']}\n")
    if not components:
        typer.secho("  no components found", fg=typer.colors.RED)
        typer.echo("")
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)

    colours = {
        "kept": typer.colors.GREEN,
        "not needed": None,
        "would write": typer.colors.CYAN,
        "written": typer.colors.CYAN,
    }
    for entry in components:
        detected = entry["detected"]
        command = detected["test_command"] or "(fill this in)"
        typer.echo(f"    {entry['name']:<24} ", nl=False)
        typer.secho(f"{entry['action']:<26}", fg=colours.get(entry["action"], typer.colors.YELLOW), nl=False)
        typer.echo(f"{detected['language']:<11} {command}")

    incomplete = [e for e in components if "incomplete" in e["action"]]
    pending = [e for e in components if e["action"].startswith("would write")]

    typer.echo("")
    if write:
        typer.secho(f"  wrote {len(plan['written'])} file(s).", fg=typer.colors.GREEN)
        typer.echo("  Read them before committing - every command is a guess until it runs.")
    elif pending:
        typer.echo(f"  {len(pending)} file(s) would be created. Nothing has been written.")
        typer.secho(
            f"    interlock manifest --write --components-root {components_root}",
            fg=typer.colors.CYAN,
        )
    else:
        typer.echo("  Every component is already covered. Nothing to write.")

    if incomplete:
        typer.echo("")
        typer.secho(
            f"  {len(incomplete)} component(s) have no detectable test command:",
            fg=typer.colors.YELLOW,
        )
        for entry in incomplete:
            typer.echo(f"    {entry['name']:<24} {entry['path']}")
        typer.echo("  Their manifests need a test_command written by hand.")
    typer.echo("")


def _print_security(payload: dict, verbose: bool = False) -> None:
    """Render findings, worst first. Never claims the code is secure."""
    findings = payload["findings"]
    counts = payload["counts"]

    if not findings:
        typer.secho("\n  no findings", fg=typer.colors.GREEN, bold=True)
        typer.echo(
            "  These checks did not fire. That is not the same as secure -\n"
            "  it is the set of things this scanner knows how to look for.\n"
        )
        return

    colour = {"high": typer.colors.RED, "medium": typer.colors.YELLOW, "low": None}
    typer.secho(
        f"\n  {len(findings)} finding(s): "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low",
        fg=typer.colors.RED if counts["high"] else typer.colors.YELLOW,
        bold=True,
    )
    typer.echo("")
    for finding in findings:
        severity = finding.get("severity", "low")
        typer.secho(f"    {severity.upper():<7}", fg=colour.get(severity), nl=False)
        typer.echo(
            f"{finding.get('rule', '?'):<26} "
            f"{finding.get('file', '?')}:{finding.get('line', '?')}"
        )
        if verbose:
            typer.echo(f"            {finding.get('detail', '')}")
            if finding.get("excerpt"):
                typer.echo(f"            {finding['excerpt']}")
    typer.echo("")
    if any(f.get("source") == "model" for f in findings):
        typer.echo(
            "  Findings prefixed `model:` come from watsonx.ai and are proposals,\n"
            "  not pattern matches. They are additive: the model cannot clear a\n"
            "  finding, only suggest one.\n"
        )


@app.command()
def security(
    components_root: str = _ROOT,
    old: Optional[str] = typer.Option(None, "--old", help="Symbol being changed, for PII and auth checks."),
    new: Optional[str] = typer.Option(None, "--new", help="Its replacement."),
    verbose: bool = typer.Option(False, "--verbose", help="Show the detail for each finding."),
    as_json: bool = _JSON,
) -> None:
    """
    Report security findings in a component tree. Reads only; changes nothing.

    Checks for committed secrets, the changed symbol flowing into logs or
    authorisation code, disabled TLS verification, plaintext endpoints and
    committed credential files. With IBM credentials configured it also asks
    watsonx.ai for issues patterns cannot express - additively: the model can
    propose a finding, never clear one.

    Exits non-zero when anything is found, so it works as a pre-push hook. It
    reports findings and, when there are none, says exactly that - it will not
    tell you a change is secure, because no scanner can establish that.
    """
    try:
        payload = core.security_scan(components_root, old or "", new or "")
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"security scan could not run: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        typer.echo(f"\n  {payload['components_root']}")
        _print_security(payload, verbose)

    if payload["findings"]:
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)


@app.command()
def check(
    old: str = typer.Option(..., "--old", help="Symbol being replaced."),
    new: str = typer.Option(..., "--new", help="Replacement symbol."),
    provider: str = typer.Option(..., "--provider", help="Component that owns the change."),
    kind: str = typer.Option("field_rename", "--kind", help="field_rename | api_contract_change | transport_migration"),
    components_root: str = _ROOT,
    topic: Optional[str] = typer.Option(None, "--topic", help="Pub/sub topic (transport_migration)."),
    webhook_path: Optional[str] = typer.Option(None, "--webhook-path", help="Retired webhook path."),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="API endpoint (api_contract_change)."),
    implementation: str = typer.Option(
        "builtin", "--implementation",
        help="builtin: Interlock edits the code (Python only). "
             "external: you or another agent already did the work and Interlock "
             "verifies it (any language).",
    ),
    fail_on_security: bool = typer.Option(
        False, "--fail-on-security",
        help="Also exit non-zero when the security agent reports any finding.",
    ),
    db: str = _DB,
    as_json: bool = _JSON,
) -> None:
    """
    Run a change to the gate and exit non-zero if it is not proven safe.

    Coordination is auto-approved because CI has no human at a terminal.
    Legacy removal is never auto-approved.
    """
    try:
        spec = core.build_spec(kind, provider, old, new, components_root,
                               topic, webhook_path, endpoint, implementation)
    except Exception as exc:
        typer.secho(f"invalid change spec: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    _check_provider(provider, components_root, as_json)

    conn = core.open_ledger(db)
    try:
        result = core.check(conn, f"{old} -> {new}", spec)
    except core.NothingDiscovered as exc:
        _nothing_discovered(exc, as_json)

    _echo(result, as_json, lambda r: (
        typer.echo(f"\nchange  {r['change_id']}"),
        typer.echo(f"kind    {r['kind']}"),
        typer.echo(f"state   {r['state']}"),
        _print_gate(r["gate"]),
    ))

    if result["gate"]["result"] != "VERIFIED":
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)

    # Checked only after the verdict, and only when asked. Security findings
    # are advisory by design: they are recorded as evidence and rendered into
    # the PR review either way, and the gate never sees them. This flag is how
    # a pipeline opts into treating them as blocking without changing what
    # VERIFIED means for everyone else.
    if fail_on_security:
        security = core.security_findings(conn, result["change_id"])
        if security["findings"]:
            if not as_json:
                typer.secho(
                    f"  gate is VERIFIED, but --fail-on-security is set and "
                    f"{len(security['findings'])} finding(s) were recorded.",
                    fg=typer.colors.RED,
                )
                _print_security(security)
            raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)


@app.command()
def start(
    old: str = typer.Option(..., "--old"),
    new: str = typer.Option(..., "--new"),
    provider: str = typer.Option(..., "--provider"),
    kind: str = typer.Option("field_rename", "--kind"),
    components_root: str = _ROOT,
    db: str = _DB,
    as_json: bool = _JSON,
) -> None:
    """Create a change and run agents up to the coordination gate."""
    spec = core.build_spec(kind, provider, old, new, components_root)
    _check_provider(provider, components_root, as_json)

    conn = core.open_ledger(db)
    try:
        result = core.start(conn, f"{old} -> {new}", spec)
    except core.NothingDiscovered as exc:
        _nothing_discovered(exc, as_json)
    _echo(result, as_json, lambda r: (
        typer.echo(f"\nchange  {r['change_id']}"),
        typer.echo(f"state   {r['state']}  (awaiting approval)"),
        _print_gate(r["gate"]),
    ))


@app.command()
def approve(
    change_id: str = typer.Argument(..., help="Change id."),
    gate_name: str = typer.Option(..., "--gate", help="coordinate | legacy_removal"),
    by: str = typer.Option("cli", "--by", help="Who is approving."),
    db: str = _DB,
    as_json: bool = _JSON,
) -> None:
    """Record a human approval. `legacy_removal` is refused unless the gate is VERIFIED."""
    conn = core.open_ledger(db)
    try:
        result = core.approve(conn, change_id, gate_name, by)
    except PermissionError as exc:
        typer.secho(f"refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)
    except (KeyError, ValueError) as exc:
        # ValueError covers an unknown gate name and an approval offered from
        # the wrong state — both of which the API rejects with a status code
        # rather than a traceback.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)
    _echo(result, as_json, lambda r: typer.echo(f"state   {r['state']}"))


@app.command()
def gate(
    change_id: str = typer.Argument(...),
    db: str = _DB,
    as_json: bool = _JSON,
) -> None:
    """Print the deterministic verdict. Exits non-zero if not VERIFIED."""
    conn = core.open_ledger(db)
    g = core.gate_status(conn, change_id)
    _echo(g, as_json, _print_gate)
    if g["result"] != "VERIFIED":
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)


@app.command()
def status(
    change_id: str = typer.Argument(...),
    db: str = _DB,
    as_json: bool = _JSON,
) -> None:
    """Show a change's current state and gate verdict."""
    conn = core.open_ledger(db)
    try:
        result = core.status(conn, change_id)
    except KeyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)
    _echo(result, as_json, lambda r: (
        typer.echo(f"\nchange  {r['change_id']}"),
        typer.echo(f"kind    {r['kind']}"),
        typer.echo(f"state   {r['state']}"),
        _print_gate(r["gate"]),
    ))


@app.command(name="list")
def list_changes(db: str = _DB, as_json: bool = _JSON) -> None:
    """List known changes, newest first."""
    conn = core.open_ledger(db)
    rows = core.changes(conn)
    _echo(rows, as_json, lambda rs: [
        typer.echo(f"  {r['id']}  {r['status']:<14} {r['description']}") for r in rs
    ] or typer.echo("  (none)"))


@app.command()
def evidence(
    change_id: str = typer.Argument(...),
    claim_type: Optional[str] = typer.Option(None, "--type", help="Filter by claim type."),
    db: str = _DB,
    as_json: bool = _JSON,
) -> None:
    """Show the evidence ledger for a change."""
    conn = core.open_ledger(db)
    items = core.evidence(conn, change_id)
    if claim_type:
        items = [e for e in items if e["claim_type"] == claim_type]
    _echo(items, as_json, lambda es: [
        typer.echo(f"  {e['claim_type']:<18} {e['subject']:<20} {str(e['source_revision'] or '')[:10]}")
        for e in es
    ] or typer.echo("  (none)"))


@app.command()
def review(
    change_id: Optional[str] = typer.Argument(None, help="Change id. Omitted with --run to create one."),
    run: bool = typer.Option(False, "--run", help="Run the change first, then render."),
    old: Optional[str] = typer.Option(None, "--old"),
    new: Optional[str] = typer.Option(None, "--new"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    kind: str = typer.Option("field_rename", "--kind"),
    components_root: str = _ROOT,
    fmt: str = typer.Option("markdown", "--format", help="markdown | summary | json"),
    db: str = _DB,
) -> None:
    """
    Render a pull-request review for a change.

    This is what the GitHub Action posts. Run it locally before opening a PR to
    see exactly what reviewers will see. Exits non-zero when the verdict is
    NOT_PROVEN_SAFE, so it is equally usable as the blocking check itself.

        interlock review --run --old customer_id --new account_id \
            --provider account-service --format markdown
    """
    conn = core.open_ledger(db)

    if run:
        if not (old and new and provider):
            typer.secho("--run requires --old, --new and --provider",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(core.EXIT_ERROR)
        try:
            spec = core.build_spec(kind, provider, old, new, components_root)
        except Exception as exc:
            typer.secho(f"invalid change spec: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(core.EXIT_ERROR)
        result = core.check(conn, f"{old} -> {new}", spec)
        change_id = result["change_id"]
    elif change_id is None:
        typer.secho("provide a change id, or use --run", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)
    else:
        try:
            result = core.status(conn, change_id)
        except KeyError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(core.EXIT_ERROR)

    graph = core.graph(conn, change_id)
    risks = [e for e in core.evidence(conn, change_id) if e["claim_type"] == "risk"]
    # Read back from the ledger, not re-scanned: these are what the agent saw
    # during the change, at the commits it recorded.
    security_payload = core.security_findings(conn, change_id)

    if fmt == "summary":
        typer.echo(review_mod.render_summary(result))
    elif fmt == "json":
        typer.echo(json.dumps(
            {"status": result, "graph": graph, "risks": risks,
             "security": security_payload},
            indent=2, default=str,
        ))
    else:
        typer.echo(review_mod.render_markdown(
            result, graph, risks, security=security_payload
        ))

    if result["gate"]["result"] != "VERIFIED":
        raise typer.Exit(core.EXIT_NOT_PROVEN_SAFE)


@app.command()
def agents(as_json: bool = _JSON) -> None:
    """
    Show the orchestration map: which agents run, for which change kind, in
    which phase, and which work item each one proves.

    Use this to confirm the fabric is wired the way you think it is. An empty
    phase is not a bug in itself, but it does mean nothing proves that step —
    and the gate will hold the change until something does.
    """
    from orchestrator.agent_registry import AGENT_REGISTRY
    from orchestrator.gate import _DEFAULT_STEP_KINDS, _REQUIRED_PROVIDER_STEPS, _REQUIRED_STEP_KINDS
    from orchestrator.schemas import CHANGE_KINDS
    from orchestrator.state_machine import STATES

    phases = [s for s in STATES if any((k, s) in AGENT_REGISTRY for k in CHANGE_KINDS)]

    payload = {
        kind: {
            "gate_requires": {
                "provider": list(_REQUIRED_PROVIDER_STEPS.get(kind, ())),
                "per_consumer": list(_REQUIRED_STEP_KINDS.get(kind, _DEFAULT_STEP_KINDS)),
            },
            "phases": {
                phase: [
                    {
                        "role": a.role,
                        "module": a.import_path,
                        "per_component": a.per_component,
                        # Only agents that write a work item prove a step.
                        # Discovery, planning and the critic produce evidence,
                        # not proof, and showing a step kind for them would
                        # misrepresent what the gate actually counts.
                        #
                        # Read off the gate's own provider-step policy rather
                        # than naming a step here: a literal "provider_patch"
                        # meant every later provider-side agent silently
                        # reported "evidence only" while actually proving a step.
                        "proves": (
                            a.step_kind
                            if a.per_component
                            or a.step_kind in _REQUIRED_PROVIDER_STEPS.get(kind, ())
                            else None
                        ),
                    }
                    for a in AGENT_REGISTRY.get((kind, phase), ())
                ]
                for phase in phases
            },
        }
        for kind in CHANGE_KINDS
    }

    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    for kind, info in payload.items():
        typer.secho(f"\n{kind}", fg=typer.colors.CYAN, bold=True)
        req = info["gate_requires"]
        typer.echo(
            f"  gate requires: provider {req['provider'] or '(none)'} | "
            f"each consumer {req['per_consumer']}"
        )
        for phase, entries in info["phases"].items():
            if not entries:
                typer.echo(f"    {phase:<12} (no agents)")
                continue
            for i, a in enumerate(entries):
                label = phase if i == 0 else ""
                scope = "per-component" if a["per_component"] else "once"
                proves = f"proves: {a['proves']}" if a["proves"] else "evidence only"
                typer.echo(
                    f"    {label:<12} {a['role']:<24} {scope:<14} {proves}"
                )
    typer.echo("")


def _mcp_server_path() -> Path | None:
    """Where the stdio MCP server actually lives, or None if it is missing."""
    import importlib.util

    spec = importlib.util.find_spec("interlock_mcp")
    if spec is None or not spec.submodule_search_locations:
        return None
    candidate = Path(list(spec.submodule_search_locations)[0]) / "server.py"
    return candidate if candidate.is_file() else None


def _components_root_check(components_root: str) -> dict:
    """
    Report what Interlock would treat as components, and flag a self-copy.

    Optional on purpose. `doctor` must never fail because a path unrelated to
    what the user is doing right now does not exist - that is the bug being
    fixed one check below. It reports; `check` and `discover` decide.

    The working-directory warning matters more than it looks. The workspace
    root defaults to a *relative* `.interlock_work`, so running against
    `--components-root .` from inside your own repository makes each run copy
    the previous run's copy, and the tree grows exponentially with the number
    of runs.
    """
    from agents.discovery.repo_map import component_dirs
    from orchestrator.manifest import MANIFEST_FILENAME

    root = Path(components_root)
    if not root.is_dir():
        return {
            "name": "components root",
            "ok": False,
            "detail": f"{root} does not exist",
            "optional": True,
        }

    resolved = root.resolve()
    names = [d.name for d in component_dirs(resolved)]
    if not names:
        return {
            "name": "components root",
            "ok": False,
            "detail": (
                f"{resolved} has no subdirectories, so Interlock sees no "
                f"components (each immediate subdirectory is one)"
            ),
            "optional": True,
        }

    manifests = sum(
        1 for n in names if (resolved / n / MANIFEST_FILENAME).is_file()
    )
    shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
    detail = f"{len(names)} component(s), {manifests} with interlock.toml: {shown}"

    cwd = Path.cwd().resolve()
    if cwd == resolved or resolved in cwd.parents:
        detail += (
            "  |  WARNING: your working directory is inside it - set "
            "INTERLOCK_WORKSPACE outside it, or the workspace copies itself"
        )

    return {
        "name": "components root",
        "ok": True,
        "detail": detail,
        "optional": True,
    }


@app.command()
def doctor(components_root: str = _ROOT, as_json: bool = _JSON) -> None:
    """
    Report which integrations are configured, and what is missing.

    Calls nothing and spends no model credits: every IBM feature here is
    optional, so "switched off" has to be distinguishable from "misconfigured"
    without paying to find out which. To PROVE the watsonx.ai connection works,
    run `interlock live`.
    """
    import shutil
    from pathlib import Path

    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    settings = _load_settings()
    wx = _watsonx.health(settings.watsonx)

    checks: list[dict] = [
        _components_root_check(components_root),
        {
            "name": "git",
            "ok": shutil.which("git") is not None,
            "detail": "required: implementation agents make real commits",
        },
        {
            "name": "ledger",
            "ok": True,
            "detail": settings.db_path,
        },
        {
            "name": "workspace",
            "ok": True,
            "detail": f"{settings.workspace} (isolated copies; your source is never touched)",
        },
        {
            "name": "watsonx.ai narration",
            "ok": wx["enabled"],
            "detail": wx["reason"] or f"{wx['model_id']} via {wx['scope']}",
            "optional": True,
        },
        {
            "name": "watsonx Orchestrate",
            "ok": settings.orchestrate.configured,
            "detail": (
                settings.orchestrate.instance_url
                if settings.orchestrate.configured
                else "set WATSONX_ORCHESTRATE_INSTANCE_URL and _API_KEY"
            ),
            "optional": True,
        },
        {
            "name": "external-agent endpoint",
            "ok": settings.orchestrate.external_agent_enabled,
            "detail": (
                "enabled"
                if settings.orchestrate.external_agent_enabled
                else "disabled — set INTERLOCK_EXTERNAL_AGENT_KEY to enable"
            ),
            "optional": True,
        },
        {
            "name": "MCP server",
            # Resolved from the installed package, not the working directory.
            # A CWD-relative path made this report MISS - on a check marked
            # non-optional, so `interlock doctor` exited non-zero - from every
            # directory except Interlock's own source tree. Since running from
            # your own repository is the entire point, it failed on the first
            # command a new user types, on a perfectly correct install.
            "ok": _mcp_server_path() is not None,
            "detail": str(_mcp_server_path() or "interlock_mcp is not importable"),
        },
    ]

    # Which MCP client config would IBM Bob actually pick up from here?
    mcp_cfg = core.mcp_client_status()
    detail = mcp_cfg["summary"]
    for scope in ("global", "workspace"):
        if mcp_cfg[scope]["problem"]:
            detail += f"  |  {scope}: {mcp_cfg[scope]['problem']}"
    if mcp_cfg["misplaced_global"]:
        detail += (
            "  |  WARNING: ~/.bob/mcp.json exists but Bob IGNORES it — the "
            "global file is ~/.bob/settings/mcp.json (interlock init --global)"
        )
    checks.append(
        {
            "name": "MCP client config",
            "ok": mcp_cfg["configured"]
            and not any(mcp_cfg[s]["problem"] for s in ("global", "workspace")),
            "detail": detail,
            "optional": True,
        }
    )

    if as_json:
        typer.echo(json.dumps({"checks": checks}, indent=2))
    else:
        typer.echo("")
        for check in checks:
            optional = check.get("optional", False)
            if check["ok"]:
                mark, colour = "OK  ", typer.colors.GREEN
            elif optional:
                mark, colour = "off ", typer.colors.YELLOW
            else:
                mark, colour = "MISS", typer.colors.RED
            typer.secho(f"  {mark}", fg=colour, nl=False)
            typer.echo(f" {check['name']:<26} {check['detail']}")
        typer.echo("")
        typer.echo("  Everything marked 'off' is optional. Interlock's gate, CLI,")
        typer.echo("  MCP server and PR review work without any IBM account.")
        typer.echo("")

    required_failures = [c for c in checks if not c["ok"] and not c.get("optional")]
    if required_failures:
        raise typer.Exit(core.EXIT_ERROR)


@app.command()
def live(as_json: bool = _JSON) -> None:
    """
    Prove the watsonx.ai connection works, end to end.

    `doctor` reports what is configured without calling anything; this actually
    connects. Four stages, each isolating one failure mode so a failure names
    the variable at fault instead of a generic connection error:

    1. variables — IBM_CLOUD_API_KEY and WATSONX_PROJECT_ID/_SPACE_ID present
    2. IAM token — the key is accepted by IBM Cloud
    3. model catalogue — WATSONX_MODEL_ID really exists in your region
    4. inference — one tiny chat call (capped at 5 tokens; credits are finite)

    Exits 0 only when every stage passes. Safe to run repeatedly: the cost of
    stage 4 is a fraction of a cent, and it is the only stage that costs at all.
    """
    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    checks = _watsonx.live_check(_load_settings().watsonx)

    if as_json:
        typer.echo(json.dumps({"checks": checks}, indent=2))
    else:
        typer.echo("")
        for check in checks:
            mark, colour = (
                ("OK  ", typer.colors.GREEN) if check["ok"] else ("FAIL", typer.colors.RED)
            )
            typer.secho(f"  {mark}", fg=colour, nl=False)
            typer.echo(f" {check['name']:<22} {check['detail']}")
        typer.echo("")
        if all(c["ok"] for c in checks):
            typer.secho(
                "  watsonx.ai is connected and working. Set "
                "INTERLOCK_ENABLE_NARRATION=1 to use it.",
                fg=typer.colors.GREEN,
            )
        else:
            typer.echo(
                "  Fix the first FAIL above and re-run. `interlock models` lists "
                "what your region offers."
            )
        typer.echo("")

    if not all(c["ok"] for c in checks):
        raise typer.Exit(core.EXIT_ERROR)


@app.command()
def init(
    target: str = typer.Argument(
        ".",
        help="The repository to configure — the one you open in Bob. Defaults to here.",
    ),
    components_root: Optional[str] = typer.Option(
        None,
        "--components-root",
        help="Directory whose immediate subdirectories are your services, "
        "relative to TARGET. Defaults to TARGET itself.",
    ),
    db: Optional[str] = typer.Option(
        None, "--db", help="Ledger path. Defaults to TARGET/.interlock/interlock.db."
    ),
    workspace: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="Isolated-copy root. Defaults to TARGET/.interlock/work.",
    ),
    global_scope: bool = typer.Option(
        False,
        "--global",
        help="Configure IBM Bob for ALL workspaces (~/.bob/settings/mcp.json) "
        "instead of one repository. TARGET is ignored.",
    ),
    as_json: bool = _JSON,
) -> None:
    """
    Wire a repository up to Interlock: write `.bob/mcp.json` (IBM Bob) and
    `.mcp.json` (Claude Code, Cursor, Copilot) into it, pointing at this
    installation with absolute paths.

    Run it from Interlock's environment, aimed at the repository you work in:

        interlock init ../your-repo --components-root services

    Or once for every workspace Bob opens (note the path Bob reads globally is
    ~/.bob/settings/mcp.json — not ~/.bob/mcp.json, which it ignores):

        interlock init --global

    Open the repository in Bob afterwards and the interlock_* tools are there.
    No IBM credentials are involved — the MCP server is entirely local.
    """
    result = core.init_mcp(
        target,
        components_root,
        db,
        workspace,
        scope="global" if global_scope else "project",
    )

    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo("")
        for problem in result["problems"]:
            typer.secho(f"  MISS {problem}", fg=typer.colors.RED)
        for skip in result["skipped"]:
            typer.secho(
                f"  skip  {skip['path']}: {skip['reason']}", fg=typer.colors.YELLOW
            )
        for path in result["written"]:
            note = (
                "  (replaced the existing interlock entry)"
                if path in result["replaced"]
                else ""
            )
            typer.echo(f"  wrote {path}{note}")
        if result["written"]:
            if result["components_root"] is None:
                typer.echo(
                    "\n  components root: none — agents pass components_root "
                    "per call"
                )
            else:
                typer.echo(f"\n  components root: {result['components_root']}")
            names = result["components"]
            if names:
                shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
                typer.echo(f"  components seen: {len(names)}: {shown}")
            elif result["components_root"] is not None:
                typer.echo(
                    "  components seen: none — each immediate subdirectory of the "
                    "components root is one component"
                )
                _print_suggestions(result)
            if result.get("scope") == "global":
                typer.echo(
                    "  default root is the bundled demo fixtures; agents pass "
                    "components_root per call for real repositories"
                )
            if not result["mcp_sdk_installed"]:
                typer.secho(
                    '  the `mcp` package is missing here — run: pip install -e ".[mcp]"',
                    fg=typer.colors.YELLOW,
                )
            typer.echo(
                "\n  Open the repository in IBM Bob (or Claude Code / Cursor) and the"
            )
            typer.echo(
                "  interlock_* tools are available. `interlock discover` first is "
                "a good habit."
            )
        typer.echo("")

    if result["problems"] or not result["written"]:
        raise typer.Exit(core.EXIT_ERROR)


@app.command()
def models(as_json: bool = _JSON) -> None:
    """
    List the watsonx.ai chat models this region actually offers.

    Run before enabling narration. The catalogue endpoint needs no
    authentication, so this works before you have any credentials — which is
    the point: a live query of us-south does NOT return
    `ibm/granite-3-8b-instruct`, even though IBM's own Prompt Lab screenshots
    show it, and a wrong model id fails at narration time where it is
    confusing to diagnose.

    Models the hackathon guide places out of scope are marked.
    """
    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    settings = _load_settings()
    try:
        available = _watsonx.list_chat_models(settings.watsonx)
    except Exception as exc:  # noqa: BLE001 - report, never traceback at a user
        typer.secho(f"could not list models: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    if as_json:
        typer.echo(json.dumps(available, indent=2))
        return

    configured = settings.watsonx.model_id
    typer.echo(f"\n  {len(available)} chat model(s) at {settings.watsonx.url}\n")
    for model in available:
        if model["forbidden"]:
            mark, colour = "  X ", typer.colors.RED
        elif model["model_id"] == configured:
            mark, colour = "  * ", typer.colors.GREEN
        else:
            mark, colour = "    ", None
        typer.secho(mark, fg=colour, nl=False)
        typer.echo(f"{model['model_id']:<52}{model['short_description'][:60]}")

    typer.echo("")
    if any(m["model_id"] == configured for m in available):
        typer.secho(f"  * WATSONX_MODEL_ID={configured} is available.",
                    fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"  WATSONX_MODEL_ID={configured} is NOT in this region's catalogue "
            f"— narration would fail. Pick one from the list above.",
            fg=typer.colors.YELLOW,
        )
    if any(m["forbidden"] for m in available):
        typer.echo("  X marks models the hackathon guide places out of scope.")
    typer.echo("")


@app.command()
def narrate(
    change_id: str = typer.Argument(..., help="Change id."),
    db: str = _DB,
) -> None:
    """
    Explain an existing verdict in plain English, using watsonx.ai.

    The verdict itself is printed verbatim from the deterministic gate; the
    model only explains the blockers. It cannot change the verdict, and if
    narration is off or unavailable you still get the verdict.
    """
    from orchestrator import watsonx as _watsonx
    from orchestrator.settings import load as _load_settings

    settings = _load_settings()
    conn = core.open_ledger(db)
    try:
        gate = core.gate_status(conn, change_id)
    except Exception as exc:  # noqa: BLE001
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    _print_gate(gate)

    if not settings.watsonx.enabled:
        typer.secho(
            f"  narration off: {settings.watsonx.why_disabled()}",
            fg=typer.colors.YELLOW,
        )
        typer.echo("")
        return

    lines = []
    for item in core.evidence(conn, change_id):
        content = item.get("content") or {}
        detail = content.get("detail") or content.get("risk") or content.get("outcome")
        if detail:
            lines.append(f"{item['subject']}: {detail}")

    with typer.progressbar(length=1, label="asking watsonx.ai") as bar:
        text = _watsonx.narrate(gate, lines, settings.watsonx)
        bar.update(1)

    if text:
        typer.echo("")
        typer.secho("  What this means", bold=True)
        typer.echo(f"  {text}\n")
    else:
        typer.secho(
            "  watsonx.ai was unavailable; the verdict above stands on its own.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("")


def main() -> None:
    try:
        app()
    finally:
        core.close_ledgers()


if __name__ == "__main__":
    sys.exit(app())
