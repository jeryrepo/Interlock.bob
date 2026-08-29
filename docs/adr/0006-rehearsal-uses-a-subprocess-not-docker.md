# ADR-0006: The coexistence rehearsal uses a subprocess, not Docker

- **Status:** Accepted
- **Date:** 2026-08-29
- **Supersedes:** the Docker Compose requirement in
  `docs/prompts/04_PERSON_4_VERIFICATION_BOB_PROMPT.md:43`

## Context

The Person 4 brief said "Use Docker Compose to run a small realistic coexistence
scenario", and the team contract assigns `docker-compose.yml` to the verification
pod. The rehearsal was built that way: a provider container, a probe container,
and one container per consumer running its own pytest suite.

Two problems surfaced once it existed.

**The consumer containers proved nothing.** The consumer fixtures are pure
functions taking a dict — `process_order(account_response: dict, ...)`,
`process_event(event: dict)`. None of them makes an HTTP call. Containerising
their pytest runs re-executed exactly what `contract_test.py` already covers,
only slower and behind an image build. They contributed no evidence about
coexistence.

**The daemon dependency made the rehearsal unrunnable.** Docker CLI 29.5.2 and
Compose v5.1.4 were installed, but no daemon was reachable. So the one agent
whose entire purpose is to *prove* something was left unverified — which is the
worst possible state for it to be in. It would also not run in CI without a
Docker service.

What Docker genuinely bought was a provider in a separate process answering over
a real socket, which is a meaningfully stronger claim than an in-process
`TestClient` that bypasses the network. That property is worth keeping.

## Decision

The rehearsal starts the provider as a **local `uvicorn` subprocess** on an
OS-assigned free port and asserts against it over real HTTP on loopback. The
consumer containers are removed.

`docker-compose.yml` is retained as an optional demo path — provider plus probe
only. Both paths share one implementation of the assertions,
`agents/verification/rehearsal/probe.py::check_payload`, so they cannot drift
apart and claim different things.

Docker would earn its place back if the components had conflicting dependencies
or real network topology worth modelling. They have neither: every fixture
depends on `pytest`, and one additionally on FastAPI.

## Consequences

The rehearsal now runs in **~1 second with no daemon**, in any environment,
including CI. Its integration tests execute by default instead of skipping, so
the agent is actually verified: three real-server tests cover the passing case,
the unpatched-provider failure, and a directory with no ASGI app.

The property that mattered is preserved — a separate OS process, a real socket,
a real HTTP round trip. What is lost is container-level dependency isolation,
which these fixtures do not need, and some demo theatre, which the retained
compose file still provides.

The wider lesson is worth stating: a verification step that cannot run is worse
than one that fails, because it produces no signal while looking like diligence.

## Revisit when

Components acquire genuinely conflicting dependencies, or the rehearsal needs to
model network topology (partitions, latency, TLS termination) that loopback
cannot represent.
