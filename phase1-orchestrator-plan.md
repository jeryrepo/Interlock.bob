# Phase 1 — Orchestrator Core Implementation Plan

## Overview

Build the central control plane for Interlock so that all other teammates can immediately plug their agents into stable contracts.

**Goal:** A runnable FastAPI application backed by SQLite that can accept a change request and drive it from `INTAKE` through every state to `DONE` using deterministic stub agents — with no real agents required.

**Scope:** Everything under `orchestrator/` plus the test files in `tests/orchestrator/`.

**Non-goals (Phase 1):** Real agents, Docker, NetworkX graph export beyond the basic list, auth.

**Constraints from the contract:**
- Agents never write SQLite.
- Agents never call other agents.
- The safety gate is deterministic Python only.
- The graph is derived from `dependency_edge` rows on every read — no second graph state.
- No silent failures: retry once on validation error, then raise.

---

## Sub-Tasks

---

### Sub-Task 1 — `orchestrator/db/schema.sql`

**Intent:** Define the canonical SQLite schema. This is the single source of truth for the database shape. All other layers must agree with it.

**Expected Outcomes:**
- Six tables created with correct columns, constraints, and foreign keys.
- Schema can be applied to a fresh SQLite file with no errors.
- `source_revision` column exists on `evidence` to satisfy the contract requirement.

**Todo List:**
1. Write `CREATE TABLE IF NOT EXISTS change_request` with columns: `id` (TEXT PK), `description`, `status` (state-machine state), `created_at`, `updated_at`.
2. Write `CREATE TABLE IF NOT EXISTS evidence` with columns: `id` (TEXT PK), `change_id` (FK → change_request), `claim_type`, `subject`, `content` (JSON text), `source_ref`, `confidence`, `source_revision`, `created_at`.
3. Write `CREATE TABLE IF NOT EXISTS dependency_edge` with columns: `id` (TEXT PK), `change_id` (FK), `from_component`, `to_component`, `edge_type`, `reason`, `created_at`.
4. Write `CREATE TABLE IF NOT EXISTS consumer_migration` with columns: `id` (TEXT PK), `change_id` (FK), `consumer`, `status` (pending/in_progress/verified/failed), `updated_at`.
5. Write `CREATE TABLE IF NOT EXISTS approval` with columns: `id` (TEXT PK), `change_id` (FK), `gate` (coordinate/legacy_removal), `approved_by`, `approved_at`.
6. Write `CREATE TABLE IF NOT EXISTS gate_decision` with columns: `id` (TEXT PK), `change_id` (FK), `result` (VERIFIED/NOT_PROVEN_SAFE), `reason`, `decided_at`.
7. Add indexes on `change_id` columns for fast lookups.

**Relevant Context:**
- File: `orchestrator/db/schema.sql` (currently empty)
- Contract tables: `00_SHARED_TEAM_CONTRACT.md` → Database section
- Prompt: `01_PERSON_1_ORCHESTRATOR_BOB_PROMPT.md` → "Evidence should support source revision/commit information"

**Status:** [x] done

---

### Sub-Task 2 — `orchestrator/ledger.py`

**Intent:** Provide the only path through which any code writes to SQLite. This layer owns connection management, schema initialisation, and every write/read function. Agents are forbidden from importing this module.

**Expected Outcomes:**
- `init_db(db_path)` creates tables from `schema.sql` and returns a connection.
- All write functions accept typed arguments, not raw dicts, before inserting.
- All read functions return plain dicts or lists of dicts (not sqlite3.Row objects directly).
- Functions are importable and testable without FastAPI.

**Todo List:**
1. Implement `init_db(db_path: str) -> sqlite3.Connection` — reads `schema.sql`, executes it, enables WAL mode and foreign keys.
2. Implement `create_change(conn, id, description) -> dict`.
3. Implement `update_change_status(conn, change_id, status)`.
4. Implement `add_evidence(conn, change_id, claim_type, subject, content, source_ref, confidence, source_revision=None) -> dict`.
5. Implement `add_dependency(conn, change_id, from_component, to_component, edge_type, reason=None) -> dict`.
6. Implement `upsert_consumer_migration(conn, change_id, consumer, status) -> dict`.
7. Implement `record_approval(conn, change_id, gate, approved_by) -> dict`.
8. Implement `record_gate_decision(conn, change_id, result, reason) -> dict`.
9. Implement read functions: `get_change(conn, change_id)`, `get_evidence(conn, change_id)`, `get_dependencies(conn, change_id)`, `get_consumer_migrations(conn, change_id)`.
10. Use `uuid.uuid4()` for all generated IDs; use `datetime.utcnow().isoformat()` for timestamps.

