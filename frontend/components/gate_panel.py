"""
Deterministic gate panel.

This component performs NO evaluation.  It renders exactly what
GET /change-requests/{id}/gate returned.  When the backend has not yet
written a gate_decision row (``decided`` false), the panel says so instead of
presenting the live preview as a verdict.
"""

from __future__ import annotations

import html

import streamlit as st

from utils import theme
from utils.derive import MIGRATION_STATUS_ICON, migration_progress


def render_gate_panel(gate: dict | None, error: str | None = None) -> None:
    st.markdown(
        theme.panel_header(
            "Deterministic safety gate",
            "The backend decision, shown verbatim. It is plain read-only "
            "Python with no LLM involved, and this UI never computes or "
            "overrides it. PENDING means the orchestrator has not evaluated "
            "the gate yet - it is not a verdict.",
        ),
        unsafe_allow_html=True,
    )

    if error:
        st.markdown("</div>", unsafe_allow_html=True)
        st.error(f"Gate status unavailable — {error}")
        return

    if not gate:
        st.markdown(
            '<div class="il-empty">Gate not evaluated yet.</div></div>',
            unsafe_allow_html=True,
        )
        return

    decided = bool(gate.get("decided"))
    result = gate.get("result", "")
    reason = gate.get("reason", "")

    if not decided:
        verdict = "PENDING"
        css = "il-gate-pending"
        sub = "The orchestrator has not evaluated the gate yet."
    elif result == "VERIFIED":
        verdict = "VERIFIED"
        css = "il-gate-ok"
        sub = reason
    else:
        verdict = "NOT PROVEN SAFE"
        css = "il-gate-blocked"
        sub = reason

    st.markdown(
        f'<div class="il-gate {css}">'
        f'<div class="il-gate-verdict">{html.escape(verdict)}</div>'
        f'<div class="il-gate-reason">{html.escape(sub)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )

    unresolved = gate.get("unresolved") or []
    if unresolved:
        chips = "".join(
            f'<span class="il-chip il-chip-blocked">{html.escape(c)}</span>'
            for c in unresolved
        )
        st.markdown(
            f'<div style="margin-top:0.7rem">'
            f'<div class="il-panel-title">Unresolved consumers</div>{chips}</div>',
            unsafe_allow_html=True,
        )

    if not decided and gate.get("required_consumers"):
        st.markdown(
            '<div style="color:var(--il-muted);font-size:0.72rem;margin-top:0.6rem">'
            "Outstanding consumers shown above are a live read of the ledger, "
            "not a recorded decision.</div>",
            unsafe_allow_html=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)


def render_migration_progress(gate: dict | None) -> None:
    """Per-consumer migration status, straight from the backend projection."""
    st.markdown(
        theme.panel_header(
            "Migration progress",
            "Per-consumer migration status from the backend ledger. Every "
            "required consumer must reach verified before the gate can pass, "
            "including any consumer that was discovered rather than "
            "documented.",
        ),
        unsafe_allow_html=True,
    )

    consumers = (gate or {}).get("consumers") or []
    if not consumers:
        st.markdown(
            '<div class="il-empty">No consumers planned yet.</div></div>',
            unsafe_allow_html=True,
        )
        return

    verified, total = migration_progress(gate)
    rows = []
    for item in consumers:
        status = item.get("status", "pending")
        icon = MIGRATION_STATUS_ICON.get(status, "?")
        rows.append(
            f'<div class="il-consumer">'
            f'<span class="name">{html.escape(item.get("consumer", "?"))}</span>'
            f'<span class="st-{html.escape(status)}">{icon} {html.escape(status)}</span>'
            f"</div>"
        )

    st.markdown("".join(rows), unsafe_allow_html=True)
    st.markdown(
        f'<div style="color:var(--il-muted);font-size:0.75rem;margin-top:0.4rem">'
        f"{verified} of {total} consumers verified</div></div>",
        unsafe_allow_html=True,
    )
    st.progress(verified / total if total else 0.0)
