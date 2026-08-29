# Planning + Implementation Pod — Build Plan
## Person 3 | Interlock | `feature/planning` branch

---

## Top-Level Overview

**Goal**: Implement three agents — `compatibility-strategy`, `provider-patch`, and `consumer-migration` — in a greenfield codebase where every file is currently an empty stub.

**Scope** (strictly Person 3 ownership):
- `agents/planning/compatibility_strategy.py`
- `agents/implementation/provider_patch.py`
- `agents/implementation/consumer_migration.py`
- `tests/planning/` (new directory)
- `tests/implementation/` (new directory)

**Out of scope** (do not touch):
- `orchestrator/` — Person 1 owns all shared Pydantic schemas; do not create duplicate models there or in `agents/planning/schemas.py`.
- `fixtures/` — Person 2 owns all fixture repositories; do not create or modify any fixture source files.
- `agents/discovery/`, `agents/verification/` — other pods.
- Docker Compose, SQLite, Neo4j, Kafka, Kubernetes, CI/CD.

**Approach**:
1. Keep agent core logic decoupled from the final shared-schema types. Agents accept and return plain Python dicts / typed `TypedDict`s internally and expose a clearly marked adapter point for when `orchestrator/schemas/` lands.
2. Tests use mock schema-shaped data (plain dicts matching the contract) and `pytest tmp_path`-based temporary Git repos — so no real `fixtures/` changes and no commits land on `feature/planning` during test runs.
3. Implement `compatibility-strategy` — derives a DAG-based migration plan from the input dependency graph; never hardcodes consumer names.
4. Implement `provider-patch` — reads a given repository path, patches source + tests, runs pytest, creates a real Git commit, returns the real SHA.
5. Implement `consumer-migration` — reads a given consumer repository path, replaces field references, runs pytest, creates a real Git commit per consumer, returns real SHAs.
6. Write pytest suites that prove every Definition-of-Done criterion using isolated temp repos.

---

## Key Design Decisions

### No duplicate Pydantic schemas
`orchestrator/schemas/` is owned by Person 1. No production Pydantic models are created here. Agents work with:
- Plain Python `TypedDict`s (or dataclasses) for **internal** computation only — these are not the final shared contracts.
- A clearly marked `# SCHEMA INTEGRATION POINT` comment in each agent file showing exactly where shared orchestrator schemas should be imported from when they arrive.
- Test files use inline mock dicts shaped to match the shared contract specification in `docs/prompts/00_SHARED_TEAM_CONTRACT.md`.

### No fixture files created or modified
Person 2 owns `fixtures/`. Until the real repos land, every test that exercises `provider-patch` or `consumer-migration` uses a **temporary Git repo** constructed inside `pytest tmp_path`. These repos mimic the expected fixture structure so agents can run against them correctly. When Person 2's work is merged, agents will operate on the real `fixtures/` paths without any code change — only the path passed to the agent changes.

### Temporary Git repos in tests
`tests/implementation/conftest.py` provides fixtures that:
1. Create a new `git init` repo under `tmp_path`.
2. Write the minimal source files (app.py / worker.py etc.) that the agents expect to find.
3. Create an initial commit so the repo has a valid HEAD.
4. Yield the repo path.
5. The temp dir is cleaned up automatically by pytest after each test.

This means `pytest tests/implementation/` never produces commits on the `feature/planning` branch.

### Agents accept repo paths as parameters
`provider-patch` and `consumer-migration` take a `repo_path: str | Path` argument alongside the change-request data. When production calls them, `repo_path` = `fixtures/account-service/`. When tests call them, `repo_path` = the temp directory. Zero code difference.

---

## Sub-Task 1 — Define Agent Internal Types and Adapter Point

**Status**: [ ] pending

### Intent
Agents need a stable internal representation of their inputs and outputs without creating production Pydantic models (which Person 1 owns). Define lightweight `TypedDict`s in each agent file for internal use only, and add a clearly marked adapter comment so integration with the real shared schemas is a one-function addition.

### Expected Outcomes
- `agents/planning/compatibility_strategy.py` defines:
  - Internal `TypedDict`s: `_ChangeRequest`, `_Dependency`, `_Evidence`, `_MigrationStep`, `_StrategyInput`, `_StrategyResult`.
  - A `# SCHEMA INTEGRATION POINT` block showing how to import from `orchestrator.schemas.planning` when available.
  - The public `run(data: dict) -> dict` signature — accepts a raw dict (validated by orchestrator before being passed), returns a raw dict.