**Relevant Context:**
- File: `orchestrator/ledger.py` (currently empty)
- Schema: `orchestrator/db/schema.sql` (Sub-Task 1)
- Python stdlib only — no ORM

**Status:** [x] done

---

### Sub-Task 3 — `orchestrator/schemas/` — Pydantic contracts

**Intent:** Define the stable, validated data contracts that all agents must conform to. These are the interfaces other teammates build against. Every agent result is validated against one of these models before any ledger write.

**Expected Outcomes:**
- `common.py` contains `Evidence`, `Dependency` exactly matching the contract document.
- `discovery.py`, `planning.py`, `implementation.py`, `verification.py` each contain the result envelope for their respective agent phase.
- `schemas/__init__.py` re-exports all public models.
- No duplicate model definitions.

**Todo List:**
1. `common.py`: Write `Evidence(BaseModel)` — fields: `claim_type: Literal[...]`, `subject: str`, `content: dict`, `source_ref: str`, `confidence: Literal[...]`, `source_revision: str | None = None`. Write `Dependency(BaseModel)` — fields from contract.
2. `discovery.py`: Write `DiscoveryResult(BaseModel)` — fields: `change_id: str`, `evidence: list[Evidence]`, `dependencies: list[Dependency]`.
3. `planning.py`: Write `PlanningResult(BaseModel)` — fields: `change_id: str`, `migration_order: list[str]`, `evidence: list[Evidence]`.
4. `implementation.py`: Write `ImplementationResult(BaseModel)` — fields: `change_id: str`, `consumer: str`, `commit_ref: str | None`, `evidence: list[Evidence]`.
5. `verification.py`: Write `VerificationResult(BaseModel)` — fields: `change_id: str`, `consumer: str`, `status: Literal["verified", "failed"]`, `evidence: list[Evidence]`.
6. `schemas/__init__.py`: re-export all models.

**Relevant Context:**
- Files: `orchestrator/schemas/*.py` (all currently empty)
- Canonical model shapes: `00_SHARED_TEAM_CONTRACT.md` → "Important Shared Interfaces"
- Prompt: "Validate every agent result before ledger insertion. Never permit arbitrary LLM JSON into the ledger."

**Status:** [x] done

---

### Sub-Task 4 — `orchestrator/state_machine.py`

**Intent:** Persist the workflow state so the orchestrator can be restarted mid-run and resume from where it stopped. Advancement decisions are made from ledger facts, never agent claims.

**Expected Outcomes:**
- `STATES` ordered list and `TRANSITIONS` map are declared explicitly.
- `get_state(conn, change_id)` returns current state string.
- `advance(conn, change_id)` checks `can_advance()` and writes the new state to the ledger — or raises `InvalidTransition`.
- `can_advance(conn, change_id, current_state)` uses only ledger reads to decide.
- State, `entered_at`, and `retry_count` are persisted in the `change_request` table (add columns if needed, or use a separate `workflow_state` table if the schema already separates concerns).

**Todo List:**
1. Declare `STATES: list[str]` in workflow order.
2. Declare `TRANSITIONS: dict[str, str]` mapping each state to its successor.
3. Implement `get_state(conn, change_id) -> str`.
4. Implement `can_advance(conn, change_id, state) -> bool` — rules per state (e.g. DISCOVERY → PLANNING requires at least one dependency_edge row; GATE_DECISION → APPROVE requires gate result == VERIFIED).
5. Implement `advance(conn, change_id) -> str` — calls `can_advance`, writes new state, returns new state name.
6. Implement `force_state(conn, change_id, state)` for test/stub use only.
7. Raise a custom `InvalidTransition(Exception)` on illegal moves.

**Relevant Context:**
- File: `orchestrator/state_machine.py` (currently empty)
- State list: `00_SHARED_TEAM_CONTRACT.md` → "Workflow"
- `change_request.status` column from Sub-Task 1 holds current state
- `can_advance` must use `ledger.py` read functions only

**Status:** [x] done

---

### Sub-Task 5 — `orchestrator/gate.py`

**Intent:** Implement the deterministic safety gate. This is the core trust anchor of the product — it must be pure Python with zero LLM involvement.

