# ADR-0001: Exit the 48-hour MVP scope

- **Status:** Accepted
- **Date:** 2026-08-29

## Context

`docs/prompts/00_SHARED_TEAM_CONTRACT.md` line 36 forbids adding "Neo4j,
Temporal, Kafka, Kubernetes, OTel, OPA, GitHub Actions, auth, or dashboards in
the MVP", and `docs/prompts/07_48_HOUR_EXECUTION_CHECKLIST.md` repeats the
prohibition on CI/CD and auth. Those constraints were correct: they protected a
48-hour build from breadth that would have sunk it.

Interlock is now moving past that build. The work ahead includes a packaged CLI,
an MCP server, a GitHub Action that reviews pull requests, and eventually an IBM
Cloud deployment behind watsonx Orchestrate. Three of those are explicitly
outside the contract's MVP scope.

Leaving the prohibition in place while violating it would make the contract
unreliable, and the contract is what every teammate and every coding agent reads
first. A stale authoritative document is worse than no document.

## Decision

We amend the shared team contract rather than quietly working around it.
Packaging (`pyproject.toml`), a `interlock` CLI, an MCP server, and GitHub
Actions are now in scope. The remaining exclusions — Neo4j, Temporal, Kafka,
Kubernetes, OTel, OPA — stay excluded, and the coexistence rehearsal in
particular must remain plain Docker Compose.

The **Golden Rules** in the contract are not amended. Every one of them, and
every invariant in `AGENTS.md`, survives this change unchanged.

## Consequences

Makes it possible to ship Interlock as a tool developers actually run, rather
than a demo they watch. Costs real maintenance surface: CI configuration, a
packaged distribution, and a second integration contract (MCP) to keep working.

The main risk is scope creep — the original exclusion list existed because this
project can absorb infinite breadth. Mitigation: the exclusions that remain are
the expensive infrastructure ones, and they stay hard limits.

## Revisit when

Never expected to be revisited; superseding this would mean returning to
hackathon scope.