- `agents/implementation/provider_patch.py` defines:
  - Internal `TypedDict`s: `_PatchInput`, `_PatchResult`.
  - A `# SCHEMA INTEGRATION POINT` comment.
  - Public `run(data: dict, repo_path: Path) -> dict` signature.
- `agents/implementation/consumer_migration.py` defines:
  - Internal `TypedDict`s: `_MigrationInput`, `_MigrationResult`.
  - A `# SCHEMA INTEGRATION POINT` comment.
  - Public `run(data: dict, repo_path: Path) -> dict` signature.

### Todo List
1. In `agents/planning/compatibility_strategy.py`: add `TypedDict` definitions and public `run` stub with adapter comment.
2. In `agents/implementation/provider_patch.py`: add `TypedDict` definitions and public `run` stub with adapter comment.
3. In `agents/implementation/consumer_migration.py`: add `TypedDict` definitions and public `run` stub with adapter comment.

### Relevant Context
- Conceptual field shapes from `docs/prompts/00_SHARED_TEAM_CONTRACT.md` lines 85–103.
- `TypedDict` is stdlib (`typing`); no new dependencies needed.
- Literal field values from the contract: `claim_type` in `{"dependency","migration_status","test_result","risk"}`; `edge_type` in `{"api","event","db","undocumented"}`; `confidence` in `{"hypothesis","confirmed","refuted"}`.

---

## Sub-Task 2 — Implement `compatibility-strategy`

**Status**: [ ] pending

### Intent
`compatibility-strategy` receives a change request and discovery evidence (dependencies + Evidence objects) encoded as a plain dict and produces a structured migration plan as a plain dict. It derives the migration order by building a NetworkX directed graph from the dependency list — it must never hardcode consumer names.

### Expected Outcomes
- `agents/planning/compatibility_strategy.py` has a fully implemented `run(data: dict) -> dict` function.
- The function builds a NetworkX directed graph from `data["dependencies"]`.
- It identifies all components that directly or transitively depend on the provider.
- It produces a topologically sorted `migration_steps` list: provider first, then each consumer in dependency order.
- If a `db` edge to the provider exists, that component is placed last (platform-config pattern).
- `compatibility_requirements` is derived: dual-field coexistence (provider must expose both old and new field names during the window).
- `verification_requirements` is derived: each affected consumer must be covered by a `test_result` evidence item before legacy field removal.
- `affected_consumers` lists all non-provider components in the migration steps.
- `evidence` list carries one entry per consumer step, citing the source dependency.
- Raises `ValueError` with a descriptive message for: no provider node in graph, no dependencies supplied, cycle in dependency graph.
- No side effects; no file I/O; no subprocess calls; no SQLite.

### Todo List
1. Import `networkx`, `typing.TypedDict`, stdlib modules.
2. Read `data["change_request"]` to obtain `provider` name, `old_field`, `new_field`.
3. Build `DiGraph` from `data["dependencies"]`: edge from `from_component` to `to_component`.
4. Verify provider node exists in graph; raise `ValueError` if absent.
5. Use `networkx.ancestors` to find all components that reach the provider (i.e., depend on it).
6. Detect cycles with `networkx.is_directed_acyclic_graph`; raise `ValueError` if cyclic.
7. Topologically sort consumers; move any `db`-edge consumer to the end.
8. Build `migration_steps` list: first step is the provider patch, then each consumer.
9. Build `compatibility_requirements` from old/new field names.
10. Build `verification_requirements`: one entry per consumer step.
11. Build `evidence` list, one entry per consumer, `claim_type="dependency"`, `confidence="confirmed"`.
12. Return assembled result dict.

### Relevant Context
- `requirements.txt` includes `networkx`.
- Contract source: `docs/prompts/03_PERSON_3_PLANNING_IMPLEMENTATION_BOB_PROMPT.md` lines 41–48 (illustrative, not prescriptive).
- Must NOT hardcode `"checkout"`, `"fraud"`, `"analytics-worker"`.

---

## Sub-Task 3 — Implement `provider-patch`

**Status**: [ ] pending

