"""
orchestrator/main.py
=====================
FastAPI application for the Interlock orchestrator.

Endpoints:
  POST /change-requests
  GET  /change-requests/{id}
  GET  /change-requests/{id}/evidence
  GET  /change-requests/{id}/graph
  POST /change-requests/{id}/approve

All responses use stable Pydantic response models.
Start with: uvicorn orchestrator.main:app --reload
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.agent_runner import run_workflow
from orchestrator.schemas import ChangeSpec
from orchestrator.settings import load as load_settings
from interlock_mcp.http import build as build_mcp
from orchestrator.gate import build_graph, evaluate_gate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH: str = os.environ.get("INTERLOCK_DB_PATH", "interlock.db")

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ChangeResponse(BaseModel):
    id: str
    description: str
    status: str
    entered_at: str
    retry_count: int
    created_at: str
    updated_at: str


class EvidenceItem(BaseModel):
    id: str
    change_id: str
    claim_type: str
    subject: str
    content: dict
    source_ref: str
    confidence: str
    source_revision: str | None
    created_at: str


class EvidenceListResponse(BaseModel):
    change_id: str
    evidence: list[EvidenceItem]


class GraphNode(BaseModel):
    id: str
    label: str

class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str
    to: str
    edge_type: str
    reason: str



class GraphResponse(BaseModel):
    change_id: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class ApprovalResponse(BaseModel):
    id: str
    change_id: str
    gate: str
    approved_by: str
    approved_at: str
    new_status: str


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateChangeRequest(BaseModel):
    description: str
    spec: ChangeSpec | None = None
    """
    Optional structured change spec.  Additive: existing clients that send only
    a description keep working exactly as before (AGENTS.md invariant 7).

    When present the orchestrator runs the real agents for that change kind;
    when absent it runs the stub workflow.
    """


class ApproveRequest(BaseModel):
    gate: str
    approved_by: str = "human"


# ---------------------------------------------------------------------------
# App + lifespan
# ---------------------------------------------------------------------------

_ALLOWED_GATES = {"coordinate", "legacy_removal"}


# Read once, at import, and shared by the app and the MCP sub-app. Two separate
# load() calls would let the two disagree about whether MCP is enabled.
_SETTINGS = load_settings()
_MCP_APP, _MCP_LIFESPAN_SRC = build_mcp(_SETTINGS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = ledger.init_db(DB_PATH)
    app.state.settings = _SETTINGS
    # Observed, not inferred: /health reports what was actually mounted.
    app.state.mcp_mounted = _MCP_APP is not None

    if _MCP_LIFESPAN_SRC is not None:
        # Starlette does not propagate lifespan into mounted sub-apps, and the
        # MCP session manager refuses to start twice, so the parent drives it
        # exactly once here. Without this every /mcp call fails with
        # "Task group is not initialized".
        async with _MCP_LIFESPAN_SRC.router.lifespan_context(_MCP_LIFESPAN_SRC):
            yield
    else:
        yield

    app.state.conn.close()


app = FastAPI(
    title="Interlock Orchestrator",
    description="Change-safety control plane for safe cross-service migrations.",
    version="0.1.0",
    lifespan=lifespan,
)

# The frontend is a separate origin (Streamlit on :8501, and any future browser
# client), so the API must permit cross-origin reads.  Every endpoint is
# read-only or human-gated and there is no auth or cookie to protect, which is
# what makes a permissive policy acceptable here; revisit if auth is added.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)



def _get_conn():
    return app.state.conn


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/change-requests", response_model=ChangeResponse, status_code=201)
def create_change_request(body: CreateChangeRequest):
    """
    Create a new change request and run agent phases up to the first human gate.

    The workflow stops at COORDINATE awaiting POST /approve {gate: "coordinate"}.
    """
    conn = _get_conn()
    change_id = str(uuid.uuid4())
    ledger.create_change(conn, change_id, body.description)
    if body.spec is not None:
        ledger.set_change_spec(
            conn, change_id, body.spec.kind, body.spec.model_dump()
        )
    run_workflow(conn, change_id)
    row = ledger.get_change(conn, change_id)
    return ChangeResponse(**row)


@app.get("/change-requests/{change_id}", response_model=ChangeResponse)
def get_change_request(change_id: str):
    """Return the current state of a change request."""
    conn = _get_conn()
    row = ledger.get_change(conn, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Change '{change_id}' not found.")
    return ChangeResponse(**row)


@app.get("/change-requests/{change_id}/evidence", response_model=EvidenceListResponse)
def get_evidence(change_id: str):
    """Return all evidence rows for a change request."""
    conn = _get_conn()
    if ledger.get_change(conn, change_id) is None:
        raise HTTPException(status_code=404, detail=f"Change '{change_id}' not found.")
    rows = ledger.get_evidence(conn, change_id)
    items = [EvidenceItem(**r) for r in rows]
    return EvidenceListResponse(change_id=change_id, evidence=items)


@app.get("/change-requests/{change_id}/graph", response_model=GraphResponse)
def get_graph(change_id: str):
    """
    Return the dependency graph for a change request.
    Derived fresh from dependency_edge rows on every call.
    """
    conn = _get_conn()
    if ledger.get_change(conn, change_id) is None:
        raise HTTPException(status_code=404, detail=f"Change '{change_id}' not found.")
    graph = build_graph(conn, change_id)
    return GraphResponse(
        change_id=change_id,
        nodes=graph["nodes"],
        edges=graph["edges"],
    )


@app.post("/change-requests/{change_id}/approve", response_model=ApprovalResponse)
def approve_change_request(change_id: str, body: ApproveRequest):
    """
    Record a human approval at a named gate and advance the state machine.

    coordinate gate (state must be COORDINATE):
      - Records approval, advances to MODIFY, then resumes agent workflow
        through MODIFY/REHEARSE/VERIFY/GATE_DECISION.
      - If gate is VERIFIED, automatically advances to APPROVE and stops.
      - If gate is NOT_PROVEN_SAFE, stops at GATE_DECISION.

    legacy_removal gate (state must be APPROVE):
      - Enforces that evaluate_gate() returns VERIFIED before accepting.
      - Records approval and advances to DONE.

    Rejects if the change is not in the correct state for the requested gate,
    or if the safety gate has not been cleared for legacy_removal.
    """
    conn = _get_conn()
    row = ledger.get_change(conn, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Change '{change_id}' not found.")

    gate = body.gate
    if gate not in _ALLOWED_GATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown gate '{gate}'. Allowed: {sorted(_ALLOWED_GATES)}.",
        )

    current_state = row["status"]

    # Validate that the change is in the correct state for this gate.
    expected_states = {
        "coordinate": "COORDINATE",
        "legacy_removal": "APPROVE",
    }
    expected = expected_states[gate]
    if current_state != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Gate '{gate}' requires state '{expected}', "
                f"but change is in '{current_state}'."
            ),
        )

    # For legacy_removal: enforce deterministic gate check before accepting.
    # This prevents approval when consumers are not yet fully verified.
    if gate == "legacy_removal":
        decision = evaluate_gate(conn, change_id)
        if decision.result != "VERIFIED":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Safety gate is NOT_PROVEN_SAFE: {decision.reason}"
                ),
            )

    approval = ledger.record_approval(conn, change_id, gate, body.approved_by)

    # Advance past the gate.
    try:
        new_state = sm.advance(conn, change_id)
    except sm.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # After coordinate approval: resume agent workflow from MODIFY.
    if gate == "coordinate":
        run_workflow(conn, change_id)
        new_state = sm.get_state(conn, change_id)

    return ApprovalResponse(
        **approval,
        new_status=new_state,
    )


# ---------------------------------------------------------------------------
# Read-only projections
# ---------------------------------------------------------------------------
# These endpoints expose ledger state that is already computed by the
# orchestrator so that clients (e.g. the Streamlit UI) never need to
# re-implement gate logic or read SQLite directly.  They are additive and
# side-effect free.


class ConsumerMigrationItem(BaseModel):
    consumer: str
    status: str
    updated_at: str


class GateStatusResponse(BaseModel):
    change_id: str
    state: str
    decided: bool
    result: str
    reason: str
    decided_at: str | None
    required_consumers: list[str]
    unresolved: list[str]
    consumers: list[ConsumerMigrationItem]


class ApprovalItem(BaseModel):
    id: str
    change_id: str
    gate: str
    approved_by: str
    approved_at: str


class ApprovalListResponse(BaseModel):
    change_id: str
    approvals: list[ApprovalItem]


@app.get("/change-requests/{change_id}/gate", response_model=GateStatusResponse)
def get_gate_status(change_id: str):
    """
    Return the deterministic safety gate status for a change request.

    If the orchestrator has already written a gate_decision row, that recorded
    decision is returned verbatim and ``decided`` is True.  Before the workflow
    reaches GATE_DECISION no row exists yet, so a live read-only
    ``evaluate_gate()`` preview is returned with ``decided`` False — this lets a
    UI show which consumers are still outstanding without duplicating gate
    logic, while never presenting a preview as a final decision.
    """
    conn = _get_conn()
    row = ledger.get_change(conn, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Change '{change_id}' not found.")

    live = evaluate_gate(conn, change_id)
    recorded = ledger.get_latest_gate_decision(conn, change_id)

    migrations = [
        ConsumerMigrationItem(
            consumer=m["consumer"],
            status=m["status"],
            updated_at=m["updated_at"],
        )
        for m in ledger.get_consumer_migrations(conn, change_id)
    ]

    if recorded is not None:
        result = recorded["result"]
        reason = recorded["reason"]
        decided_at = recorded["decided_at"]
    else:
        result = live.result
        reason = live.reason
        decided_at = None

    return GateStatusResponse(
        change_id=change_id,
        state=row["status"],
        decided=recorded is not None,
        result=result,
        reason=reason,
        decided_at=decided_at,
        required_consumers=live.required_consumers,
        unresolved=live.unresolved,
        consumers=migrations,
    )


@app.get("/change-requests/{change_id}/approvals", response_model=ApprovalListResponse)
def get_approvals(change_id: str):
    """Return all human approvals recorded for a change request."""
    conn = _get_conn()
    if ledger.get_change(conn, change_id) is None:
        raise HTTPException(status_code=404, detail=f"Change '{change_id}' not found.")
    rows = ledger.get_approvals(conn, change_id)
    return ApprovalListResponse(
        change_id=change_id,
        approvals=[ApprovalItem(**r) for r in rows],
    )


class ChangeSpecResponse(BaseModel):
    change_id: str
    kind: str | None
    spec: dict | None


@app.get("/change-requests/{change_id}/spec", response_model=ChangeSpecResponse)
def get_change_spec(change_id: str):
    """
    Read-only projection of the structured change spec.

    Additive endpoint (invariant 7): ChangeResponse is unchanged, so existing
    clients are unaffected.  `kind` and `spec` are null for a legacy
    description-only change, which is how a client can tell the two apart.
    """
    conn = _get_conn()
    if ledger.get_change(conn, change_id) is None:
        raise HTTPException(status_code=404, detail="change_request not found")
    row = ledger.get_change_spec(conn, change_id)
    if row is None:
        return ChangeSpecResponse(change_id=change_id, kind=None, spec=None)
    return ChangeSpecResponse(
        change_id=change_id, kind=row["kind"], spec=row["spec"]
    )


# ---------------------------------------------------------------------------
# watsonx Orchestrate external-agent surface (optional, additive)
# ---------------------------------------------------------------------------
# Mounted unconditionally so the route always exists and can explain itself;
# it returns 503 until INTERLOCK_EXTERNAL_AGENT_KEY is set, rather than 404,
# which makes "not configured" distinguishable from "wrong URL".
from orchestrator.external_agent import router as _external_agent_router  # noqa: E402

app.include_router(_external_agent_router)


class HealthResponse(BaseModel):
    status: str
    integrations: dict


@app.get("/health", response_model=HealthResponse)
def health():
    """
    Liveness plus a report of which optional integrations are actually wired up.

    Every IBM feature is optional, so "is watsonx connected?" has to be
    answerable without reading logs or spending model credits.
    """
    from orchestrator import watsonx as _watsonx

    settings = app.state.settings
    return HealthResponse(
        status="ok",
        integrations={
            "watsonx_ai_narration": _watsonx.health(settings.watsonx),
            "watsonx_orchestrate": {
                "configured": settings.orchestrate.configured,
                "external_agent_endpoint": (
                    "enabled" if settings.orchestrate.external_agent_enabled
                    else "disabled (set INTERLOCK_EXTERNAL_AGENT_KEY)"
                ),
                "mcp_http": (
                    "mounted at POST /mcp"
                    if getattr(app.state, "mcp_mounted", False)
                    else "not mounted (set INTERLOCK_EXTERNAL_AGENT_KEY)"
                ),
                "mcp_stdio": "always available via interlock_mcp.server",
            },
        },
    )


# ---------------------------------------------------------------------------
# MCP over HTTP, for the watsonx Orchestrate toolkit (optional, additive)
# ---------------------------------------------------------------------------
# LAST on purpose. Mounting at "" with the full path inside the sub-app makes
# `POST /mcp` match exactly rather than 307-redirecting, but a "" mount matches
# every path — so it has to be registered after every real route, and its
# middleware 404s anything that is not /mcp.
if _MCP_APP is not None:
    app.mount("", _MCP_APP)