**Expected Outcomes:**
- `evaluate_gate(conn, change_id) -> GateDecision` is pure and side-effect-free (reads only).
- Result is `Literal["VERIFIED", "NOT_PROVEN_SAFE"]` with a `reason` string.
- Gate writes the `gate_decision` record via ledger when called from the orchestrator workflow.
- All three critical test cases pass: all-verified → VERIFIED; one unresolved → NOT_PROVEN_SAFE; no migration record → NOT_PROVEN_SAFE.
- The NetworkX graph is built from `dependency_edge` rows here for graph endpoint use.

**Todo List:**
1. Define `GateDecision(BaseModel)` with fields `result`, `reason`, `required_consumers: list[str]`, `unresolved: list[str]`.
2. Implement `get_required_consumers(conn, change_id) -> list[str]` — query `dependency_edge` where `to_component` is the provider and derive consumer list.
3. Implement `evaluate_gate(conn, change_id) -> GateDecision`:
   a. Fetch required consumers from dependency edges.
   b. Fetch `consumer_migration` rows for those consumers.
   c. Any consumer missing a row → NOT_PROVEN_SAFE.
   d. Any consumer with status != "verified" → NOT_PROVEN_SAFE.
   e. All verified → VERIFIED.
4. Implement `build_graph(conn, change_id) -> dict` — returns `{"nodes": [...], "edges": [...]}` suitable for pyvis/Streamlit.
5. No NetworkX graph object stored in memory between requests.

**Relevant Context:**
- File: `orchestrator/gate.py` (currently empty)
- Prompt: "find provider's required consumers from dependency edges; each required consumer must have migration status `verified`"
- `networkx` is in `requirements.txt`
- Test cases specified in `01_PERSON_1_ORCHESTRATOR_BOB_PROMPT.md` → "Important gate tests"

**Status:** [x] done

---

### Sub-Task 6 — `orchestrator/agent_runner.py` (stub)

**Intent:** Provide the reusable execution wrapper that all agent calls will go through. Phase 1 ships with deterministic stub agents built in. When real agents arrive, they replace the stub callables — the runner interface stays the same.

**Expected Outcomes:**
- `AgentRunner` class accepts a role name, callable agent function, timeout, and output schema.
- It calls the agent, validates output against the Pydantic schema, retries once on `ValidationError`, raises `AgentFailure` on second failure.
- Logs role, attempt number, and outcome to stdout.
- Stub callables for all 10 agents are defined and return hard-coded but schema-valid data.
- A `run_workflow(conn, change_id)` function drives all stubs in order, writing to the ledger between steps.

**Todo List:**
1. Define `AgentFailure(Exception)`.
2. Implement `AgentRunner(role, fn, output_schema, timeout=30)`.
3. Implement `AgentRunner.run(context: dict) -> BaseModel` — try/validate/retry logic.
4. Write stub callables: `stub_repo_map`, `stub_api_contract_discovery`, `stub_event_contract_discovery`, `stub_db_schema_discovery`, `stub_compatibility_strategy`, `stub_provider_patch`, `stub_consumer_migration_fn`, `stub_contract_test`, `stub_coexistence_rehearsal`, `stub_critic` — each returns a valid Pydantic result object.
5. Implement `run_workflow(conn, change_id)` — iterate states, run stubs, write results to ledger via `ledger.py`, call `advance()` after each phase.

**Relevant Context:**
- File: `orchestrator/agent_runner.py` (currently empty)
- Base class hint in prompt: `orchestrator/agents/base_agent.py`
- Schema models from Sub-Task 3
- Ledger write functions from Sub-Task 2
- State machine `advance()` from Sub-Task 4

**Status:** [x] done

---

### Sub-Task 7 — `orchestrator/main.py` — FastAPI routes

**Intent:** Expose the five stable HTTP endpoints that the Streamlit teammate can consume immediately. Routes are thin — they delegate to ledger, state machine, gate, and runner.

**Expected Outcomes:**
- `POST /change-requests` creates a change, triggers `run_workflow` asynchronously (or synchronously for Phase 1), returns the change record.
- `GET /change-requests/{id}` returns change status including current state.
- `GET /change-requests/{id}/evidence` returns all evidence rows.
- `GET /change-requests/{id}/graph` returns the NetworkX-derived node/edge structure.
- `POST /change-requests/{id}/approve` validates gate, records approval, advances state.
- All responses use stable Pydantic response models — no raw dict returns.
- App starts with `uvicorn orchestrator.main:app`.

