"""Evidence explorer — raw ledger rows from GET /change-requests/{id}/evidence."""

from __future__ import annotations

import html

import streamlit as st

from utils import theme
from utils.derive import evidence_by_type

_CONFIDENCE_CHIP = {
    "confirmed": "ok",
    "hypothesis": "wait",
    "refuted": "blocked",
}

_TYPE_ORDER = ["dependency", "migration_status", "test_result", "risk"]


def render_evidence(evidence: list[dict], error: str | None = None) -> None:
    st.markdown(
        theme.panel_header(
            "Evidence ledger",
            "Raw evidence rows from the backend, grouped by claim type. "
            "Confirmed means proven from a source the agent read; hypothesis is "
            "a lead still to be verified. The path shown is the file the claim "
            "came from.",
        ),
        unsafe_allow_html=True,
    )

    if error:
        st.markdown("</div>", unsafe_allow_html=True)
        st.error(f"Evidence unavailable — {error}")
        return

    if not evidence:
        st.markdown(
            '<div class="il-empty">No evidence yet.</div></div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown("</div>", unsafe_allow_html=True)

    grouped = evidence_by_type(evidence)
    ordered = [t for t in _TYPE_ORDER if t in grouped]
    ordered += [t for t in grouped if t not in _TYPE_ORDER]

    tabs = st.tabs([f"{t} ({len(grouped[t])})" for t in ordered])
    for tab, claim_type in zip(tabs, ordered):
        with tab:
            for row in grouped[claim_type]:
                chip_kind = _CONFIDENCE_CHIP.get(row.get("confidence", ""), "muted")
                st.markdown(
                    f'<div style="margin-bottom:0.35rem">'
                    f'<span class="il-chip il-chip-info">'
                    f'{html.escape(row.get("subject", "?"))}</span>'
                    f'<span class="il-chip il-chip-{chip_kind}">'
                    f'{html.escape(row.get("confidence", ""))}</span>'
                    f'<span style="color:var(--il-muted);font-size:0.72rem;'
                    f'font-family:ui-monospace,Menlo,Consolas,monospace">'
                    f'{html.escape(row.get("source_ref", ""))}</span></div>',
                    unsafe_allow_html=True,
                )
                st.json(row.get("content", {}), expanded=False)
