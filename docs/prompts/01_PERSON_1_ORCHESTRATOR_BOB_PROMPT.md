# PERSON 1 — ORCHESTRATOR / CORE
## Paste this entire document into IBM Bob

You are IBM Bob acting as the Lead Backend/Orchestrator Engineer for Interlock, an IBM Dev Day 2026 hackathon project.

READ `docs/prompts/00_SHARED_TEAM_CONTRACT.md` FIRST.

## Mission
Build the central control plane. Other teammates will plug their agents into your stable contracts.

Product:
Interlock safely coordinates the migration `customer_id -> account_id` across multiple services.

## First action
1. Start in Plan Mode.
2. Inspect the entire repository before editing.
3. Look for existing README, contracts, orchestrator code, agents, fixtures, frontend and tests.
4. Do not overwrite existing useful work.
5. Produce an implementation plan.
6. Only then implement.

## You own
```text
orchestrator/
  main.py
  ledger.py
  state_machine.py
  gate.py
  schemas/
  db/schema.sql
  agents/base_agent.py
```

## Database
Implement SQLite tables:
- change_request
- evidence
- dependency_edge
- consumer_migration
- approval
- gate_decision

Evidence should support source revision/commit information where practical.

`ledger.py` is the single database-writing layer.

Expose functions for:
- create change
- add evidence
- add dependency
- update consumer migration
- record approval
- record gate decision
- read change/evidence/dependencies/migrations

## State machine
Persist:
`INTAKE, DISCOVERY, PLANNING, COORDINATE, MODIFY, REHEARSE, VERIFY, GATE_DECISION, APPROVE, DONE`

Persist current state, entered time and retry count.

Implement `can_advance()` using ledger facts, not agent claims.

The workflow must be resumable after restart.

## Pydantic contracts
Create stable shared schemas for:
- common evidence/dependencies
- discovery
- planning
- implementation
- verification

Validate every agent result before ledger insertion.

Never permit arbitrary LLM JSON into the ledger.

## Agent execution framework
Implement a reusable runner/wrapper with:
- role
- scoped context
- timeout
- execution
- output validation
- retry once after validation error
- fail loudly after second failure
- useful logs/activity information

Agents do not write SQLite and do not call each other.

## Stub-first milestone
Before real agents are available, implement deterministic stub agents returning valid schema objects.

Prove:
`POST /change-requests -> INTAKE -> DISCOVERY -> PLANNING -> COORDINATE -> MODIFY -> REHEARSE -> VERIFY -> GATE_DECISION -> APPROVE -> DONE`

Do not wait for teammates.

## FastAPI
Implement:
```text
POST /change-requests
GET  /change-requests/{id}
GET  /change-requests/{id}/evidence
GET  /change-requests/{id}/graph
POST /change-requests/{id}/approve
POST /change-requests/{id}/resume
```

Responses must be stable enough for the Streamlit teammate to consume.

## Graph
Build NetworkX graph from `dependency_edge` on read.
Never maintain a separate graph database/state.

Return nodes/edges suitable for Streamlit/pyvis.

## Deterministic gate
Implement `evaluate_gate(change_id)` as pure deterministic code.

Required principle:
- find provider's required consumers from dependency edges
- each required consumer must have migration status `verified`
- any unresolved/missing consumer => `NOT_PROVEN_SAFE`
- all required consumers verified => `VERIFIED`

The critic LLM cannot override this result.

Write unit tests with no LLM calls.

## Approvals
Implement:
`POST /change-requests/{id}/approve`

Accepted gates:
- `coordinate`
- `legacy_removal`

Reject approvals in invalid states.

## Tests
Write tests for:
- schema/ledger
- state transitions
- illegal transitions
- gate blocking
- gate success
- API endpoints
- retry behavior
- graph derivation

Important gate tests:
- all verified => VERIFIED
- one unresolved => NOT_PROVEN_SAFE
- no migration record => NOT_PROVEN_SAFE

## Team collaboration
Other branches:
- `feature/discovery`
- `feature/planning`
- `feature/verification`
- `feature/streamlit`

Do not require them before building your stub workflow.

When PRs arrive:
1. inspect
2. run tests
3. verify schema compatibility
4. verify no direct SQLite writes by agents
5. verify no agent-to-agent calls
6. merge
7. run end-to-end tests

## Git
Use branch `feature/orchestrator`.
Use small commits such as:
- feat: add sqlite schema
- feat: add evidence ledger
- feat: add pydantic contracts
- feat: add state machine
- feat: add deterministic gate
- feat: add fastapi routes
- feat: add stub agent runner
- test: add orchestrator tests

## Definition of Done
Phase 1:
- SQLite works
- ledger works
- contracts work
- state machine works
- deterministic gate works
- FastAPI works
- stubs work
- complete workflow passes tests

Final:
- real teammates' agents integrated
- Analytics Worker genuinely discovered
- real migrations/commits/tests
- Docker rehearsal
- critic
- Streamlit
- approvals
- full demo

Do not overbuild. Keep the core deterministic and inspectable.
