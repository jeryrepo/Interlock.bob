# Verification Pod — Implementation Plan
## Interlock · IBM Dev Day 2026

---

## Top-Level Overview

Build three verification agents (`contract_test`, `coexistence_rehearsal`, `critic`) and a supporting `docker-compose.yml`. All agents live in `agents/verification/`, all tests in `tests/verification/`. The real `fixtures/` directory must never be mutated by any test.

**Key constraints grounded in code reading:**

- `VerificationResult` (in `orchestrator/schemas/verification.py`) requires: `change_id: str`, `consumer: str`, `status: Literal["verified","failed"]`, `evidence: list[Evidence]`.
- `Evidence` (in `orchestrator/schemas/common.py`) requires: `claim_type` (one of `"dependency"`, `"migration_status"`, `"test_result"`, `"risk"`), `subject`, `content`, `source_ref`, `confidence`, `source_revision | None`.
- The critic MUST emit only `claim_type="risk"` evidence. It MUST NOT set `status="verified"` or `"failed"` on its own — wait, actually `VerificationResult.status` is required by the schema. The resolution: critic returns `status="failed"` only when risks are found (reflecting evidence quality problems), never a "safe" verdict implying the gate can open. The gate decision belongs to `orchestrator/gate.py`.
- No agent writes SQLite; no agent calls another agent.
- `GET /change-requests/{id}/evidence` is the critic's only data source (real HTTP call).
- The `_make_worktree` + `_prepare_account_service_worktree` pattern from `tests/implementation/test_fixture_integration.py` must be reused verbatim.

**Fixtures are pre-migration (customer_id only).** To test post-migration behavior, tests must: copy to `tmp_path` → run the relevant implementation agent against the copy → then run `contract_test.run()` against that migrated copy.

**Docker Compose:** Each fixture has a `Dockerfile`. account-service runs a uvicorn server; checkout/fraud/analytics-worker run pytest inside their containers. The compose file must be minimal and reproducible.

---

## Sub-Task 1 — `agents/verification/contract_test.py`

**Status:** `[ ] pending`

### Intent
Run the real pytest suite of a fixture (in a migrated `tmp_path` copy) as a subprocess. Return `VerificationResult` with `claim_type="test_result"` evidence populated from actual subprocess output. Never fabricate output.

### Design
```
run(data: dict, repo_path: Path) -> VerificationResult
```

`data` keys:
- `change_id: str`
- `consumer: str` — e.g. `"checkout"`, `"account-service"`
- `commit_ref: str | None` — the migration commit SHA (goes into `source_revision`)

Internally:
1. Invoke `subprocess.run(["python", "-m", "pytest", str(repo_path), "-v", "--tb=short"])`.
2. Capture stdout+stderr.
3. Parse returncode: 0 → `status="verified"`, non-zero → `status="failed"`.
4. Build one `Evidence(claim_type="test_result", ...)` from the real output.
5. Return `VerificationResult(change_id=..., consumer=..., status=..., evidence=[...])`.

### Expected Outcomes
- `run()` returns a schema-valid `VerificationResult`.
- `evidence[0].claim_type == "test_result"`.
- `evidence[0].content` includes `returncode` and `output` fields from real subprocess.
- A broken fixture causes `status="failed"` — not silently `"verified"`.

### Todo List
1. Implement `run(data, repo_path)` as described.
2. Use `VerificationResult` and `Evidence` from orchestrator schemas (no duplicate models).
3. Expose `REPO_ROOT` constant for tests to locate fixtures.
4. Return `VerificationResult` (Pydantic model), not a plain dict.

### Relevant Context
- `orchestrator/schemas/verification.py` — `VerificationResult`
- `orchestrator/schemas/common.py` — `Evidence`
- `agents/implementation/provider_patch.py` — `_run_pytest()` pattern to reuse
- `tests/implementation/test_fixture_integration.py` — `_make_worktree()` pattern

---

