"""State rail + change header — renders the backend's current workflow state."""

from __future__ import annotations

import html
import re

import streamlit as st

from utils import theme
from utils.derive import STATE_CAPTIONS, STATES, state_index

# Matches "customer_id -> account_id" inside a free-text description so the
# headline can highlight the field rename.  Falls back to plain text.
_RENAME = re.compile(r"([A-Za-z_][\w.]*)\s*(?:->|→)\s*([A-Za-z_][\w.]*)")


def _rename_html(description: str) -> str | None:
    """Render ``old -> new`` as struck-through old beside the new name."""
    match = _RENAME.search(description or "")
    if not match:
        return None
    return (
        f'<span class="from">{html.escape(match.group(1))}</span>'
        f'<span class="arrow">→</span>'
        f'<span class="to">{html.escape(match.group(2))}</span>'
    )


def render_change_header(change: dict | None) -> None:
    """Render the current change description, id and status chip."""
    if not change:
        st.markdown(
            '<div class="il-empty">No change request loaded.</div>',
            unsafe_allow_html=True,
        )
        return

    description = change.get("description", "")
    headline = _rename_html(description) or html.escape(description)

    status = change.get("status", "UNKNOWN")
    kind = {
        "DONE": "ok",
        "COORDINATE": "wait",
        "APPROVE": "wait",
        "GATE_DECISION": "blocked",
    }.get(status, "info")

    st.markdown(
        theme.panel_header(
            "Current change",
            "The change request as the backend stored it. A from -> to rename "
            "is highlighted; the chips show the current workflow state and the "
            "short change ID.",
        )
        + f'<div class="il-change">{headline}</div>'
        f'<div style="margin-top:0.55rem">'
        f'<span class="il-chip il-chip-{kind}">{html.escape(status)}</span>'
        f'<span class="il-chip il-chip-muted">{html.escape(change.get("id", "")[:8])}</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )


def render_state_rail(change: dict | None) -> None:
    """Render the ten-step workflow rail with the backend state highlighted."""
    status = (change or {}).get("status", "")
    idx = state_index(status)

    steps = []
    labels = []
    for i, state in enumerate(STATES):
        if idx < 0:
            cls = ""
        elif i < idx:
            cls = "done"
        elif i == idx:
            cls = "current"
        else:
            cls = ""
        steps.append(f'<div class="il-rail-step {cls}"></div>')
        label_cls = "current" if i == idx else ""
        labels.append(f'<div class="{label_cls}">{state.replace("_", " ")}</div>')

    caption = STATE_CAPTIONS.get(status, "Waiting for backend state")

    st.markdown(
        theme.panel_header(
            "Workflow state",
            "Where this change sits in the orchestrator ten-step state "
            "machine. The backend owns the state and the UI only displays it. "
            "Two steps stop for a human: COORDINATE, before any code is "
            "touched, and APPROVE, before the legacy field is removed.",
        )
        + f'<div class="il-rail">{"".join(steps)}</div>'
        f'<div class="il-rail-labels">{"".join(labels)}</div>'
        f'<div style="color:var(--il-muted);font-size:0.8rem;margin-top:0.6rem">'
        f'{html.escape(caption)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
