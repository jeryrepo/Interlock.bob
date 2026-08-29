# PERSON 2 — DISCOVERY POD
## Paste this entire document into IBM Bob

You are IBM Bob acting as the Discovery Pod Engineer for Interlock.

READ `docs/prompts/00_SHARED_TEAM_CONTRACT.md` FIRST.

## Mission
Build four discovery agents and five real fixture Git repositories.

The critical demo requirement is that Analytics Worker is discovered from source code, not hardcoded.

## First action
Start in Plan Mode. Inspect the repository and existing contracts before coding. Reuse shared Pydantic models. Do not create duplicate schemas.

## You own
```text
agents/discovery/
fixtures/
tests/discovery/
```

Do not redesign `orchestrator/`.

Do not write SQLite directly.

Do not call other agents.

## Four agents
1. repo-map
2. api-contract-discovery
3. event-contract-discovery
4. db-schema-discovery

All return strict structured JSON/Pydantic results.

## repo-map
Inspect all fixture repositories and identify:
- components
- source files
- OpenAPI specs
- event-related files
- DB/schema/migration files
- field references

Use actual filesystem inspection, pathlib, AST, grep/search and Git as appropriate.

## api-contract-discovery
Inspect OpenAPI/API specs and actual consumer code where useful.

Must discover documented:
- account-service -> checkout
- account-service -> fraud

Use real source references. Never invent line numbers.

## event-contract-discovery — highest priority
Inspect actual source code for event consumers.

Analytics Worker must contain genuine source like:
`event["customer_id"]`

There must be no documentation telling the agent this is a dependency.

Detect it through AST/search/source inspection.

The result must identify:
`account-service -> analytics-worker`
with `edge_type = event` or an appropriate equivalent.

Do not hardcode "analytics-worker" as an expected result.

Create a regression test proving that if the source reference is removed, the dependency is no longer discovered.

## db-schema-discovery
Inspect platform-config and relevant schema/migration files for `customer_id` references.

Produce dependency evidence with real source references.

## Fixtures
Create actual small Git repos:

### account-service
- FastAPI
- owns customer_id/account_id transition
- OpenAPI
- event emission simulation
- tests
- Dockerfile

### checkout
- documented Account Service consumer
- uses customer_id initially
- tests
- Dockerfile

### fraud
- documented Account Service consumer
- uses customer_id initially
- tests
- Dockerfile

### analytics-worker
- event consumer
- directly accesses customer_id in source
- intentionally undocumented
- tests
- Dockerfile

### platform-config
- migration/schema files referencing customer_id
- tests if useful
- Dockerfile only if needed

Keep them tiny and realistic.

## Discovery tests
Prove:
1. repo-map finds all five
2. API discovery finds Checkout
3. API discovery finds Fraud
4. event discovery finds Analytics Worker
5. DB discovery finds platform-config
6. source references are real
7. removing Analytics source usage removes that dependency
8. outputs validate

## Evidence quality
Every dependency claim must include:
- subject
- structured content
- real source_ref
- confidence
- source revision when practical

Never fabricate evidence.

## Git
Branch: `feature/discovery`

Use logical commits:
- feat: add fixture repositories
- feat: add repo map discovery
- feat: add API contract discovery
- feat: add event contract discovery
- feat: add DB schema discovery
- test: add discovery regression tests

Push branch and open PR.

## Definition of Done
- all four agents work
- five real fixture repos exist
- Analytics Worker is mechanism-discovered
- shared schemas validate
- tests pass
- no SQLite writes
- no agent-to-agent calls
- PR ready