## Sub-Task 2 — `agents/verification/coexistence_rehearsal.py`

**Status:** `[ ] pending`

### Intent
Use `docker compose` to run a real coexistence scenario: account-service (new, dual-field) + old consumers (pre-migration). Prove the dual-field provider is backward-compatible. Then separately run the migrated consumer containers. Capture real `docker compose` output; never fabricate it.

### Design
```
run(data: dict, compose_file: Path) -> VerificationResult
```

`data` keys:
- `change_id: str`
- `consumer: str` — `"coexistence"` (the whole scenario, not one consumer)
- `scenario: Literal["old_consumers", "migrated_consumers"]` — which half to run

Internally:
1. `subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "--abort-on-container-exit", "--exit-code-from", <service>])` 
   — but since checkout/fraud/analytics-worker containers run `pytest` and exit, use `--exit-code-from` for the relevant service.
2. Capture stdout+stderr.
3. Non-zero exit → `status="failed"`.
4. Return `VerificationResult` with `claim_type="test_result"` evidence.

### Docker Compose Architecture
```
docker-compose.yml
  account-service   — builds from fixtures/account-service/, port 8000, always up
  checkout          — builds from fixtures/checkout/, runs pytest tests/, depends_on account-service
  fraud             — builds from fixtures/fraud/, runs pytest tests/, depends_on account-service
  analytics-worker  — builds from fixtures/analytics-worker/, runs pytest tests/
```

The account-service Dockerfile already runs `uvicorn app:app --host 0.0.0.0 --port 8000`. The consumer Dockerfiles already run `python -m pytest tests/ -v`.

Since the consumers run their pre-migration tests (which use `customer_id`), and the pre-migration account-service also returns only `customer_id`, the coexistence scenario for the compatibility window is: the NEW (patched) provider returns both fields, old consumers still work because `customer_id` is still present. 

For the rehearsal test in CI: we keep the containers in their pre-migration state (as fixtures ship). This proves that `docker compose up` runs and exits successfully for the baseline. A separate negative test uses a deliberately broken consumer image.

### Expected Outcomes
- `run()` returns schema-valid `VerificationResult`.
- `evidence[0].content` includes real `docker_output` from subprocess.
- If a container exits with non-zero code, `status="failed"`.
- Agent never fabricates output.

### Todo List
1. Write `docker-compose.yml` at the repo root building from `fixtures/*`.
2. Implement `run(data, compose_file)`.
3. Use `subprocess.run(["docker", "compose", ...])` — real call, not mocked.
4. Return `VerificationResult` with `claim_type="test_result"` evidence.

### Relevant Context
- `fixtures/account-service/Dockerfile` — `uvicorn app:app --host 0.0.0.0 --port 8000`
- `fixtures/checkout/Dockerfile` — `python -m pytest tests/ -v`
- `fixtures/fraud/Dockerfile` — `python -m pytest tests/ -v`
- `fixtures/analytics-worker/Dockerfile` — `python -m pytest tests/ -v`
- `orchestrator/schemas/verification.py`

---

## Sub-Task 3 — `agents/verification/critic.py`

**Status:** `[ ] pending`

### Intent
Read evidence for a change request from the orchestrator's HTTP API (`GET /change-requests/{id}/evidence`). Flag evidence quality issues as `claim_type="risk"` evidence. Never decide the gate; never write the ledger.

### Design
```
run(data: dict, base_url: str) -> VerificationResult
```

`data` keys:
- `change_id: str`
- `consumer: str` — `"critic"` (the critic speaks for the whole change, not one consumer)
- `required_consumers: list[str]` — e.g. `["checkout", "fraud", "analytics-worker"]`
- `latest_migration_commit_ts: str | None` — ISO timestamp of the last implementation commit (optional; used for staleness check)

