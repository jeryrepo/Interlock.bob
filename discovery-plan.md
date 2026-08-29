# Discovery Pod — Implementation Plan
## Interlock IBM Dev Day 2026 | feature/discovery branch

---

## Top-Level Overview

**Goal:** Implement four discovery agents, fix five fixture repositories, and write regression tests that prove the entire discovery pipeline — including the mechanism-discovered `analytics-worker` dependency.

**Scope:** `agents/discovery/`, `fixtures/`, `tests/discovery/`

**Do NOT touch:** `orchestrator/`, other agents, other test directories.

**Canonical shared models** (`Evidence`, `Dependency`, `DiscoveryResult`) already exist in `orchestrator/schemas/`. Import them; never duplicate.

**Critical constraint:** `analytics-worker/worker.py` currently accesses `event["account_id"]` (the new field). It MUST be corrected to access `event["customer_id"]` (the OLD field being migrated FROM) so discovery agents can detect it as an undocumented consumer of the migrating field. Same correction applies to `checkout.py` and `fraud.py` — they must use `customer_id` to represent their pre-migration state.

**Key design decisions:**
- All agents are pure functions: `run(data: dict) -> dict` matching `DiscoveryResult` shape.
- No subprocess/git writes; agents are read-only inspectors.
- AST-based discovery in event agent (no string grep for the analytics-worker case).
- `platform-config/schema.sql` must contain actual `customer_id` DDL.
- No hardcoded service names anywhere in discovery logic.

---

## Sub-Task 1 — Fix Fixture Source Files

**Status:** [ ] pending

**Intent:**  
The fixtures are currently in a post-migration or incorrect state. The demo story is: "a developer proposes migrating `customer_id → account_id`". For discovery to be meaningful, the fixture code must be in the PRE-migration state — using `customer_id`. The undocumented `analytics-worker` must directly access `event["customer_id"]` in source so discovery can find it. `checkout` and `fraud` must document `customer_id` usage via their API contracts (openapi.yaml) and source. `platform-config/schema.sql` must contain DDL with `customer_id` columns. The account-service Dockerfiles and platform-config files are currently empty and must be populated.

