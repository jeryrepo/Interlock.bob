"""
interlock_cli/review.py
========================
Renders a change into a pull-request review comment.

This lives in Python rather than as inline JavaScript in the workflow file for
one reason: it can be tested. A review renderer buried in a `github-script`
block is exercised only when a real PR runs, which means it is exercised only
when it is too late to find out it was wrong.

The renderer is pure — verdict dict in, markdown out — so the GitHub Action
becomes a thin `interlock review --format markdown` call, and the same output
can be printed locally before opening the PR.

What the comment must convey, in priority order:

1. the verdict, unambiguously, in the first line;
2. exactly which components are unproven, so the author knows what to fix;
3. the undocumented consumers, because those are the ones nobody knew about and
   the whole reason the tool exists;
4. the evidence trail, so nothing has to be taken on trust.
"""

from __future__ import annotations

from typing import Any

_VERDICT_HEADLINE = {
    "VERIFIED": "✅ Interlock — VERIFIED",
    "NOT_PROVEN_SAFE": "❌ Interlock — NOT PROVEN SAFE",
}

_STATUS_ICON = {
    "verified": "✅",
    "failed": "❌",
    "in_progress": "⏳",
    "pending": "⬜",
    "blocked": "🚫",
}

_STEP_LABEL = {
    "provider_patch": "provider patch",
    "migrate": "migrated",
    "subscribe": "switched transport",
    "webhook_quiet": "webhook drained",
}


def render_markdown(
    status: dict[str, Any],
    graph: dict[str, Any] | None = None,
    risks: list[dict[str, Any]] | None = None,
) -> str:
    """
    Render a PR comment for one change.

    `status` is the payload from `core.status()`; `graph` and `risks` are
    optional enrichments from `core.graph()` and the risk-filtered evidence.
    """
    gate = status["gate"]
    verdict = gate["result"]
    lines: list[str] = []

    lines.append(f"## {_VERDICT_HEADLINE.get(verdict, verdict)}")
    lines.append("")
    lines.append(gate["reason"])
    lines.append("")

    if not gate["decided"]:
        lines.append(
            "> This is a live preview — the orchestrator has not recorded a "
            "final decision yet."
        )
        lines.append("")

    unresolved = gate.get("unresolved") or []
    if unresolved:
        lines.append(f"**Blocking ({len(unresolved)}):** " + ", ".join(
            f"`{u}`" for u in unresolved
        ))
        lines.append("")

    work_items = gate.get("work_items") or []
    if work_items:
        lines.append("| Component | Step | Status |")
        lines.append("| --- | --- | --- |")
        for item in sorted(work_items, key=lambda w: (w["component"], w["step_kind"])):
            icon = _STATUS_ICON.get(item["status"], "•")
            step = _STEP_LABEL.get(item["step_kind"], item["step_kind"])
            lines.append(
                f"| `{item['component']}` | {step} | {icon} {item['status']} |"
            )
        lines.append("")

    hidden = _undocumented(graph)
    if hidden:
        lines.append("### Consumers not in any published contract")
        lines.append("")
        lines.append(
            "Found by reading source. These are the dependencies that break "
            "production because nobody knew they existed."
        )
        lines.append("")
        for edge in hidden:
            lines.append(f"- `{edge['to']}` — {edge.get('reason') or edge['edge_type']}")
        lines.append("")

    if risks:
        lines.append("### Risks recorded")
        lines.append("")
        for risk in risks:
            content = risk.get("content") or {}
            name = content.get("risk", "risk")
            detail = str(content.get("detail", "")).strip()
            lines.append(f"- **{name}** ({risk.get('subject', '?')}) — {detail[:200]}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"<sub>change `{status['change_id']}` · kind `{status.get('kind') or 'n/a'}` · "
        f"state `{status['state']}` · verdict from the deterministic gate, "
        f"which no agent can override.</sub>"
    )
    return "\n".join(lines)


def _undocumented(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Edges representing consumers absent from any published contract."""
    if not graph:
        return []
    return [
        e for e in graph.get("edges", [])
        if e.get("edge_type") in ("undocumented", "event")
    ]


def render_summary(status: dict[str, Any]) -> str:
    """One-line summary, for a CI job name or a commit status."""
    gate = status["gate"]
    unresolved = gate.get("unresolved") or []
    if gate["result"] == "VERIFIED":
        n = len(gate.get("required_consumers") or [])
        return f"VERIFIED — {n} consumer(s) proven safe"
    return f"NOT_PROVEN_SAFE — blocking: {', '.join(unresolved) or 'unknown'}"
