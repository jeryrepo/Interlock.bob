"""Terminal-style agent activity feed, derived from backend evidence rows."""

from __future__ import annotations

import html

import streamlit as st

from utils import theme
from utils.derive import build_activity_feed


def _timestamp(raw: str) -> str:
    """Render an ISO timestamp as HH:MM:SS, or blank when unparseable."""
    if not raw:
        return "--:--:--"
    part = raw.split("T")[-1]
    return part[:8] if len(part) >= 8 else raw[:8]


def render_activity_feed(evidence: list[dict], *, running: bool) -> None:
    """
    Render agent events.

    ``running`` reflects whether the backend is mid-phase; it only controls the
    trailing cursor line and never invents an outcome.
    """
    events = build_activity_feed(evidence)

    st.markdown(
        theme.panel_header(
            "Agent activity",
            "One line per evidence row the agents wrote, oldest first. Each "
            "line is a claim the backend stored - nothing here is simulated. A "
            "hidden dependency discovered line marks a hypothesis: something "
            "found in source code that the published contract never documented.",
        ),
        unsafe_allow_html=True,
    )

    if not events:
        st.markdown(
            '<div class="il-term"><span class="d">'
            "no evidence yet — waiting for the first agent to report"
            "</span></div></div>",
            unsafe_allow_html=True,
        )
        return

    rows = []
    for ev in events:
        rows.append(
            '<div class="row">'
            f'<span class="t">{_timestamp(ev["created_at"])}</span>  '
            f'<span class="ph">{html.escape(ev["phase"]):<16}</span>'
            f'<span class="sj">{html.escape(ev["subject"])}</span> '
            f'<span class="d">→</span> '
            f'<span class="{ev["level"]}">{html.escape(ev["outcome"])}</span>'
            "</div>"
            f'<div class="row"><span class="d">{" " * 12}'
            f'{html.escape(ev["detail"])}</span></div>'
        )

    if running:
        rows.append('<div class="row"><span class="cursor">▊ agent running…</span></div>')

    st.markdown(
        f'<div class="il-term">{"".join(rows)}</div></div>',
        unsafe_allow_html=True,
    )
