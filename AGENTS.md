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
| `orchestrator/agent_runner.py` | Agent wrapper, retry, routing, stub workflow |
| `orchestrator/real_workflow.py` | The real-agent workflow, per change kind |
| `orchestrator/agent_registry.py` | Which agents run for which (kind, phase) |
| `orchestrator/adapters.py` | Agent return shapes -> orchestrator schemas |
| `interlock_cli/` | `interlock` CLI; exits non-zero on NOT_PROVEN_SAFE |
| `interlock_mcp/` | MCP server so Bob and other agents can call Interlock |
| `orchestrator/schemas/` | Shared Pydantic contracts |
| `agents/` | Discovery, planning, implementation, verification agents |
| `fixtures/` | Component tree for field/API changes |
| `fixtures_transport/` | Component tree for webhook -> pub/sub changes |
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

### Real agents vs stubs

Routing is per change, on whether it carries a structured `ChangeSpec`:

| Change | Path |
| --- | --- |
| has a `change_spec` row | `real_workflow.py` — the real agents |
| description only | the legacy stub workflow in `agent_runner.py` |

The Streamlit UI sends a spec by default (the **Run real agents** toggle in the
sidebar), so the panels show discovered dependencies and real commit SHAs. Turn
the toggle off to exercise the stub path. Until 2026-08-29 the UI sent only a
description, which meant every demo rendered seeded data while looking exactly
like a real run — `tests/frontend/test_spec.py` pins that shut.

`STUB_MODE` no longer means "stubs everywhere". It controls only whether the
stub fallback is available for description-only changes.

**Real runs never touch `fixtures/`.** The implementation agents rewrite files
and `git commit` inside the path they are given, so every real run operates on a
copy in `.interlock_work/<change_id>/`, git-initialised per component. Pointing
an agent at `fixtures/` directly would commit into this repository — that has
happened before. A test asserts the fixtures are unmutated; keep it passing.

### Change kinds

Three kinds, discriminated by `ChangeSpec.kind`. They exist because a developer
touching something critical in a microservice estate is usually touching more
than one of these at once:

| Kind | What moves | Components live in |
| --- | --- | --- |
| `field_rename` | a schema / model field | `fixtures/` |
| `api_contract_change` | a published API field or endpoint | `fixtures/` |
| `transport_migration` | webhook delivery -> pub/sub | `fixtures_transport/` |

Adding a kind means a registry entry in `agent_registry.py` plus, if its proof
condition differs, an entry in `gate._REQUIRED_STEP_KINDS`. It must **not** mean
new branching inside the gate (ADR-0002).

See the whole fabric at any time:

```bash
interlock agents
```

That prints, per kind, which agents run in which phase and which work item each
one proves. A step that no agent proves will hold every change of that kind at
`NOT_PROVEN_SAFE` forever — there is a test asserting that cannot happen.

### Step kinds

`work_item.step_kind` is what the gate counts. `provider_patch` for the provider;
`migrate` for a field or API consumer; `subscribe` **and** `webhook_quiet` for a
transport subscriber. The two-step transport requirement is the point: a
subscriber that moved to pub/sub but still sends webhook traffic is not safe,
because retiring the webhook would still break it.

Anything proved after MODIFY must be listed in
`state_machine._PROVED_AFTER_MODIFY`, or it sits at `pending` and stalls the
change.

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

## Architecture decisions

Irreversible decisions are recorded in [`docs/adr/`](docs/adr/README.md), one
file per decision, with the context that forced it and the cost it carries.

**Read the ADR index before changing architecture.** Do not contradict an
accepted ADR. If you believe one is wrong, write a new ADR that supersedes it and
say so in both files — never edit an accepted ADR to mean something different.

Decisions most likely to be re-litigated by accident:

- [ADR-0002](docs/adr/0002-deterministic-gate-stays-single-function.md) — the
  gate stays **one** function. Per-kind variation is data, not a dispatch table.
  This refactor looks like an improvement and is not.
- [ADR-0003](docs/adr/0003-change-kind-discriminator.md) — change kinds go
  through `ChangeSpec`. Do not add a fourth special case or reintroduce
  hardcoded field names.
- [ADR-0004](docs/adr/0004-streamlit-retained.md) — Streamlit stays until the
  pipeline runs real agents. The revisit trigger is written down.

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

`frontend/` is a **pure view over the API**. It builds the `ChangeSpec` payload
in `utils/spec.py` — shape only, since the backend owns validation — and must
not duplicate that validation or the two will drift. Pure helpers live in
`utils/` rather than in `streamlit_app.py`, because a Streamlit module executes
top to bottom on import and nothing defined inside it can be unit-tested.

It must not:

- open SQLite,
- run an LLM,
- implement orchestration,
- evaluate or re-derive the gate,
- hardcode component names,
- present a failure as a success.

It consumes these endpoints only:

```
POST /change-requests            (description, plus an optional spec)
GET  /change-requests/{id}
GET  /change-requests/{id}/spec
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

CLI — no server needed, exits non-zero when the gate is not satisfied:

```bash
interlock check --old customer_id --new account_id --provider account-service
```

Preview exactly what the PR bot will post:

```bash
interlock review --run --old customer_id --new account_id --provider account-service
```

The review body is rendered by `interlock_cli/review.py`, **not** by inline
JavaScript in the workflow. A formatter that only runs on a real pull request is
one you debug in production; this one is a pure function with unit tests.

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
- **Do not add `__init__.py` to a `tests/` subdirectory.** This is the sharpest
  trap in the repo, and it has already cost two rounds of debugging.

  `tests/` itself has no `__init__.py`. When a subdirectory *does* have one,
  pytest walks up only as far as `tests/` and names the module
  `verification.test_critic` rather than `tests.verification.test_critic` —
  which then needs `tests/` on `sys.path`. That happens to be true when the
  directory is run on its own and false in a full-suite run, so the breakage
  looks like a test-ordering problem and is not one.

  Measured, not assumed: adding `tests/verification/__init__.py` breaks
  collection of all three verification modules in a full run while
  `pytest tests/verification/` stays green. Adding `tests/__init__.py` is worse
  — five collection errors across four packages.

  `tests/discovery/`, `tests/planning/`, `tests/implementation/` and
  `tests/frontend/` do have one. They are the anomaly, not the pattern to copy;
  they survive only because pytest's rootdir insertion happens to cover them.
  `tests/orchestrator/` and `tests/verification/` have none, deliberately.

  The same mechanism caused four tests in
  `tests/implementation/test_consumer_migration.py` to fail in full-suite runs
  until 2026-08-29. They imported `from tests.implementation.conftest import ...`,
  which cannot resolve without `tests/__init__.py`. Fixed by using the
  `tmp_checkout_repo` / `tmp_fraud_repo` / `tmp_analytics_worker_repo` fixtures
  that `tests/implementation/conftest.py` already exposed. **Never import
  conftest directly** — pytest loads it specially, and importing it as a module
  can create a second, divergent instance. Use fixtures.
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
- [ ] tests pass — the suite is fully green, so any failure is yours
- [ ] no duplicate schemas
- [ ] no direct SQLite access from agents or frontend
- [ ] no agent-to-agent calls
- [ ] no hardcoded `analytics-worker` dependency
- [ ] no fake Git SHAs or test results
- [ ] no breaking API changes
- [ ] changes stay within the ownership area unless discussed
- [ ] no new `__init__.py` in a `tests/` subdirectory (see the testing note above)
- [ ] no agent pointed at `fixtures/` directly — real runs use a workspace copy
- [ ] gate policy still lives only in `gate.py`, as data (ADR-0002)