Internally:
1. `GET {base_url}/change-requests/{change_id}/evidence` — real `httpx` or `requests` call.
2. Parse the `EvidenceListResponse` shape (`{"change_id": ..., "evidence": [...]}`).
3. Apply checks in order:
   a. **Missing consumer**: for each `required_consumer`, check that at least one `migration_status` evidence item exists with `subject == consumer`. If absent → risk.
   b. **No commit_ref**: `migration_status` evidence with no `source_revision` → risk.
   c. **Stale evidence**: if `latest_migration_commit_ts` provided, any `test_result` evidence whose `created_at` is older than that timestamp → risk.
4. Build one `Evidence(claim_type="risk", ...)` per flagged issue.
5. If no risks found → `status="verified"` (evidence quality is clean), empty evidence list.
6. If any risks found → `status="failed"`, evidence = list of risk items.

**Critical constraint**: The critic MUST NOT return `claim_type` anything other than `"risk"`. It MUST NOT set `"verified"` to mean "safe to deploy" — the gate decides safety.

### Expected Outcomes
- `run()` returns schema-valid `VerificationResult`.
- All evidence items have `claim_type="risk"`.
- `run()` never writes to the ledger (verified by test: inspect what it returns, ensure no SQLite calls).
- Stale evidence is detected.
- Missing consumer migration is detected.

### Todo List
1. Implement `run(data, base_url)` using `httpx` (or `requests`).
2. Only emit `claim_type="risk"` Evidence items.
3. Never set a gate verdict — just risk flags.
4. Return `VerificationResult`.

### Relevant Context
- `orchestrator/main.py` lines 169-177 — `GET /change-requests/{id}/evidence` route
- `orchestrator/schemas/verification.py`
- `orchestrator/schemas/common.py`

---

## Sub-Task 4 — `docker-compose.yml`

**Status:** `[ ] pending`

### Intent
Minimal compose file that builds all four fixture services and runs their tests. The consumer services run pytest and exit; account-service stays up for network reachability during the compatibility window scenario.

### Design
```yaml
services:
  account-service:
    build: ./fixtures/account-service
    ports: ["8000:8000"]

  checkout:
    build: ./fixtures/checkout
    depends_on: [account-service]

  fraud:
    build: ./fixtures/fraud
    depends_on: [account-service]

  analytics-worker:
    build: ./fixtures/analytics-worker
```

No Kafka, no K8s, no Temporal. No volumes needed for the test run.

### Expected Outcomes
- `docker compose up --abort-on-container-exit` exits 0 when all services pass.
- A broken service causes non-zero exit.

### Todo List
1. Write `docker-compose.yml` at the repo root.

---

## Sub-Task 5 — `tests/verification/` test suite

**Status:** `[ ] pending`

### Intent
Full test suite proving every required property. Uses `tmp_path` isolation (never mutates real `fixtures/`). Explicitly asserts the real fixtures are unchanged after every test that touches a copy.

### Test Structure

**`test_contract_test.py`**
- `test_contract_test_runs_real_pytest`: copy fixture to `tmp_path`, run implementation agent, run `contract_test.run()`, assert `status="verified"`, assert `evidence[0].content["output"]` contains real pytest output.
- `test_contract_test_detects_failure`: copy fixture to `tmp_path`, inject a broken test (adds `assert False`), run `contract_test.run()`, assert `status="failed"`.
- `test_contract_test_schema_valid`: assert result validates as `VerificationResult`.
- `test_contract_test_no_fixture_mutation`: assert `fixtures/checkout/checkout.py` content unchanged after test.

**`test_coexistence_rehearsal.py`** *(marked `@pytest.mark.docker`)*
- `test_rehearsal_runs_docker_compose`: call `coexistence_rehearsal.run()`, assert `status="verified"`, assert `evidence[0].content["docker_output"]` is a non-empty string.
- `test_rehearsal_detects_broken_service`: build a compose override with a broken command, assert `status="failed"`.
- `test_rehearsal_schema_valid`: assert result validates as `VerificationResult`.

