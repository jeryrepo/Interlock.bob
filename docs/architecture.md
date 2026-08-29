# Interlock architecture

Interlock is a **change-safety control plane**. Given a breaking change to a
shared field, it discovers every consumer — including ones absent from the
published contract — migrates them, verifies them, and refuses to authorise
removal of the legacy field until a deterministic gate says every required
consumer is proven safe.

The product claim is *"nothing ships until every consumer is proven safe."*
Every design choice below exists to keep that claim true.

---

## The shape

```
                POST /change-requests
                         │
                         ▼
                  state machine                 ┌──────────────────────┐
        INTAKE → DISCOVERY → PLANNING →         │  evidence ledger     │
        COORDINATE → MODIFY → REHEARSE →  ────▶ │  (SQLite, append-    │
        VERIFY → GATE_DECISION → APPROVE        │   only in practice)  │
        → DONE                                  └──────────┬───────────┘
                         │                                 │
                         ▼                                 ▼
                   agent phases                   dependency_edge rows
                         │                                 │
                         │                                 ▼
                         │                          NetworkX DiGraph
                         │                        (derived every read)
                         ▼                                 │
                 deterministic gate  ◀────────────────────-┘
                         │
              VERIFIED | NOT_PROVEN_SAFE
                         │
                         ▼
                  human approvals ──▶ DONE
```

| Path | Role |
| --- | --- |
| `orchestrator/main.py` | FastAPI app; the only HTTP surface |
| `orchestrator/ledger.py` | All SQLite reads/writes |
| `orchestrator/db/schema.sql` | Canonical schema (6 tables) |
| `orchestrator/state_machine.py` | `STATES` + strictly linear `TRANSITIONS` |
| `orchestrator/gate.py` | Deterministic gate + graph derivation |
| `orchestrator/agent_runner.py` | Agent wrapper, retry, phase workflow |
| `orchestrator/schemas/` | Shared Pydantic contracts |
| `agents/` | Discovery, planning, implementation, verification agents |
| `fixtures/` | Fixture repositories the agents operate on |
| `frontend/` | Streamlit UI (pure view over HTTP) |

---

## Why it is built this way

### The gate is deterministic, and that is the whole point

`gate.evaluate_gate()` is pure, read-only Python with **zero LLM involvement**.
It asks one question: does every component that depends on the provider have a
migration record in status `verified`? Any consumer missing a record, or in any
other status, yields `NOT_PROVEN_SAFE`.

This is deliberately boring. An LLM can be persuaded; a topological fact cannot.
The critic agent inspects *evidence quality* and emits `risk` evidence, but it
cannot compute, cache, or override the verdict — and the API independently
re-evaluates the gate when a human tries to approve legacy removal, returning
`409` if it is not `VERIFIED`. A human cannot click past it either.

Per-kind variation in the proof condition is expressed as **data the one gate
function interprets**, never as a dispatch table. See
[ADR-0002](adr/0002-deterministic-gate-stays-single-function.md).

### Evidence is the substrate, not a log

Agents never talk to each other and never write the database. They return
validated Pydantic results; the orchestrator writes evidence rows. Coordination
happens entirely through the ledger. This is what makes the workflow resumable —
`run_workflow()` reads the persisted state and continues — and what makes every
claim in the UI traceable to a `source_ref` and, where available, a real Git SHA.

`Evidence.claim_type` is one of `dependency | migration_status | test_result |
risk`. `Dependency.edge_type` is one of `api | event | db | undocumented`. Those
two vocabularies carry most of the system's meaning.

### The graph is derived, never stored

`build_graph()` rebuilds a NetworkX `DiGraph` from `dependency_edge` rows on
every call. There is no second source of truth to drift. Canonical edge direction
is **provider → consumer**, so consumers are read off the `to_component` end.

### The undocumented consumer is discovered, not declared

The demo's point is `analytics-worker`: a service that reads the field straight
out of an event payload, with no API contract, no README reference, and no
config linking it to the provider. It must arrive from AST inspection of real
source. Hardcoding it anywhere destroys the demonstration, which is why
`AGENTS.md` makes that an invariant.

### Two human gates, and only two

`COORDINATE` waits for approval of the migration plan before any code is touched.
`APPROVE` waits for approval of legacy removal after the gate has passed.
`run_workflow()` never auto-approves either.

---

## Current state — read this before trusting a demo

**`orchestrator/agent_runner.py` sets `STUB_MODE = True`.** The real agents in
`agents/` are implemented and independently tested, but none of them is wired
into the pipeline — nothing in `orchestrator/` imports `agents/`. `run_workflow()`
executes hardcoded stub functions that return canned demo data, including
placeholder commit SHAs.

Everything above describes the architecture accurately. What a running instance
currently *shows* is seeded, not discovered. Two of the ten runtime agents —
`contract-test` and `coexistence-rehearsal` — are unwritten, and `docker-compose.yml`
is empty.

Closing that gap is Phase 1 of the current plan: an adapter layer plus an agent
registry, then deleting the stubs outright rather than leaving them switchable.
