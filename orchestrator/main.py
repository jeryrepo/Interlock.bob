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
Start with: uvicorn orchestrator.main:app
   Or with reload: uvicorn orchestrator.main:app --reload --reload-dir orchestrator --reload-dir agents
   Or: python run_backend.py
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

import orchestrator.ledger as ledger
import orchestrator.state_machine as sm
from orchestrator.agent_runner import run_workflow
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


class ApproveRequest(BaseModel):
    gate: str
    approved_by: str = "human"


# ---------------------------------------------------------------------------
# App + lifespan
# ---------------------------------------------------------------------------

_ALLOWED_GATES = {"coordinate", "legacy_removal"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = ledger.init_db(DB_PATH)
    yield
    app.state.conn.close()


app = FastAPI(
    title="Interlock Orchestrator",
    description="Change-safety control plane for safe cross-service migrations.",
    version="0.1.0",
    lifespan=lifespan,
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
