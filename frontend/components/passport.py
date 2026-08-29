"""
Change Passport — the closing artefact of the demo.

Every field is read from backend data.  Anything the backend has not produced
renders as "not recorded" rather than as a success.
"""

from __future__ import annotations

import html

import streamlit as st

from utils import theme
from utils.derive import (
    coexistence_result,
    contract_test_results,
    critic_assessment,
    migration_progress,
    split_consumers,
)

_NOT_RECORDED = '<span style="color:var(--il-muted)">not recorded</span>'


def _row(key: str, value: str) -> str:
    return (
        f'<div class="il-pp-row"><div class="il-pp-key">{html.escape(key)}</div>'
        f'<div class="il-pp-val">{value}</div></div>'
    )


def _list_or_none(items: list[str], colour: str) -> str:
    if not items:
        return _NOT_RECORDED
    return " ".join(
        f'<span style="color:{colour}">{html.escape(i)}</span>' for i in items
    )


def render_passport(
    change: dict | None,
    graph: dict | None,
    gate: dict | None,
    evidence: list[dict],
    approvals: dict[str, dict],
) -> None:
    """Render the passport.  Only shown by the caller once the gate is decided."""
    if not change:
        return

    documented, undocumented = split_consumers(graph)
    verified, total = migration_progress(gate)
    tests = contract_test_results(evidence)
    passed = [
        t for t in tests
        if (t.get("content") or {}).get("tests_passed") is True
    ]
    coexistence = coexistence_result(evidence)
    critic = critic_assessment(evidence)

    # -- gate --------------------------------------------------------------
    if gate and gate.get("decided"):
        if gate.get("result") == "VERIFIED":
            gate_html = f'<span style="color:var(--il-green);font-weight:700">VERIFIED</span>'
        else:
            gate_html = (
                '<span style="color:var(--il-red);font-weight:700">NOT PROVEN SAFE</span>'
                f'<span style="color:var(--il-muted)"> — {html.escape(gate.get("reason", ""))}</span>'
            )
    else:
        gate_html = _NOT_RECORDED

    # -- tests -------------------------------------------------------------
    if tests:
        colour = "var(--il-green)" if len(passed) == len(tests) else "var(--il-red)"
        tests_html = (
            f'<span style="color:{colour}">{len(passed)}</span>'
            f'<span style="color:var(--il-muted)"> / {len(tests)} contract test '
            f"suites passed</span>"
        )
    else:
        tests_html = _NOT_RECORDED

    # -- coexistence -------------------------------------------------------
    if coexistence:
        content = coexistence.get("content") or {}
        ok = content.get("dual_write_passed") is True
        colour = "var(--il-green)" if ok else "var(--il-red)"
        detail = " ".join(f"{k}={v}" for k, v in content.items())
        coexistence_html = f'<span style="color:{colour}">{html.escape(detail)}</span>'
    else:
        coexistence_html = _NOT_RECORDED

    # -- critic ------------------------------------------------------------
    if critic:
        content = critic.get("content") or {}
        critic_html = html.escape(
            " ".join(f"{k}={v}" for k, v in content.items())
        )
    else:
        critic_html = _NOT_RECORDED

    # -- approvals ---------------------------------------------------------
    if approvals:
        approvals_html = " ".join(
            f'<span style="color:var(--il-green)">{html.escape(g)}</span>'
            f'<span style="color:var(--il-muted)">·{html.escape(a["approved_by"])}</span>'
            for g, a in approvals.items()
        )
    else:
        approvals_html = _NOT_RECORDED

    nodes = (graph or {}).get("nodes", [])
    affected = _list_or_none([n.get("label", n.get("id", "")) for n in nodes], "var(--il-text)")

    body = "".join(
        [
            _row("Change", html.escape(change.get("description", ""))),
            _row("Change ID", html.escape(change.get("id", ""))),
            _row("Final state", html.escape(change.get("status", ""))),
            _row("Affected components", affected),
            _row("Documented consumers", _list_or_none(documented, "var(--il-green)")),
            _row("Undocumented consumers", _list_or_none(undocumented, "var(--il-amber)")),
            _row(
                "Migration status",
                f'<span style="color:var(--il-green)">{verified}</span>'
                f'<span style="color:var(--il-muted)"> / {total} verified</span>'
                if total
                else _NOT_RECORDED,
            ),
            _row("Tests", tests_html),
            _row("Coexistence rehearsal", coexistence_html),
            _row("Critic", critic_html),
            _row("Gate decision", gate_html),
            _row("Approvals", approvals_html),
        ]
    )

    st.markdown(
        f'<div class="il-passport">'
        f'<div class="il-panel-head"><h3>Change Passport</h3>'
        + theme.info_icon(
            "Change Passport",
            "The closing record, assembled only from backend data. Anything "
            "the backend has not produced reads as not recorded rather than "
            "being assumed - so this page can never claim more than was "
            "actually proven.",
        )
        + '</div>'
        f'<div style="color:var(--il-muted);font-size:0.72rem;letter-spacing:0.1em;'
        f'text-transform:uppercase;margin-bottom:0.8rem">'
        f"Interlock · evidence-backed record</div>{body}</div>",
        unsafe_allow_html=True,
    )
