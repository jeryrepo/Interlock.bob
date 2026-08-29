# Verification pod — plan and decisions

Person 4's workstream: `agents/verification/`, `tests/verification/`,
`docker-compose.yml`. The other three workstreams each shipped a plan document;
this is the one that was missing.

## Status

| Deliverable | State |
| --- | --- |
| `critic` | Complete — 330 lines, 36 tests |
| `contract-test` | Complete — real pytest execution, 22 tests |
| `coexistence-rehearsal` | Complete — real uvicorn subprocess, 33 tests incl. 3 integration |
| `docker-compose.yml` | Retained as an optional demo path (provider + probe) |

Full suite: **384 passed, 0 failed, 0 skipped**. The rehearsal's integration
tests start a genuine uvicorn process and run by default — no daemon needed.

## Decisions

### The rehearsal runs a real service — as a subprocess, not a container

The fixtures could not be composed as they stood: `account-service`'s Dockerfile
ran `uvicorn app:app`, but `app.py` defines no ASGI application, and its
`requirements.txt` listed only `pytest`. The container could never have started.

The first fix promoted `account-service` into a real service and drove the
rehearsal with `docker compose`, per the Person 4 brief. That was then reversed
— see [ADR-0006](docs/adr/0006-rehearsal-uses-a-subprocess-not-docker.md).

Two things were wrong with the container version. The consumer services proved
nothing: the consumer fixtures are pure functions taking a dict and make no HTTP
calls, so containerising their pytest runs re-ran what `contract_test.py` already
covers. And the daemon dependency left the rehearsal **unverified**, which is the
worst state for the one agent whose job is to prove something.

The provider now starts as a local `uvicorn` subprocess on an OS-assigned free
port. This keeps the property that mattered — separate process, real socket,
real HTTP round trip — while running in ~1 second with no daemon, in CI, and
verifiably. `docker-compose.yml` stays as an optional demo path, sharing one
implementation of the assertions via `rehearsal/probe.py::check_payload`.

### The web layer lives in `service.py`, not `app.py`

`app.py` is the file the provider-patch agent rewrites, and its regexes were
written against exactly that code. Adding FastAPI scaffolding to it would risk
the migration mechanics and would force a web framework onto the fixture's
dependency-free unit tests.

So `service.py` is a thin delegating wrapper: `/health` plus `/accounts/{key}`
calling `get_account`. The path parameter is named `key`, not `customer_id`, so
migrating the provider does not incidentally rewrite the HTTP layer. `app.py` is
untouched and its three existing tests still pass.

> **Cross-boundary change, called out per `AGENTS.md`:** `fixtures/` is Person
> 2's area. Changes there were additive — a new `service.py`, two lines added to
> `requirements.txt`, and a corrected `Dockerfile` CMD. No existing fixture logic
> was modified.

### The probe does not live under `fixtures/`

The discovery agents treat every immediate subdirectory of the fixtures root as
a component. A probe directory there would have appeared in the dependency graph
as a phantom consumer and polluted the gate's required set. It lives in
`agents/verification/rehearsal/` instead.

### Not proven is recorded as failed, never skipped

An unreachable Docker daemon, a missing compose file, a provider that never goes
healthy, and a suite with no tests collected (pytest exit 5) all produce
`status="failed"` with an `outcome` field distinguishing *why*. None of them
produce a pass. `content["outcome"]` separates "tests failed" from "tests could
not run" — both block the gate, but only the second means evidence is absent
rather than damning.

This is `AGENTS.md` invariant 4, and it is the single most load-bearing property
of both agents.

### The critic no longer hardcodes component names

`_REAL_COMPONENTS` was a frozenset of the four demo component names used in a
live check. It broke invariant 6 twice: it hardcoded `analytics-worker` — the
dependency the product claims to *discover* — and it silently skipped the
missing-commit check for any component outside the list, weakening verification
exactly where that was least visible.

Components are now derived from evidence: `required_consumers` unioned with the
subjects of discovery's `dependency` claims. What remains hardcoded is only
`migration-plan`, the planner's own artifact subject — naming the planner's
output is not naming a discovered component. The `625828d` regression stays
covered, and a new test proves a component that was never in the old allowlist
is now checked.

## How to run it

Unit tests need no Docker:

```bash
.venv/Scripts/python.exe -m pytest tests/verification/ -q
```

That includes the real-server integration tests — no daemon required.

The optional containerised demo path:

```bash
docker compose run --rm coexistence-probe
```

`EXPECT_NEW=1` flips the probe to require both fields, which is the assertion
that holds once the provider patch has landed.

## Known issues

- ~~The live rehearsal has not been run end-to-end.~~ **Resolved 2026-08-29**
  by moving off Docker (ADR-0006). The rehearsal now runs for real in every test
  run: three integration tests start a genuine uvicorn process and cover the
  passing case, an unpatched provider failing post-migration expectations, and a
  directory with no ASGI app.
- The optional `docker-compose.yml` path is syntax-validated
  (`docker compose config`) but has not been executed, since no daemon was
  reachable during development. Verify it before relying on it in a demo.
- ~~Four tests in `tests/implementation/test_consumer_migration.py` fail in a
  full-suite run.~~ **Fixed 2026-08-29.** They were not a test-ordering problem:
  they imported `from tests.implementation.conftest import ...`, which cannot
  resolve without `tests/__init__.py`. Fixed by using the
  `tmp_checkout_repo` / `tmp_fraud_repo` / `tmp_analytics_worker_repo` fixtures
  the conftest already exposed. No assertion was changed. `AGENTS.md` and
  `README.md` have been corrected.
