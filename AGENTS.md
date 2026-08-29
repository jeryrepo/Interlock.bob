# AGENTS.md

Guidance for AI coding agents working in this repository.

## What Interlock is

Interlock is a **change-safety control plane**. Given a breaking change to a
shared field (the demo case is `customer_id -> account_id` on `account-service`),
it discovers every consumer — including ones absent from the published contract —
migrates them, verifies them, and refuses to authorise removal of the legacy
field until a *deterministic* gate says every required consumer is proven safe.

The product claim is "nothing ships until every consumer is proven safe." Every
rule below exists to keep that claim true.

## Architecture

```
POST /change-requests
        │
        ▼
   state machine  ──────────────┐
        │                       │
   agent phases            evidence ledger (SQLite)
        │                       │
        ▼                       ▼
  dependency_edge rows ──▶ NetworkX graph
        │
        ▼
  deterministic gate ──▶ VERIFIED | NOT_PROVEN_SAFE
        │
        ▼
   human approvals ──▶ DONE
```

| Path | Role |
| --- | --- |
| `orchestrator/main.py` | FastAPI app; the only HTTP surface |
| `orchestrator/ledger.py` | All SQLite reads/writes |
| `orchestrator/db/schema.sql` | Canonical schema |
| `orchestrator/state_machine.py` | `STATES` + strictly linear `TRANSITIONS` |
| `orchestrator/gate.py` | Deterministic gate + NetworkX graph derivation |
| `orchestrator/agent_runner.py` | Agent wrapper, retry, and phase workflow |
| `orchestrator/schemas/` | Shared Pydantic contracts |
| `agents/` | Discovery, planning, implementation, verification agents |
| `fixtures/` | Fixture repositories the agents operate on |
| `frontend/` | Streamlit UI (pure view layer) |
| `tests/` | pytest suite, mirroring the package layout |

### Workflow states

`INTAKE → DISCOVERY → PLANNING → COORDINATE → MODIFY → REHEARSE → VERIFY →
GATE_DECISION → APPROVE → DONE`

Transitions are linear and one-step; `state_machine.advance()` is the only way
to move. Two states wait on a human:

- `COORDINATE` → needs `POST /approve {"gate": "coordinate"}`
- `APPROVE` → needs `POST /approve {"gate": "legacy_removal"}`

`run_workflow()` resumes from the persisted state and **never** auto-approves a
human gate.

## Invariants — do not break these

1. **The gate is deterministic and lives in exactly one place.** `gate.evaluate_gate()`
   is pure, read-only Python with zero LLM involvement. No other component —
   critic agent, frontend, or otherwise — may compute, duplicate, cache, or
   override it.
2. **Only `ledger.py` touches SQLite.** Agents and the frontend never open the
   database. The frontend reads HTTP only.
3. **No agent-to-agent calls.** Agents return a validated Pydantic result to the
   orchestrator, which writes evidence. Coordination happens through the ledger.
4. **Never fabricate results.** No fake Git SHAs, no fake test results, no
   converting an API failure into a success. If something has not been proven,
   the correct output is an explicit "not recorded" / "pending" state.
5. **Canonical edge direction is provider → consumer.** A `dependency_edge` row
   reads `from_component = account-service`, `to_component = checkout`. The gate
   reads consumers off the `to_component` end.
6. **Nothing hardcodes `analytics-worker`.** It is the *discovered* undocumented
   dependency and must arrive from evidence. Hardcoding it anywhere — especially
   in the UI — destroys the demo's point. `gate.PROVIDER` is the one deliberate
   component constant.
7. **No breaking API changes.** New endpoints are additive; existing response
   shapes stay stable.

## Ownership boundaries

The project is split across five workstreams. Stay inside your area unless the
change is additive and you say so explicitly:

| Area | Scope |
| --- | --- |
| Orchestrator | `orchestrator/`, schema, state machine, gate, API |
| Discovery | `agents/discovery/`, `fixtures/` |
| Planning / implementation | `agents/planning/`, `agents/implementation/` |
| Verification | `agents/verification/`, `docker-compose.yml` |
| Frontend | `frontend/` |

## Frontend rules

`frontend/` is a **pure view over the API**. It must not:

- open SQLite,
- run an LLM,
- implement orchestration,
- evaluate or re-derive the gate,
- hardcode component names,
- present a failure as a success.

It consumes these endpoints only:

```
POST /change-requests
GET  /change-requests/{id}
GET  /change-requests/{id}/evidence
GET  /change-requests/{id}/graph
GET  /change-requests/{id}/gate
GET  /change-requests/{id}/approvals
POST /change-requests/{id}/approve
```

`/gate` and `/approvals` are read-only projections of ledger state that the
orchestrator already computed — they exist so the UI never has to re-implement
gate logic. `/gate` returns `decided: false` with a live preview before the
orchestrator has written a `gate_decision` row; the UI must render that as
`PENDING`, never as a verdict.

Polling every 1–2s is fine. Do not introduce WebSockets.

### Streamlit gotchas worth remembering

- `st.text_input(..., value=X)` **without a `key`** re-applies `value` on every
  rerun and silently discards user input. Always pass `key=`.
- A widget's `disabled=` flag is computed from the *previous* run, so a button
  gated on a sibling text input stays dead until that input triggers its own
  rerun. Validate on click instead.
- pyvis pulls Bootstrap from a CDN and its `body` rule repaints the frame white.
  `components/graph.py` strips external `<link>`/`<script>` tags so the graph is
  self-contained and offline-safe. Keep it that way.
- `st.iframe(src, *, width, height, tab_index)` has **no** `scrolling` argument,
  unlike the deprecated `st.components.v1.html` it replaces.
- Components must style themselves with the `--il-*` CSS variables from
  `utils/theme.py`, never literal hex — that is what makes light and dark work
  from one stylesheet. `components/graph.py` is the sole exception, because
  pyvis renders into an iframe those variables do not reach.
- Streamlit exposes no `data-theme` DOM hook, so `theme.py` emits three layered
  `:root` blocks: dark base, a `prefers-color-scheme` override, then the
  `st.context.theme.type` result last. That last layer is authoritative but one
  rerun stale, which is why a live theme switch settles on the next interaction.

## Running things

Backend:

```bash
uvicorn orchestrator.main:app --reload
```

Frontend (from the repository root, so `.streamlit/config.toml` is picked up):

```bash
streamlit run frontend/streamlit_app.py
```

The UI reads `ORCHESTRATOR_API_URL` (default `http://localhost:8000`); the
backend reads `INTERLOCK_DB_PATH` (default `interlock.db`).

## Tests

```bash
python -m pytest -q
```

- `pytest.ini` sets `--basetemp=.pytest_tmp` to avoid Windows temp permission
  errors. Both that directory and `interlock.db` are gitignored.
- **Known issue:** four tests in `tests/implementation/test_consumer_migration.py`
  fail in a full-suite run but pass when that file runs alone — a test-ordering
  problem in that module, not a product bug. Do not "fix" it by weakening
  assertions.
- **Test hygiene:** the FastAPI lifespan assigns `app.state.conn` from the
  configured on-disk database, so an in-memory override installed *before*
  `TestClient(app)` is discarded and the test then mutates `interlock.db`.
  Install the override *inside* the context manager:

  ```python
  with TestClient(app) as c:
      app.state.conn.close()
      app.state.conn = ledger.init_db(":memory:")
      yield c
  ```

  `tests/frontend/` and `tests/orchestrator/test_api_projections.py` follow this
  pattern; some older test modules do not.

## Conventions

- Python 3.11+, `from __future__ import annotations`, type hints on public
  functions.
- Module docstrings explain *why* a component exists and what it must not do.
- Pydantic models are the contract between agents and the orchestrator.
- Do not name a helper `test_*` outside a test file — pytest collects it as a
  test and errors on the missing fixture.
- Logical, scoped commits.

## Before opening a PR

- [ ] project still starts (backend and UI)
- [ ] tests pass (minus the known pre-existing failures above)
- [ ] no duplicate schemas
- [ ] no direct SQLite access from agents or frontend
- [ ] no agent-to-agent calls
- [ ] no hardcoded `analytics-worker` dependency
- [ ] no fake Git SHAs or test results
- [ ] no breaking API changes
- [ ] changes stay within the ownership area unless discussed