**Todo List:**
1. Create `app = FastAPI(title="Interlock Orchestrator")`.
2. Implement DB lifespan — `init_db` on startup, store connection in app state.
3. Define response models: `ChangeResponse`, `EvidenceListResponse`, `GraphResponse`, `ApprovalResponse`.
4. `POST /change-requests` — body: `{"description": str}`, calls `ledger.create_change`, then `run_workflow`, returns `ChangeResponse`.
5. `GET /change-requests/{id}` — calls `ledger.get_change`, returns `ChangeResponse`.
6. `GET /change-requests/{id}/evidence` — calls `ledger.get_evidence`, returns `EvidenceListResponse`.
7. `GET /change-requests/{id}/graph` — calls `gate.build_graph`, returns `GraphResponse`.
8. `POST /change-requests/{id}/approve` — validates allowed gates (`coordinate`, `legacy_removal`), validates state, calls `ledger.record_approval`, advances state, returns `ApprovalResponse`.
9. All 404/400 cases raise `HTTPException` with clear messages.

**Relevant Context:**
- File: `orchestrator/main.py` (currently empty)
- Prompt: route list and approval gate names
- Streamlit teammate will consume these — keep response shapes stable
- `uvicorn` is in `requirements.txt`

**Status:** [x] done

---

### Sub-Task 8 — `tests/orchestrator/` — Unit and integration tests

**Intent:** Prove correctness of every component in isolation and the full stub workflow end-to-end. All tests use an in-memory SQLite DB — no file system side-effects.

**Expected Outcomes:**
- All tests pass with `pytest tests/orchestrator/`.
- Gate blocking and gate success tests are present.
- Illegal state transition raises `InvalidTransition`.
- API endpoint tests use FastAPI `TestClient`.
- Retry behaviour is tested with a stub that fails validation once then succeeds.

**Todo List:**
1. `test_schema_ledger.py` — test every ledger write/read function against in-memory DB.
2. `test_state_machine.py` — test legal advances, illegal advance raises `InvalidTransition`, `can_advance` per state.
3. `test_gate.py` — test all-verified → VERIFIED; one unresolved → NOT_PROVEN_SAFE; no migration record → NOT_PROVEN_SAFE.
4. `test_agent_runner.py` — test successful run, validation failure + retry success, double failure → `AgentFailure`.
5. `test_api.py` — test all five endpoints using `TestClient`; test approval rejection in wrong state.
6. `test_graph.py` — test `build_graph` returns correct nodes/edges from seeded dependency rows.

**Relevant Context:**
- Directory: `tests/orchestrator/` (currently empty)
- `pytest` is in `requirements.txt`
- Use `sqlite3.connect(":memory:")` for all DB tests
- `fastapi.testclient.TestClient` for API tests

**Status:** [x] done

---

## File Map

| File | Sub-Task |
|---|---|
| `orchestrator/db/schema.sql` | 1 |
| `orchestrator/ledger.py` | 2 |
| `orchestrator/schemas/common.py` | 3 |
| `orchestrator/schemas/discovery.py` | 3 |
| `orchestrator/schemas/planning.py` | 3 |
| `orchestrator/schemas/implementation.py` | 3 |
| `orchestrator/schemas/verification.py` | 3 |
| `orchestrator/schemas/__init__.py` | 3 |
| `orchestrator/state_machine.py` | 4 |
| `orchestrator/gate.py` | 5 |
| `orchestrator/agent_runner.py` | 6 |
| `orchestrator/main.py` | 7 |
| `tests/orchestrator/test_schema_ledger.py` | 8 |
| `tests/orchestrator/test_state_machine.py` | 8 |
| `tests/orchestrator/test_gate.py` | 8 |
| `tests/orchestrator/test_agent_runner.py` | 8 |
| `tests/orchestrator/test_api.py` | 8 |
| `tests/orchestrator/test_graph.py` | 8 |

## Dependency Order

Sub-Tasks must be implemented in this order (each depends on the previous):

```
1 (schema.sql)
  → 2 (ledger.py)
    → 3 (schemas/)
      → 4 (state_machine.py)
        → 5 (gate.py)
          → 6 (agent_runner.py)
            → 7 (main.py)
              → 8 (tests)
```
