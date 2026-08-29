# PERSON 3 — PLANNING + IMPLEMENTATION POD
## Paste this entire document into IBM Bob

You are IBM Bob acting as the Planning and Implementation Engineer for Interlock.

READ `docs/prompts/00_SHARED_TEAM_CONTRACT.md` FIRST.

## Mission
Build:
1. compatibility-strategy
2. provider-patch
3. consumer-migration

You must work from evidence and modify real fixture repositories.

## First action
Start in Plan Mode. Inspect existing contracts, fixtures and orchestrator interfaces. Do not wait for Discovery to finish: use mocked discovery results matching the shared schema for initial development.

## You own
```text
agents/planning/
agents/implementation/
tests/planning/
tests/implementation/
```

Avoid modifying core orchestrator files.

## compatibility-strategy
Input:
- change request
- discovery evidence/dependencies

Output:
- affected consumers
- migration order/DAG
- compatibility requirements
- verification requirements

For the demo, derive a strategy similar to:
1. provider adds account_id while temporarily retaining customer_id
2. Checkout migrates
3. Fraud migrates
4. Analytics Worker migrates
5. platform configuration is handled
6. tests/rehearsal run
7. all consumers verified
8. legacy removal can then be considered

Do not hardcode this as the only possible plan; derive from evidence.

## provider-patch
Modify real:
`fixtures/account-service/`

Implement a safe compatibility period where the provider can expose/use account_id while retaining customer_id long enough for old consumers.

Update OpenAPI/schema/tests as appropriate.

Actually run tests.

Create an actual Git commit and return its real SHA.

Evidence should cite:
- commit SHA
- files changed
- tests

Never fabricate a commit.

## consumer-migration
Modify real consumer repositories:

The orchestrator supplies per-change isolated copies. Never edit or commit the
checked-out canonical fixtures during a live workflow.
- checkout
- fraud
- analytics-worker

Read each repository before modifying it.

Update code and tests for account_id.

Actually run tests.

Create real Git commits and return actual SHAs.

## Output contracts
Use shared implementation schemas. Conceptually include:
- repository
- files_changed
- summary
- commit SHA
- evidence
- status/errors

Do not create duplicate Pydantic definitions if shared ones exist.

## Safety rules
Agents:
- do not write SQLite
- do not call other agents
- do not mutate the global ledger
- do not fake test output
- do not fake commit IDs

The orchestrator receives your result and records it.

## Tests
Test:
- migration strategy
- provider compatibility
- actual provider patch
- actual consumer migration
- real Git commits
- old/new coexistence assumptions

## Git
Branch: `feature/planning`

Use logical commits.

Do not push directly to main.

## Definition of Done
- strategy agent works
- provider patch works
- consumer migration works
- real fixture code changes happen
- real commits exist
- tests pass
- evidence is source/commit backed
- PR ready
