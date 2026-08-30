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

The real agents in `agents/` are wired in and run. Which agents execute is
selected per change by the presence of a `change_spec` row, not by a global
flag: a change **with** a spec runs the real agents via
`orchestrator/real_workflow.py`; a change **without** one falls back to the stub
workflow in `agent_runner.py` while `STUB_MODE` is True. All ten runtime agents
are written, `contract-test` and `coexistence-rehearsal` included.

What is genuinely proved, per change kind:

| Kind | Verdict on the bundled fixtures | Why |
| --- | --- | --- |
| `field_rename` | VERIFIED | Provider patched, all four consumers migrated (including the SQL schema), coexistence rehearsed against a real running provider. |
| `api_contract_change` | VERIFIED | Same path as `field_rename`. |
| `transport_migration` | **NOT_PROVEN_SAFE** | Subscribers migrate correctly, but the provider side is not automatable — see below. |

### The transport migration does not reach VERIFIED, deliberately

`provider-patch` matches *field-shaped* symbols: class annotations, dict-literal
keys, assignments, OpenAPI properties. A webhook → pub/sub cut-over renames a
*function*, which matches none of those patterns. Synthesising a real pub/sub
implementation is beyond a deterministic agent.

The agent therefore reports `status="failed"` and the gate blocks, naming
`event-publisher:provider_patch` as unproved. This is the intended outcome. It
previously reported success while changing nothing, so the gate declared a
migration safe that had never happened — the exact failure mode this project
exists to prevent. Completing this path needs either a human-written provider
patch or an LLM-backed implementation agent.

### Non-Python consumers are discovered, and block honestly

Discovery is no longer Python-only. `polyglot-source-discovery` finds consumers
written in JavaScript/TypeScript, Java, Kotlin, Go, C#, Ruby and PHP by lexical
scanning: the quoted wire name (`"customer_id"` — annotations, struct tags,
string keys) is matched everywhere, and naming-convention variants
(`customerId`, `CustomerId`) are matched in the languages whose style renames
fields at the mapping layer. Convention matches are recorded as `hypothesis`
rather than `confirmed` — an inference a human can refute — but they still emit
a dependency edge, because the gate must know about a *probable* consumer.
Vendored and generated trees (`node_modules/`, `target/`, `dist/`, minified
bundles) are never scanned.

The consequence is deliberately asymmetric: a TypeScript consumer is
*discovered* and therefore *required*, but the built-in implementation agent
migrates only Python and SQL — so it reports failure for that component and the
gate blocks, naming it. Before this agent existed the same repository produced
VERIFIED with the TypeScript consumer silently unmigrated. Being told what
still needs a human (or an external agent, via `interlock.toml` /
external-change mode) is the honest verdict; the false VERIFIED was the bug.

### The invariant that holds all of this up

An implementation agent that changed no non-test source file **must not** report
success. `status` used to be set purely from "pytest passed", decoupled from
whether the agent had changed anything, which is what allowed a schema-only
component to be reported migrated while its `schema.sql` was untouched. See
`tests/implementation/test_no_silent_success.py`.

Correspondingly, the gate counts **work items** and never reads evidence, so any
agent whose result must affect the verdict has to write one. The coexistence
rehearsal is a required provider step (`gate.REHEARSAL_STEP_KIND`) for this
reason; it previously wrote evidence alone and could fail with no effect on the
verdict at all.

`docker-compose.yml` is retained as an optional demo path only — the rehearsal
drives a uvicorn subprocess instead, so it can run without a Docker daemon.
