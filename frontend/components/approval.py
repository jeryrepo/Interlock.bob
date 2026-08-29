"""
Human approval controls.

Buttons are shown only when the backend state permits the gate:
  coordinate      requires state COORDINATE
  legacy_removal  requires state APPROVE

The UI does not decide whether an approval is safe — it posts to the backend
and renders whatever the backend answers, including 409 rejections.
"""

from __future__ import annotations

import html

import streamlit as st

from utils import theme
from utils.api_client import ApiError, InterlockClient

# gate -> state the backend requires (mirrors orchestrator/main.py).
GATE_STATE = {
    "coordinate": "COORDINATE",
    "legacy_removal": "APPROVE",
}

GATE_LABEL = {
    "coordinate": "Approve coordination plan",
    "legacy_removal": "Approve legacy field removal",
}

GATE_HELP = {
    "coordinate": "Authorises the provider patch and consumer migrations.",
    "legacy_removal": "Authorises removal of the deprecated field. "
                      "Backend refuses this unless the gate is VERIFIED.",
}


def render_approvals(
    client: InterlockClient,
    change: dict | None,
    approvals: dict[str, dict],
    *,
    approved_by: str,
) -> bool:
    """
    Render approval controls.  Returns True when an approval was accepted,
    so the caller can refresh its snapshot.
    """
    st.markdown(
        theme.panel_header(
            "Human approval",
            "The two gates that require a person. A button appears only when "
            "the backend state allows that gate, and the backend independently "
            "refuses legacy removal unless the safety gate reads VERIFIED - "
            "approving here cannot bypass it.",
        ),
        unsafe_allow_html=True,
    )

    if not change:
        st.markdown(
            '<div class="il-empty">No change request loaded.</div></div>',
            unsafe_allow_html=True,
        )
        return False

    state = change.get("status", "")
    change_id = change.get("id", "")
    acted = False

    for gate, required_state in GATE_STATE.items():
        recorded = approvals.get(gate)
        if recorded:
            st.markdown(
                f'<div class="il-consumer">'
                f'<span class="name">{html.escape(gate)}</span>'
                f'<span class="st-verified">✓ {html.escape(recorded["approved_by"])}'
                f' · {html.escape(recorded["approved_at"][:19])}</span></div>',
                unsafe_allow_html=True,
            )
            continue

        if state == required_state:
            if st.button(
                GATE_LABEL[gate],
                key=f"approve-{gate}",
                type="primary",
                use_container_width=True,
                help=GATE_HELP[gate],
            ):
                try:
                    resp = client.approve(change_id, gate, approved_by)
                except ApiError as exc:
                    st.error(f"Approval rejected — {exc}")
                else:
                    st.success(f"{gate} approved → {resp['new_status']}")
                    acted = True
        else:
            st.markdown(
                f'<div class="il-consumer">'
                f'<span class="name">{html.escape(gate)}</span>'
                f'<span class="st-pending">locked · needs '
                f'{html.escape(required_state)}</span></div>',
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)
    return acted