### Intent
`provider-patch` receives a change request and the strategy result. It accepts a `repo_path` parameter pointing to the provider repository (real or temporary). It reads the source, applies the dual-field compatibility patch, updates the OpenAPI spec, updates or adds tests that assert the new field, runs pytest, creates a real Git commit, and returns the real SHA. When `repo_path` is the real `fixtures/account-service/` (once Person 2's work lands), the agent operates identically — only the path differs.

### Expected Outcomes
- `agents/implementation/provider_patch.py` has a `run(data: dict, repo_path: Path) -> dict` function.
- The function reads ALL `.py` files under `repo_path` before modifying anything.
- It locates the Pydantic response model and adds `account_id` field while retaining `customer_id`.
- It updates `openapi.yaml` (if present) to add `account_id` to the response schema.
- It updates or creates a test file asserting `account_id` is present in the response.
- `pytest <repo_path>` is run as a subprocess; stdout/stderr is captured.
- If pytest exits non-zero, `RuntimeError` is raised with the captured output — never silently swallowed.
- `git -C <repo_path> add .` then `git -C <repo_path> commit -m "provider-patch: add <new_field>, retain <old_field>"` is executed.
- `git -C <repo_path> rev-parse HEAD` is run to capture the real SHA.
- Returned dict includes: `repository`, `files_changed`, `summary`, `commit_sha` (real 40-char hex), `evidence` (list), `status`.
- Evidence entry: `claim_type="migration_status"`, `subject=provider`, `source_ref=<file_path>`, `source_revision=<sha>`, `confidence="confirmed"`.

### Todo List
1. Implement function signature: `run(data: dict, repo_path: Path) -> dict`.
2. Extract `old_field`, `new_field`, `provider` from `data["change_request"]`.
3. Read all `.py` files under `repo_path` using `Path.rglob("*.py")`.
4. Find the response model file; insert `new_field` field after `old_field`.
5. Update `openapi.yaml` if it exists: add `new_field` property to response schema.
6. Update test files: add assertion that `new_field` is present in response payload.
7. Write all modified files back to disk.
8. Run `pytest <repo_path>` via `subprocess.run`; capture output.
9. Raise `RuntimeError` if returncode != 0.
10. Run `git -C <repo_path> add .` and `git -C <repo_path> commit`.
11. Run `git -C <repo_path> rev-parse HEAD`; store as `commit_sha`.
12. Build evidence dict and return full result dict.

### Relevant Context
- Agent does NOT write SQLite; it returns a dict and the orchestrator persists it.
- Agent does NOT call other agents.
- `repo_path` is configurable; no hardcoded `fixtures/` path inside the agent.
- On real fixture repos, the `git -C` flag scopes git to the fixture dir, not the entire Interlock repo.

---

## Sub-Task 4 — Implement `consumer-migration`

**Status**: [ ] pending

### Intent
`consumer-migration` receives a consumer name, the change request, and the strategy result. Given a `repo_path` parameter, it reads the consumer's source, replaces the old field reference with the new one, updates tests, runs pytest, creates a real Git commit, and returns the real SHA. Each consumer call is independent; each produces its own commit.

### Expected Outcomes
- `agents/implementation/consumer_migration.py` has a `run(data: dict, repo_path: Path) -> dict` function.
- `data["consumer"]` identifies which consumer is being migrated (used in commit message and evidence subject).
- Reads all `.py` files under `repo_path` before modifying.
- Replaces `old_field` string references with `new_field` using precise replacement (e.g., `event["customer_id"]` → `event["account_id"]`) to avoid corrupting unrelated identifiers.
- Updates test files to match.
- Runs `pytest <repo_path>`; raises `RuntimeError` on failure.
- Creates one Git commit per call; captures real SHA.
- Returns dict with same shape as provider-patch result plus `consumer` field.
- Evidence: `claim_type="migration_status"`, `confidence="confirmed"`, `source_revision=<sha>`.

### Todo List
1. Implement `run(data: dict, repo_path: Path) -> dict`.
2. Extract `consumer`, `old_field`, `new_field` from `data`.
3. Read all `.py` files under `repo_path`.
4. Apply precise string replacement: replace `old_field` references in string keys/attributes — not blind identifier replacement.
5. Write modified files back.
6. Run `pytest <repo_path>`; raise `RuntimeError` on non-zero exit.
7. Run `git -C <repo_path> add .` + `git -C <repo_path> commit -m "consumer-migration(<consumer>): migrate to <new_field>"`.
8. Run `git -C <repo_path> rev-parse HEAD`; capture real SHA.
9. Build evidence dict; return full result dict.

### Relevant Context
- `analytics-worker` source will contain `event["customer_id"]`; the replacement must match this string form precisely.
- `repo_path` is always passed in; never derived inside the agent by hardcoding `fixtures/<consumer>`.
- Each call to `run` handles exactly one consumer.

---

## Sub-Task 5 — Write `tests/planning/` Suite

**Status**: [ ] pending

### Intent
Prove that `compatibility-strategy` is correct: derives the plan from the dependency graph, produces a valid topological order, handles edge cases cleanly.

### Expected Outcomes
- `tests/planning/__init__.py` exists.
- `tests/planning/conftest.py` provides reusable mock input builders (plain dicts).
- `tests/planning/test_compatibility_strategy.py` with these tests:
  - `test_basic_strategy` — three consumers in the graph; migration_steps contains all of them, provider is first.
  - `test_strategy_excludes_unrelated_components` — a component with no path to the provider is absent from steps.
  - `test_strategy_raises_on_cycle` — circular dependency raises `ValueError`.
  - `test_platform_config_last` — db-edge consumer is last in migration_steps.
  - `test_evidence_backed` — `evidence` list in result is non-empty.
  - `test_analytics_worker_discovered_not_hardcoded` — graph has no analytics-worker node; result has no analytics-worker step.
  - `test_verification_requirements_per_consumer` — one verification requirement per affected consumer.
- All mock inputs are inline dicts matching the contract shape; no Pydantic imports.
- All tests are pure unit tests; no file I/O, no subprocess.

### Todo List
1. Create `tests/planning/__init__.py`.
2. Write `tests/planning/conftest.py` with `mock_three_consumer_input`, `mock_cyclic_input`, `mock_no_analytics_input` builders.
3. Write `tests/planning/test_compatibility_strategy.py` with the seven tests.
4. Run `pytest tests/planning/` — all pass.

### Relevant Context
- The anti-hardcoding test (`test_analytics_worker_discovered_not_hardcoded`) is critical for the demo.
- Mock dicts must match shapes described in `docs/prompts/00_SHARED_TEAM_CONTRACT.md` lines 85–103.

---

## Sub-Task 6 — Write `tests/implementation/` Suite

**Status**: [ ] pending

### Intent
Prove that `provider-patch` and `consumer-migration` make real code changes and real Git commits — using isolated temporary Git repositories so no commits land on `feature/planning`.

### Temporary Repo Fixture Pattern
`tests/implementation/conftest.py` provides a `tmp_provider_repo(tmp_path)` and `tmp_consumer_repo(tmp_path)` pytest fixture that:
1. Creates a `git init` repo inside `tmp_path`.
2. Configures `user.email` and `user.name` for the temp repo.
3. Writes minimal source files mimicking the expected structure (e.g., `app.py` with `customer_id` field, `tests/test_app.py`, `openapi.yaml`).
4. Commits the initial state: `git add .` + `git commit -m "initial"`.
5. Yields the `Path` to the repo root.
6. Cleanup is automatic (pytest `tmp_path` lifecycle).

This means all git history lives only in a temp directory, never in the main Interlock repo.

### Expected Outcomes
- `tests/implementation/__init__.py` exists.
- `tests/implementation/conftest.py` with `tmp_provider_repo` and `tmp_consumer_repo` fixtures.
- `tests/implementation/test_provider_patch.py`:
  - `test_provider_patch_adds_new_field` — after run, `new_field` appears in source.
  - `test_provider_patch_retains_old_field` — `old_field` still present (dual-field window).
  - `test_provider_patch_commit_sha_is_real` — SHA is 40-char lowercase hex string.
  - `test_provider_patch_tests_pass` — `run()` does not raise (pytest in temp repo passes).
  - `test_provider_patch_evidence_has_commit` — returned evidence list contains the SHA.
  - `test_provider_patch_openapi_updated` — `openapi.yaml` in temp repo mentions the new field.
- `tests/implementation/test_consumer_migration.py`:
  - `test_consumer_migration_replaces_field` — old field no longer present in source.
  - `test_consumer_migration_commit_sha_is_real` — SHA is 40-char hex.
  - `test_consumer_migration_tests_pass` — run() does not raise.
  - `test_consumer_migration_evidence_has_sha` — evidence cites real SHA.
  - `test_two_consumers_distinct_shas` — running migration on two separate temp repos yields two different SHAs.
- SHA format assertion helper: `re.fullmatch(r"[0-9a-f]{40}", sha)`.

### Todo List
1. Create `tests/implementation/__init__.py`.
2. Write `tests/implementation/conftest.py` with `tmp_provider_repo` and `tmp_consumer_repo`.
3. Write `tests/implementation/test_provider_patch.py`.
4. Write `tests/implementation/test_consumer_migration.py`.
5. Run `pytest tests/implementation/` — all pass.

### Relevant Context
- Temp repos must have a valid git identity (`user.email`, `user.name` set locally) for commits to succeed.
- The `git -C <repo_path>` flag keeps all git commands scoped to the temp repo; the main Interlock repo is never touched by the agents during tests.

---

## Input/Output Contract Summary

| Agent | Input (`data` dict) | Extra param | Output dict |
|---|---|---|---|
| `compatibility-strategy` | `change_request`, `dependencies`, `evidence` | — | `affected_consumers`, `migration_steps`, `compatibility_requirements`, `verification_requirements`, `evidence` |
| `provider-patch` | `change_request`, `strategy_result` | `repo_path: Path` | `repository`, `files_changed`, `summary`, `commit_sha`, `evidence`, `status` |
| `consumer-migration` | `consumer`, `change_request`, `strategy_result` | `repo_path: Path` | `consumer`, `repository`, `files_changed`, `summary`, `commit_sha`, `evidence`, `status` |

## Data Flow

```
DiscoveryOutput (mocked in tests / real from orchestrator)
         |
         v
compatibility-strategy.run(data)
         |
         v
StrategyResult dict
    /              \
   v                v
provider-patch.run(        consumer-migration.run(
  data, repo_path=          data, repo_path=
  fixtures/account-service  fixtures/<consumer>    ) x N
)
   |                    |
   v                    v
PatchResult dict     MigrationResult dict (one per consumer)
         \              /
          v            v
       Returned to orchestrator
       (orchestrator writes ledger — not our job)
```

## Schema Integration Point (per agent file)

Each agent file will contain a block like:

```python
# ---------------------------------------------------------------------------
# SCHEMA INTEGRATION POINT
# When orchestrator/schemas/ (Person 1) is available, replace the TypedDict
# definitions below with:
#
#   from orchestrator.schemas.planning import (
#       CompatibilityStrategyInput, CompatibilityStrategyResult, ...
#   )
#
# Until then, agents work with plain dicts; the orchestrator validates them
# before calling the agent and after receiving the result.
# ---------------------------------------------------------------------------
```

## Definition of Done Checklist
- [ ] `compatibility-strategy` derives plan from graph, not hardcoded names
- [ ] `provider-patch` reads before patching, runs real pytest, produces real SHA
- [ ] `consumer-migration` migrates each consumer independently, real commits, real SHAs
- [ ] Person 3 has not created or scaffolded baseline fixture repositories (Person 2 owns those); when real fixtures land, `provider-patch` modifies `fixtures/account-service/` and `consumer-migration` modifies the Checkout, Fraud, and Analytics Worker repos — through the implementation agents, with real pytest, real Git commits, and real SHAs
- [ ] No production Pydantic schemas in `agents/planning/schemas.py` or `orchestrator/schemas/`
- [ ] `tests/planning/` all pass with mock dict inputs
- [ ] `tests/implementation/` all pass with temporary Git repos
- [ ] Running tests leaves zero commits on `feature/planning`
- [ ] No agent writes SQLite
- [ ] No agent calls another agent
- [ ] No faked SHAs or test output
- [ ] PR on `feature/planning` branch ready

---

## Execution Order

Sub-tasks have the following dependencies:

```
Sub-Task 1 (internal types + stubs)
     |
     +---> Sub-Task 2 (compatibility-strategy logic)
     |          |
     |          +---> Sub-Task 5 (tests/planning/)
     |
     +---> Sub-Task 3 (provider-patch logic)
     |          |
     |          +---> Sub-Task 6 (tests/implementation/)
     |
     +---> Sub-Task 4 (consumer-migration logic)
               |
               +---> Sub-Task 6 (tests/implementation/)
```

Recommended serial order: **1 → 2 → 3 → 4 → 5 → 6**.
