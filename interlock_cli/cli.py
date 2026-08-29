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
                               topic, webhook_path, endpoint)
    except Exception as exc:
        typer.secho(f"invalid change spec: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(core.EXIT_ERROR)

    conn = core.open_ledger(db)
    result = core.check(conn, f"{old} -> {new}", spec)

    _echo(result, as_json, lambda r: (
        typer.echo(f"\nchange  {r['change_id']}"),
        typer.echo(f"kind    {r['kind']}"),
        typer.echo(f"state   {r['state']}"),
        _print_gate(r["gate"]),
    ))

    if result["gate"]["result"] != "VERIFIED":
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
    conn = core.open_ledger(db)
    result = core.start(conn, f"{old} -> {new}", spec)
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
    except KeyError as exc:
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

    if fmt == "summary":
        typer.echo(review_mod.render_summary(result))
    elif fmt == "json":
        typer.echo(json.dumps(
            {"status": result, "graph": graph, "risks": risks},
            indent=2, default=str,
        ))
    else:
        typer.echo(review_mod.render_markdown(result, graph, risks))

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
                        "proves": (
                            a.step_kind
                            if a.per_component or a.step_kind == "provider_patch"
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


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(app())