**Expected Outcomes:**
- `fixtures/analytics-worker/worker.py` accesses `event["customer_id"]` (not `account_id`)
- `fixtures/checkout/checkout.py` reads `account_response["customer_id"]`
- `fixtures/fraud/fraud.py` reads `account_response["customer_id"]`
- `fixtures/account-service/app.py` exposes `customer_id` only (no `account_id` yet — that's pre-migration)
- `fixtures/account-service/openapi.yaml` documents only `customer_id` in the response schema
- `fixtures/platform-config/schema.sql` contains real DDL with `customer_id` columns and a migration comment
- `fixtures/platform-config/README.md` describes the config schema
- All four Dockerfiles populated (minimal but real)
- All fixture tests updated to match corrected field names and pass

**Todo List:**
1. Fix `fixtures/analytics-worker/worker.py` — change `event["account_id"]` → `event["customer_id"]` in `process_event()`, update docstring to say this is the undocumented consumer of the old field
2. Fix `fixtures/analytics-worker/tests/test_worker.py` — update test dicts to use `customer_id` key and assert on correct output
3. Fix `fixtures/checkout/checkout.py` — change `account_response["account_id"]` → `account_response["customer_id"]`
4. Fix `fixtures/checkout/tests/test_checkout.py` — update test input dict keys
5. Fix `fixtures/fraud/fraud.py` — change both occurrences of `account_response["account_id"]` → `["customer_id"]`, rename local vars
6. Fix `fixtures/fraud/tests/test_fraud.py` — update test input dict keys
7. Fix `fixtures/account-service/app.py` — remove the `account_id` key from `to_dict()`, keep only `customer_id`
8. Fix `fixtures/account-service/tests/test_app.py` — remove assertion about `account_id`
9. Fix `fixtures/account-service/openapi.yaml` — remove `account_id` property, keep only `customer_id`
10. Write `fixtures/platform-config/schema.sql` — real DDL: `accounts` table with `customer_id` PK, `orders` table with `customer_id` FK, migration comment block
11. Write `fixtures/platform-config/README.md` — describe the schema and customer_id usage
12. Write `fixtures/account-service/Dockerfile` — minimal Python/FastAPI Dockerfile
13. Write `fixtures/checkout/Dockerfile` — minimal Python Dockerfile
14. Write `fixtures/fraud/Dockerfile` — minimal Python Dockerfile
15. Write `fixtures/analytics-worker/Dockerfile` — minimal Python Dockerfile

**Relevant Context:**
- `fixtures/analytics-worker/worker.py` line 21: currently `event["account_id"]` — must become `event["customer_id"]`
- `fixtures/checkout/checkout.py` line 17: currently `account_response["account_id"]`
- `fixtures/fraud/fraud.py` lines 19, 25: currently `account_response["account_id"]`
- `fixtures/account-service/openapi.yaml` already has `account_id` property — remove it
- `fixtures/platform-config/schema.sql` is currently empty

---

## Sub-Task 2 — Implement `repo-map` Agent

**Status:** [ ] pending

**Intent:**  
The repo-map agent does a filesystem walk of every repository under `fixtures/` and produces a structured inventory: which components exist, what source files they contain, whether they have OpenAPI specs, event-related files, schema/migration files, and which files reference `customer_id`. This is the foundation for the other three agents. It uses `pathlib` for walking and `ast` or simple grep for field references. It must discover all five fixtures without any hardcoded names.

**Expected Outcomes:**
- `agents/discovery/repo_map.py` is fully implemented
- `run(data)` accepts `{"change_id": str, "fixtures_root": str}` and returns a dict matching `DiscoveryResult`
- Discovers all five fixture directories dynamically (no hardcoded names)
- Produces repository inventory evidence; specialized agents own dependency edges
- Produces `Evidence` items with `source_ref` pointing to real file paths
- Returns valid `DiscoveryResult` (Pydantic validates successfully)

**Todo List:**
1. Write `agents/discovery/repo_map.py` with a `run(data: dict) -> dict` function
2. Accept `fixtures_root` from data; default to `fixtures/` relative to project root
3. Walk each subdirectory of `fixtures_root` as a component
4. For each component, collect: list of `.py` source files, list of `.yaml`/`.yml` files, list of `.sql`/migration files, `Dockerfile` presence
5. Scan all collected files for occurrences of `customer_id` (using `ast.walk` for `.py` files, line-grep for others) — record file path and line number as `source_ref`
6. Emit one `Evidence` per component with `claim_type="dependency"`, `content={"files": [...], "openapi": [...], "sql": [...]}`, real `source_ref`
7. Return `dependencies=[]`; otherwise generic scan edges duplicate and can overwrite the API/event/DB classification supplied by specialized agents
8. Wrap everything into `DiscoveryResult(change_id=..., evidence=[...], dependencies=[]).model_dump()` and return

**Relevant Context:**
- `orchestrator/schemas/discovery.py` — `DiscoveryResult` model
- `orchestrator/schemas/common.py` — `Evidence`, `Dependency`
- `orchestrator/schemas/__init__.py` — import point: `from orchestrator.schemas import Evidence, Dependency, DiscoveryResult`
- Pattern from `agents/planning/compatibility_strategy.py` — returns `dict`, validates with Pydantic internally

---

## Sub-Task 3 — Implement `api-contract-discovery` Agent

**Status:** [ ] pending

**Intent:**  
Inspect OpenAPI specs and consumer source code to identify documented API consumers of `account-service`. Must discover `checkout` and `fraud` as consumers via two evidence sources: (1) the OpenAPI spec of `account-service` showing the `customer_id` field, and (2) source code in `checkout.py` / `fraud.py` that accesses `customer_id` from an API response dict. Evidence must cite real file paths and real line numbers obtained by inspecting files at runtime.

**Expected Outcomes:**
- `agents/discovery/api_contract.py` is fully implemented
- Discovers `account-service → checkout` with `edge_type="api"`
- Discovers `account-service → fraud` with `edge_type="api"`
- Each dependency is backed by at least one `Evidence` item with a real `source_ref` (file:line)
- Does NOT discover `analytics-worker` as an API consumer (it has no API contract)
- Returns valid `DiscoveryResult`

**Todo List:**
1. Write `agents/discovery/api_contract.py` with `run(data: dict) -> dict`
2. Accept `fixtures_root` from data
3. Find the `account-service/openapi.yaml` and parse it with `yaml` (stdlib `pathlib` + `yaml` module)
4. Extract the response schema field names that contain `customer_id` — record file + line as `source_ref`
5. Walk all other fixture directories for Python source files
6. In each Python file, use AST (`ast.parse`, walk `ast.Subscript` nodes) to find `dict["customer_id"]` access patterns
7. Exclude `analytics-worker` from API consumer detection (it has no documented API contract reference — its `conftest.py` / `README` contain no account-service URL/spec reference)
8. For each confirmed API consumer (checkout, fraud): emit `Dependency(from_component="account-service", to_component=component, edge_type="api")` and `Evidence(claim_type="dependency", source_ref="fixtures/{component}/{file}:{line}", ...)`
9. Return `DiscoveryResult(...).model_dump()`

**Relevant Context:**
- `fixtures/account-service/openapi.yaml` — the spec to parse
- `fixtures/checkout/checkout.py` — contains `account_response["customer_id"]`
- `fixtures/fraud/fraud.py` — contains `account_response["customer_id"]`
- `fixtures/analytics-worker/worker.py` — must NOT be classified as API consumer
- The distinction: checkout/fraud READMEs will reference account-service; analytics-worker README will not

---

## Sub-Task 4 — Implement `event-contract-discovery` Agent (HIGHEST PRIORITY)

**Status:** [ ] pending

**Intent:**
This is the critical demo mechanism. The agent must find `analytics-worker` as a consumer of `account-service` events by inspecting Python AST — not by reading any documentation, config, or hardcoded list. The detection is grounded in the actual `analytics-worker/worker.py` source: after Sub-Task 1, function `process_event(event: dict)` contains `cid = event["customer_id"]`. The agent matches this with three combined AST conditions: (1) subscript on a variable literally named `event`, (2) key equals the target field (`customer_id`), and (3) enclosing function name contains `"event"`, `"handle"`, `"on_"`, or `"consume"`. This combination correctly fires on `process_event()` and does not fire on `process_order()` (checkout) or `check_fraud()` (fraud) which use `account_response` as the variable name.

No file should document or hint at this dependency. The discovery must be purely mechanism-driven (AST).

**Expected Outcomes:**
- `agents/discovery/event_contract.py` is fully implemented
- Discovers `account-service → analytics-worker` with `edge_type="event"`
- Evidence `source_ref` cites the exact file and line number of `event["customer_id"]` in `worker.py`
- Does NOT discover checkout or fraud as event consumers (they access `account_response["customer_id"]`, not `event["customer_id"]`)
- Returns valid `DiscoveryResult`
- If `event["customer_id"]` is removed from `worker.py`, the agent produces zero event-type dependencies

**Todo List:**
1. Write `agents/discovery/event_contract.py` with `run(data: dict) -> dict`
2. Accept `fixtures_root` from data
3. Walk all fixture directories, collect all `.py` source files (exclude test files and `conftest.py`)
4. For each file, use `ast.parse()` + `ast.walk()` to find `ast.Subscript` nodes where **all three conditions hold**:
   - The value (object being subscripted) is an `ast.Name` whose `id` is exactly `"event"`
   - The slice is an `ast.Constant` with value matching `data.get("old_field", "customer_id")`
   - The enclosing `ast.FunctionDef` `.name` contains any of: `"event"`, `"handle"`, `"on_"`, `"consume"` (case-insensitive substring match)
   - Rationale grounded in actual file: `process_event()` in `worker.py` satisfies all three conditions; `process_order()` in `checkout.py` uses `account_response` not `event`; `check_fraud()` in `fraud.py` uses `account_response` not `event`
5. Record `(file_path, line_number)` for each match — this is the `source_ref`
6. The containing directory name of `file_path` (relative to `fixtures_root`) is the `consumer` component name
7. Emit `Dependency(from_component="account-service", to_component=consumer, edge_type="event")` and `Evidence(claim_type="dependency", source_ref=f"{relative_path}:{lineno}", confidence="confirmed", ...)`
8. Do not hardcode any component name — the component name is derived from the directory containing the matching file
9. Return `DiscoveryResult(...).model_dump()`

**Relevant Context:**
- `fixtures/analytics-worker/worker.py` after Sub-Task 1 fix: function `process_event(event: dict)` contains `cid = event["customer_id"]` at line ~13
- All three conditions: variable `event`, key `customer_id`, enclosing function `process_event` (contains "event") → MATCHED
- `fixtures/checkout/checkout.py`: function `process_order(account_response)` uses `account_response["customer_id"]` — variable is `account_response`, not `event` → NOT matched
- `fixtures/fraud/fraud.py`: functions `check_fraud` / `get_risk_score` use `account_response["customer_id"]` — variable `account_response` → NOT matched
- AST node type: `ast.Subscript(value=ast.Name(id="event"), slice=ast.Constant(value="customer_id"))` inside `ast.FunctionDef(name="process_event")`

---

## Sub-Task 5 — Implement `db-schema-discovery` Agent

**Status:** [ ] pending

**Intent:**  
Inspect `platform-config` schema/migration files for `customer_id` references to produce evidence that the DB schema is a dependency of the migration. Uses file-reading and line-grep (not AST, since SQL is not Python). Must cite real line numbers from `schema.sql`.

**Expected Outcomes:**
- `agents/discovery/db_schema.py` is fully implemented
- Discovers `account-service → platform-config` with `edge_type="db"`
- Evidence `source_ref` cites `fixtures/platform-config/schema.sql:{line}` with a real line number
- Returns valid `DiscoveryResult`

**Todo List:**
1. Write `agents/discovery/db_schema.py` with `run(data: dict) -> dict`
2. Accept `fixtures_root` from data
3. Walk all fixture directories for files matching: `*.sql`, `*migration*`, `*schema*`, `alembic.ini`, `versions/*.py`
4. For each file, read lines and find occurrences of the target field (default `customer_id`)
5. Record `(file_path, line_number, line_content)` for each match
6. Emit `Dependency(from_component="account-service", to_component=component, edge_type="db")` where `component` is the directory name
7. Emit `Evidence` with `claim_type="dependency"`, `source_ref=f"{relative_path}:{lineno}"`, `confidence="confirmed"`, `content={"line": line_content.strip(), "field": old_field}`
8. Return `DiscoveryResult(...).model_dump()`

**Relevant Context:**
- `fixtures/platform-config/schema.sql` (written in Sub-Task 1) — must contain `customer_id` DDL at specific lines
- No Python AST needed — simple line scan is appropriate for SQL

---

## Sub-Task 6 — Write Discovery Regression Tests

**Status:** [ ] pending

**Intent:**  
Create `tests/discovery/` with a test file for each agent plus a critical regression test that proves analytics-worker is only discovered when `event["customer_id"]` is present in source. Tests use the real `fixtures/` directories (read-only) and validate Pydantic output shapes.

**Expected Outcomes:**
- `tests/discovery/__init__.py` exists
- `tests/discovery/conftest.py` with shared fixtures (fixtures_root path, sample data dict)
- `tests/discovery/test_repo_map.py` — proves all five repos found
- `tests/discovery/test_api_contract.py` — proves checkout and fraud found, analytics-worker not in API edges
- `tests/discovery/test_event_contract.py` — proves analytics-worker found; plus removal regression
- `tests/discovery/test_db_schema.py` — proves platform-config found
- All 8 required assertions pass (from the mission spec)

**Todo List:**
1. Create `tests/discovery/__init__.py` (empty)
2. Create `tests/discovery/conftest.py` with:
   - `fixtures_root` fixture returning the absolute path to `fixtures/`
   - `base_data` fixture returning `{"change_id": "test-001", "fixtures_root": str(fixtures_root), "old_field": "customer_id"}`
3. Write `tests/discovery/test_repo_map.py`:
   - `test_finds_all_five_repos` — assert all of `{"account-service", "checkout", "fraud", "analytics-worker", "platform-config"}` appear in component names from dependencies or evidence
   - `test_result_validates_as_discovery_result` — assert `DiscoveryResult(**result)` does not raise
   - `test_source_refs_are_real_paths` — assert every `source_ref` in evidence refers to an existing file path
4. Write `tests/discovery/test_api_contract.py`:
   - `test_finds_checkout_as_api_consumer` — assert dependency `from_component="account-service", to_component="checkout", edge_type="api"` present
   - `test_finds_fraud_as_api_consumer` — assert dependency `from_component="account-service", to_component="fraud", edge_type="api"` present
   - `test_analytics_worker_not_api_consumer` — assert no edge with `to_component="analytics-worker"` and `edge_type="api"`
   - `test_result_validates_as_discovery_result` — Pydantic validation
5. Write `tests/discovery/test_event_contract.py`:
   - `test_finds_analytics_worker_as_event_consumer` — assert dependency `from="account-service", to="analytics-worker", edge_type="event"` present
   - `test_source_ref_is_real_line` — open the cited file at the cited line and assert `customer_id` is in that line
   - **`test_removing_source_usage_removes_dependency`** (REGRESSION TEST):
     - Read `fixtures/analytics-worker/worker.py`
     - Write a modified version WITHOUT `event["customer_id"]` to a `tmp_path` copy of the fixtures tree
     - Run event discovery against the `tmp_path` fixtures root
     - Assert zero event-type dependencies returned
     - (Original file is never mutated)
   - `test_result_validates_as_discovery_result`
6. Write `tests/discovery/test_db_schema.py`:
   - `test_finds_platform_config_as_db_consumer` — assert dependency `to_component="platform-config", edge_type="db"` present
   - `test_source_ref_is_real_line` — open cited SQL file at cited line and assert `customer_id` in that line
   - `test_result_validates_as_discovery_result`

**Relevant Context:**
- `tests/planning/test_compatibility_strategy.py` — reference for test style (plain dicts, pure unit)
- `tests/implementation/test_provider_patch.py` — reference for `tmp_path` isolation pattern
- Regression test must NOT mutate `fixtures/analytics-worker/worker.py` — use `tmp_path` copy
- `pytest.ini` — `addopts = --basetemp=.pytest_tmp`

---

## Dependency Map

```
Sub-Task 1 (Fix Fixtures)
    ↓
Sub-Task 2 (repo-map)       Sub-Task 3 (api-contract)
    ↓                               ↓
Sub-Task 4 (event-contract)    Sub-Task 5 (db-schema)
    ↓
Sub-Task 6 (Tests)  ←  all agents complete
```

Sub-Tasks 2, 3, 4, 5 each depend on Sub-Task 1.
Sub-Task 6 depends on all agents (2–5) being complete.
Sub-Tasks 2, 3, 4, 5 can be implemented in any order after Sub-Task 1.

---

## Non-Goals (Explicit)

- Do NOT implement git commits or PRs (that's a final step done after all sub-tasks pass)
- Do NOT write SQLite
- Do NOT call other agents
- Do NOT redesign orchestrator/
- Do NOT add a `fixtures/__init__.py` that would make fixtures importable as a package
- Do NOT add Neo4j, Kafka, or any infrastructure beyond what's in requirements.txt
- Do NOT hardcode "analytics-worker" as an expected result in any agent (only in tests)

---

## Definition of Done

- [ ] All five fixture directories have correct pre-migration `customer_id` source code
- [ ] `platform-config/schema.sql` has real DDL with `customer_id`
- [ ] All four agents (`repo_map`, `api_contract`, `event_contract`, `db_schema`) return valid `DiscoveryResult`-shaped dicts
- [ ] `analytics-worker` is discovered via AST inspection, not hardcoding
- [ ] All 8 assertions in `tests/discovery/` pass
- [ ] Regression test proves removal of `event["customer_id"]` removes the dependency
- [ ] No duplicate Pydantic models created
- [ ] `requirements.txt` updated with `pyyaml` if needed
