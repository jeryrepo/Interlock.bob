"""
Interlock — Streamlit control room.

Run with:
    streamlit run frontend/streamlit_app.py

The backend orchestrator must be running:
    uvicorn orchestrator.main:app --reload

This UI is a pure view over the orchestrator API.  It never touches SQLite,
never runs an LLM, never evaluates the safety gate, and never turns an API
failure into a success.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import streamlit as st

# Make sibling packages importable when Streamlit runs this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from components.activity_feed import render_activity_feed  # noqa: E402
from components.approval import render_approvals  # noqa: E402
from components.evidence import render_evidence  # noqa: E402
from components.gate_panel import (  # noqa: E402
    render_gate_panel,
    render_migration_progress,
)
from components.graph import (  # noqa: E402
    render_graph,
    render_hidden_dependency_callout,
)
from components.passport import render_passport  # noqa: E402
from components.timeline import (  # noqa: E402
    render_change_header,
    render_state_rail,
)
from utils import theme  # noqa: E402
from utils.api_client import DEFAULT_BASE_URL, ApiError, InterlockClient  # noqa: E402
from utils.derive import approvals_by_gate, hidden_dependencies  # noqa: E402

DEFAULT_DESCRIPTION = "customer_id -> account_id"
POLL_SECONDS = 2.0

# States in which the backend may still be doing agent work, so the UI keeps
# polling.  Waiting-for-human and terminal states do not need polling.
WORKING_STATES = {"INTAKE", "DISCOVERY", "PLANNING", "MODIFY", "REHEARSE", "VERIFY"}

st.set_page_config(
    page_title="Interlock",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.inject()


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init_state() -> None:
    st.session_state.setdefault("change_id", None)
    st.session_state.setdefault("base_url", DEFAULT_BASE_URL)
    st.session_state.setdefault("approved_by", "release-manager")
    st.session_state.setdefault("polling", True)
    st.session_state.setdefault("create_error", None)


_init_state()
client = InterlockClient(base_url=st.session_state["base_url"])


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown(
    '<div class="il-title">INTER<span>LOCK</span></div>'
    '<div class="il-sub">Agentic change-safety control plane &mdash; '
    "nothing ships until every consumer is proven safe.</div>",
    unsafe_allow_html=True,
)
st.write("")


# ---------------------------------------------------------------------------
# Sidebar — connection and controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Backend")
    base_url = st.text_input("Orchestrator URL", value=st.session_state["base_url"])
    if base_url != st.session_state["base_url"]:
        st.session_state["base_url"] = base_url
        st.rerun()

    backend_up = client.health()
    if backend_up:
        st.markdown(theme.chip("connected", "ok"), unsafe_allow_html=True)
    else:
        st.markdown(theme.chip("unavailable", "blocked"), unsafe_allow_html=True)

    st.divider()
    st.markdown("### Change request")
    # Widgets carry a key so their value survives a rerun.  Without one,
    # Streamlit re-applies the literal `value=` on every rerun and silently
    # discards whatever the user typed.
    description = st.text_input(
        "Description",
        value=DEFAULT_DESCRIPTION,
        key="description",
        label_visibility="collapsed",
        placeholder="old_field -> new_field",
        help="Describe the breaking change. A `from -> to` rename is "
             "highlighted once the request is created.",
    )

    if st.button(
        "Start change request",
        type="primary",
        use_container_width=True,
        disabled=not backend_up,
    ):
        st.session_state["create_error"] = None
        with st.spinner("Discovery agents running…"):
            try:
                created = client.create_change_request(description)
            except ApiError as exc:
                st.session_state["create_error"] = str(exc)
            else:
                st.session_state["change_id"] = created["id"]
        st.rerun()

    with st.expander("Load an existing change"):
        existing = st.text_input(
            "Change ID",
            key="load_id",
            placeholder="paste a change UUID",
            help="Press Enter to apply the value, then choose Load.",
        )
        # Validate on click rather than via `disabled=`: a disabled flag is
        # computed from the *previous* run, so the button would stay dead until
        # the text input triggered a rerun of its own.
        if st.button("Load", use_container_width=True):
            if existing.strip():
                st.session_state["change_id"] = existing.strip()
                st.rerun()
            else:
                st.warning("Enter a change ID to load.")

    st.divider()
    st.markdown("### Session")
    st.session_state["approved_by"] = st.text_input(
        "Approver", value=st.session_state["approved_by"]
    )
    st.session_state["polling"] = st.toggle(
        f"Live polling ({POLL_SECONDS:.0f}s)", value=st.session_state["polling"]
    )
    if st.button("Refresh now", use_container_width=True):
        st.rerun()

    if st.session_state["change_id"]:
        st.caption(f"change: {st.session_state['change_id']}")


# ---------------------------------------------------------------------------
# Guard rails — backend unavailable / nothing loaded
# ---------------------------------------------------------------------------

if st.session_state["create_error"]:
    st.error(f"Could not create change request — {st.session_state['create_error']}")

if not backend_up:
    st.error(
        f"Backend unavailable at {st.session_state['base_url']}. "
        "Start it with `uvicorn orchestrator.main:app --reload`. "
        "No cached or simulated data is shown."
    )
    st.stop()

change_id = st.session_state["change_id"]
if not change_id:
    st.info(
        "No change request loaded. Start one from the sidebar to watch the "
        "agents discover dependencies, migrate consumers and face the gate."
    )
    st.stop()


# ---------------------------------------------------------------------------
# Fetch every projection
# ---------------------------------------------------------------------------

with st.spinner("Loading change…"):
    snap = client.snapshot(change_id)

errors: dict[str, str] = snap["errors"]
change = snap["change"]

if change is None:
    st.error(
        f"Could not load change {change_id} — "
        f"{errors.get('change', 'unknown error')}"
    )
    st.stop()

evidence_payload = snap["evidence"] or {}
evidence = evidence_payload.get("evidence", [])
graph = snap["graph"]
gate = snap["gate"]
approvals = approvals_by_gate(snap["approvals"])

state = change.get("status", "")
is_working = state in WORKING_STATES

if errors:
    st.warning(
        "Some sections failed to load: "
        + "; ".join(f"{k} ({v})" for k, v in errors.items())
    )


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

render_change_header(change)
render_state_rail(change)

if state == "COORDINATE":
    st.info("Waiting for human coordinate approval before any code is modified.")
elif state == "GATE_DECISION":
    st.error("Blocked at the deterministic gate — legacy removal cannot be approved.")
elif state == "APPROVE":
    st.warning("Gate cleared. Waiting for human legacy-removal approval.")
elif state == "DONE":
    st.success("Change complete — legacy field removal approved.")
elif is_working:
    st.info("Agents are running…")

left, right = st.columns([1.05, 1], gap="medium")

with left:
    render_activity_feed(evidence, running=is_working)
    render_hidden_dependency_callout(hidden_dependencies(graph))
    render_evidence(evidence, errors.get("evidence"))

with right:
    render_graph(graph, errors.get("graph"))
    render_gate_panel(gate, errors.get("gate"))
    render_migration_progress(gate)
    if render_approvals(
        client, change, approvals, approved_by=st.session_state["approved_by"]
    ):
        st.rerun()


# ---------------------------------------------------------------------------
# Change Passport — only once the backend has recorded a gate decision
# ---------------------------------------------------------------------------

st.write("")
if gate and gate.get("decided"):
    render_passport(change, graph, gate, evidence, approvals)
else:
    st.markdown(
        '<div class="il-panel"><div class="il-panel-title">Change Passport</div>'
        '<div class="il-empty">Issued once the deterministic gate has been '
        "evaluated.</div></div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

if st.session_state["polling"] and is_working:
    time.sleep(POLL_SECONDS)
    st.rerun()
