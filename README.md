# Interlock

Agentic change-safety control plane for safe cross-service migrations.
Built for IBM Dev Day 2026 (48-hour hackathon).

## Start here
Read these in order before writing any code:
1. `docs/prompts/00_SHARED_TEAM_CONTRACT.md`
2. Your role's numbered prompt in `docs/prompts/`
3. `docs/prompts/06_TEAM_INTEGRATION_AND_MERGE_GUIDE.md`
4. `docs/prompts/07_48_HOUR_EXECUTION_CHECKLIST.md`

## Structure
- `orchestrator/` - ledger, state machine, deterministic gate, FastAPI (Person 1)
- `agents/discovery/` - repo-map, api-contract, event-contract, db-schema (Person 2)
- `agents/planning/` + `agents/implementation/` - strategy, provider/consumer patch (Person 3)
- `agents/verification/` - contract-test, coexistence-rehearsal, critic (Person 4)
- `frontend/` - Streamlit UI (Person 5)
- `fixtures/` - five real demo repos (account-service, checkout, fraud, analytics-worker, platform-config)

## Golden rules
- Agents never write SQLite directly and never call other agents.
- The orchestrator is the only ledger writer.
- The safety gate is deterministic Python, never an LLM decision.
- Analytics Worker must be discovered from source, never hardcoded.
