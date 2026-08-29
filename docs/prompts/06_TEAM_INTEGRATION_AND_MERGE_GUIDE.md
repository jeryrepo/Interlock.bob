# INTERLOCK — TEAM INTEGRATION & MERGE GUIDE
## How the teammates work together

## Do we wait for each other?
NO.

Person 1 builds the backend contract and stub workflow first.

Persons 2-5 work in parallel against the contracts. They can use mocks/stubs until real dependencies are ready.

The only thing everyone must agree on early is the shared Pydantic/API contract.

## Branches
```text
main
feature/orchestrator
feature/discovery
feature/planning
feature/verification
feature/streamlit
```

## Recommended sequence

### Phase 1 - first few hours
Person 1:
- schema.sql
- Pydantic contracts
- ledger
- state machine
- deterministic gate
- API skeleton
- stub workflow

Person 2:
- fixture repos
- discovery logic using local fixtures

Person 3:
- planning and implementation agents using mock discovery

Person 4:
- tests, verification agents and Compose using mock implementation results

Person 5:
- Streamlit UI using mock API responses

### Phase 2
Merge Person 2 discovery first because downstream agents need its real evidence.

Then:
1. discovery
2. planning/implementation
3. verification
4. Streamlit

The exact order can vary if interfaces are stable.

## Pull Request checklist

Before merging any teammate PR:

- [ ] project still starts
- [ ] tests pass
- [ ] no duplicate schemas
- [ ] no direct SQLite access from agents
- [ ] no agent-to-agent calls
- [ ] no hardcoded Analytics Worker dependency
- [ ] no fake Git SHAs
- [ ] no fake test results
- [ ] no breaking API changes
- [ ] changes stay within ownership area unless discussed

## How code is merged

1. Teammate pushes branch.
2. Teammate opens PR into `main`.
3. Person 1 reviews contract/integration impact.
4. Run tests.
5. Merge PR.
6. Pull latest `main` into the next branch before final integration.
7. Run end-to-end tests.

If conflicts occur, do not randomly delete code. Preserve the shared contract and resolve ownership intentionally.

## Agent result flow

Every agent follows:

```text
read scoped context
       v
perform work
       v
return Pydantic result
       v
orchestrator validates
       v
ledger writes evidence
       v
state machine checks ledger
       v
next stage
```

## Final runtime flow

```text
POST change request
       v
INTAKE
       v
Discovery agents
       v
Evidence Ledger
       v
NetworkX graph
       v
Planning agent
       v
Human coordinate approval
       v
Provider + consumer implementation
       v
Contract tests
       v
Docker coexistence rehearsal
       v
Critic
       v
Deterministic gate
       v
Human legacy-removal approval
       v
DONE
```

## 48-hour priority rule

If time becomes tight, prioritize in this order:

1. deterministic gate
2. real Analytics Worker discovery
3. real fixture repositories
4. state machine + ledger
5. real migration commits
6. real pytest verification
7. Streamlit visualization
8. polish

Do not sacrifice the core architecture for visual polish.

## Fallback
Maintain a deterministic demo dataset/recording that can be used if live IBM Bob agent execution is too slow. The fallback must represent results that the real system can actually produce; it must not be presented as live discovery.
