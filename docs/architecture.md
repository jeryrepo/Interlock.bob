# Interlock architecture

Interlock is a change-safety control plane. The FastAPI orchestrator is the only
HTTP and persistence boundary; agents inspect or modify isolated repositories
and return validated Pydantic results. Only `orchestrator/ledger.py` reads or
writes SQLite.

```text
Streamlit UI -> FastAPI -> state machine -> agent runner -> isolated workspaces
                         |                         |
                         +-> evidence ledger <----+
                                  |
                         provider -> consumer graph
                                  |
                         deterministic safety gate
```

## Workflow

`INTAKE → DISCOVERY → PLANNING → COORDINATE → MODIFY → REHEARSE → VERIFY →
GATE_DECISION → APPROVE → DONE`

`COORDINATE` and `APPROVE` require human approvals. Coordinate approval returns
from HTTP at `MODIFY` and schedules long-running work in the background. A real
failure is recorded as `workflow-error`; `POST /change-requests/{id}/resume`
retries work stopped in `MODIFY` or `REHEARSE`.

## Dependency and gate contract

Every edge is directed from the provider to its consumer. `edge_type` records
the mechanism (`api`, `event`, or `db`) and `documentation_status` independently
records whether the dependency was documented. Thus the demo's hidden edge is:

```text
account-service -> analytics-worker
edge_type=event, documentation_status=undocumented
```

The deterministic gate derives every direct and transitive consumer reachable
from `account-service`. It returns `VERIFIED` only when each one has a ledger
migration row with status `verified`.

## Mutation and verification boundaries

Discovery reads canonical `fixtures/`. After coordinate approval, the runner
copies each fixture into a per-change temporary workspace and initializes each
copy as an independent Git repository. Provider patches, consumer migrations,
commit SHAs, contract tests, and Docker Compose all operate on that workspace.
The checked-out source fixtures are never workflow output.

The coexistence rehearsal must produce a confirmed zero exit code. Missing
Docker, timeouts, and non-zero exits remain failed evidence and block transition
from `REHEARSE`. No agent, critic, frontend component, or human approval can
override the gate.

## HTTP surface

The frontend consumes only the change, evidence, graph, gate, approval, approve,
and resume endpoints listed in the root README. It does not import orchestrator
code, open SQLite, call agents, or calculate gate results.
