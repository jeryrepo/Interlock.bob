# Interlock architecture

Interlock is a change-safety control plane for coordinated changes across
multiple repositories. The demonstration change renames `customer_id` to
`account_id` in `account-service`, but the workflow is structured around a
general sequence: discover dependencies, plan compatibility, modify providers
and consumers, verify the result, and require human approval before removal of
the legacy contract.

## System boundary

```text
Streamlit UI
    |
    | HTTP only
    v
FastAPI orchestrator
    |-- persisted state machine
    |-- validated agent execution
    |-- deterministic safety gate
    `-- evidence ledger (SQLite through ledger.py only)
             |
             v
      dependency graph (NetworkX)
```

The Streamlit application is a view over the FastAPI API. It does not open the
database, execute agents, or calculate a safety verdict. The backend remains
the authority for workflow state, evidence, approvals, and the gate decision.

## Workflow

```text
INTAKE -> DISCOVERY -> PLANNING -> COORDINATE -> MODIFY
       -> REHEARSE -> VERIFY -> GATE_DECISION -> APPROVE -> DONE
```

`COORDINATE` and `APPROVE` are human checkpoints. The first authorises code
modification after reviewing the migration plan. The second authorises legacy
field removal only after the backend has recorded a successful deterministic
gate decision.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| `orchestrator/main.py` | Stable HTTP API and approval enforcement |
| `orchestrator/state_machine.py` | Linear, persisted workflow transitions |
| `orchestrator/ledger.py` | The only SQLite read/write boundary |
| `orchestrator/gate.py` | Read-only deterministic gate and graph projection |
| `orchestrator/agent_runner.py` | Agent validation, retry, and phase sequencing |
| `agents/discovery/` | Repository, API, event, and schema discovery |
| `agents/planning/` | Compatibility strategy and migration ordering |
| `agents/implementation/` | Provider compatibility patch and consumer migration |
| `agents/verification/` | Contract tests, coexistence rehearsal, and evidence critic |
| `frontend/` | Human-readable presentation of backend facts |

## Evidence model

Agents return Pydantic-validated results to the orchestrator. The orchestrator
records evidence and dependency rows through `ledger.py`; agents never write
SQLite directly and never call one another. Evidence includes its subject,
claim type, source reference, confidence, and source revision when available.

The dependency graph is derived from ledger rows on demand rather than cached
as a second source of truth. The safety gate is plain Python with no LLM
involvement. A critic may flag evidence-quality risks, but it cannot override
the gate.

## API surface

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/change-requests` | Create a change and run to the first human gate |
| `GET` | `/change-requests/{id}` | Read current workflow state |
| `GET` | `/change-requests/{id}/evidence` | Read the evidence ledger |
| `GET` | `/change-requests/{id}/graph` | Read the derived dependency graph |
| `GET` | `/change-requests/{id}/gate` | Read gate status and consumer progress |
| `GET` | `/change-requests/{id}/approvals` | Read recorded approvals |
| `POST` | `/change-requests/{id}/approve` | Approve coordination or legacy removal |

## Safety and operational constraints

- A missing or failed proof must remain pending or failed; it must never be
  presented as successful evidence.
- Git SHAs and test output must come from real commands.
- Fixture repositories are independent test targets and are not collected by
  the root product suite.
- Secrets belong in local environment variables and must not be committed.
- SQLite is suitable for the proof of concept; a multi-instance production
  deployment would require a shared transactional database and concurrency
  controls.

The contribution invariants and ownership boundaries are defined in
[`AGENTS.md`](../AGENTS.md).