**`test_critic.py`**
- `test_critic_detects_missing_consumer`: spin up a mock HTTP server returning evidence with no checkout migration entry; assert critic returns a risk item for missing checkout.
- `test_critic_detects_stale_evidence`: mock HTTP returning a `test_result` evidence item with an old `created_at`; pass a newer `latest_migration_commit_ts`; assert risk flagged.
- `test_critic_no_ledger_writes`: run critic, inspect that it only returns a `VerificationResult` and has no SQLite calls (structural — verified by reading the code and by ensuring `import orchestrator.ledger` is absent from `critic.py`).
- `test_critic_only_emits_risk_evidence`: assert all `evidence[i].claim_type == "risk"`.
- `test_critic_schema_valid`: assert result validates as `VerificationResult`.
- `test_critic_no_verdict_when_risks_found`: critic returns `status="failed"` when risks found — not a gate VERIFIED/NOT_PROVEN_SAFE decision.

**Common assertion in every test class:**
- `test_no_fixture_mutation`: after running any agent in a `tmp_path`, assert the canonical fixture file content equals `before_content`.

### Todo List
1. Write `tests/verification/__init__.py`.
2. Write `tests/verification/test_contract_test.py`.
3. Write `tests/verification/test_coexistence_rehearsal.py`.
4. Write `tests/verification/test_critic.py`.
5. Ensure every test that copies a fixture also explicitly asserts the real fixture was not mutated.
6. Mark Docker-dependent tests with `@pytest.mark.docker` so they can be skipped in CI without Docker.

### Relevant Context
- `tests/implementation/test_fixture_integration.py` — `_make_worktree()` for tmp_path setup
- `agents/implementation/provider_patch.py` — `run()` for pre-seeding migrated state
- `agents/implementation/consumer_migration.py` — `run()` for pre-seeding migrated state
- `orchestrator/schemas/verification.py` — schema validation

---

## Architecture Diagram

```
fixtures/ (never mutated)
    account-service/   checkout/   fraud/   analytics-worker/
           |               |           |           |
    [tmp_path copy]  [tmp_path copy]  ...        ...
           |               |
    provider_patch()  consumer_migration()
           |               |
    contract_test.run()  contract_test.run()
           |               |
    VerificationResult   VerificationResult
           |
    [returned to orchestrator — ledger writes by orchestrator only]

    critic.run(change_id, base_url)
        → GET /change-requests/{id}/evidence   [read-only HTTP]
        → returns list[Evidence(claim_type="risk")]
        → VerificationResult

    coexistence_rehearsal.run(data, compose_file)
        → subprocess docker compose up
        → VerificationResult
```

---

## Dependencies

- Python stdlib: `subprocess`, `pathlib`, `datetime`
- `httpx` (or `requests`) — critic HTTP call
- `pydantic` — already in project
- `pytest` — already in project
- Docker + docker-compose — only for coexistence tests
- `orchestrator.schemas.verification.VerificationResult`
- `orchestrator.schemas.common.Evidence`
- Implementation agents (`provider_patch`, `consumer_migration`) — used in tests only, not imported by verification agents themselves

---

## Open Questions / Decisions Made

1. **Critic `status` field**: Since `VerificationResult.status` is `Literal["verified","failed"]` and the critic must not issue a safety verdict, we use: `"failed"` = risks found (evidence quality problems), `"verified"` = no quality problems found. This is evidence-quality status, not migration safety status. The gate reads migration_status rows, not critic output.

2. **`consumer` field for critic and rehearsal**: The schema requires `consumer: str`. Critic uses `"critic"` as the consumer name; rehearsal uses `"coexistence"`. These are conventional, not one of the four fixture services.

3. **Coexistence test strategy**: Keep the coexistence rehearsal in the pre-migration baseline state (what fixtures already are). This means: old provider + old consumers → all pass. The separate "negative path" test uses a docker compose override with a broken CMD.

4. **`httpx` vs `requests`**: Use `httpx` (synchronous) since it's more modern. If not in the project's deps, fall back to `requests`.
