# ADR-0004: Keep Streamlit; defer React

- **Status:** Accepted
- **Date:** 2026-08-29

## Context

The question was whether to replace the Streamlit frontend with React now.

The frontend is roughly 2,000 lines and unusually well separated: no `sqlite3`
import, no `orchestrator.*` import, all backend contact through
`frontend/utils/api_client.py`, and every response modelled in Pydantic so
`/openapi.json` can generate TypeScript types directly. `AGENTS.md` enforces this
as an invariant rather than a convention.

But `orchestrator/agent_runner.py` sets `STUB_MODE = True`, and no real agent is
wired into the pipeline. Everything the UI renders today — the `analytics-worker`
reveal, the commit SHAs, the verification results — is seeded demo data.

## Decision

Stay on Streamlit. Do not start a React migration yet.

Instead make three cheap changes that remove every blocker to porting later: add
CORS middleware (currently absent, which hard-blocks any browser app on another
origin), serve the view-model constants that `frontend/utils/derive.py` and
`frontend/components/approval.py` currently duplicate from the backend, and add a
live event stream.

## Consequences

Keeps effort on the thing that actually limits the product — a pipeline that runs
stubs. Porting stub output to React would produce prettier fake data and no new
capability.

The option does not decay. Because the API boundary is real and enforced, the
port stays roughly a one-week job whenever it is chosen, with no backend rework.
Deferring costs nothing but the features React would have bought early: real
streaming traces, auth/RBAC, embeddability, and full layout control.

## Revisit when

Both of these are true: `STUB_MODE` is gone, and live agent traces rather than
1-2 second polling are the actual bottleneck. Note that `AGENTS.md` currently
says "Do not introduce WebSockets" — that rule is precisely what a React port
would need relaxed, so the revisit requires its own ADR superseding this one.
