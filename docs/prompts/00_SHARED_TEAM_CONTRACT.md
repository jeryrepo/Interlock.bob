# INTERLOCK — SHARED TEAM CONTRACT
## IBM Dev Day 2026 | 48-Hour Build

This document is mandatory reading for every teammate before asking IBM Bob to code.

## Product
Interlock is an agentic change-safety control plane for safe cross-service migrations.

Demo change:
`customer_id -> account_id`

Core story:
1. Developer proposes a breaking change.
2. Discovery agents inspect five real fixture repositories.
3. A hidden Analytics Worker dependency is discovered from source code.
4. Planning creates a safe migration order.
5. Implementation agents modify real repositories and create real Git commits.
6. Verification runs real pytest/Docker checks.
7. Critic inspects evidence quality.
8. A deterministic Python gate decides whether all required consumers are verified.
9. Human approvals are required at the defined gates.
10. Streamlit presents the live story.

## Golden Rules
- IBM Bob is the required coding/agent-development environment.
- Agents never call other agents.
- Agents never write SQLite directly.
- Agents return strict structured results.
- The FastAPI orchestrator validates agent results and is the only ledger writer.
- The state machine is persisted, not in-memory only.
- The final safety gate is deterministic Python, not an LLM decision.
- The graph is derived from `dependency_edge`; do not maintain a second source of truth.
- Analytics Worker must be discovered from actual source code, never hardcoded.
- Real fixture repositories, real tests, and real Git commits are required.
- No silent agent failures: retry once on validation failure, then fail loudly.
- Do not add Neo4j, Temporal, Kafka, Kubernetes, OTel, OPA, GitHub Actions, auth, or dashboards in the MVP.

## Runtime Agents — 10
1. repo-map
2. api-contract-discovery
3. event-contract-discovery
4. db-schema-discovery
5. compatibility-strategy
6. provider-patch
7. consumer-migration
8. contract-test
9. coexistence-rehearsal
10. critic

## Workflow
`INTAKE -> DISCOVERY -> PLANNING -> COORDINATE -> MODIFY -> REHEARSE -> VERIFY -> GATE_DECISION -> APPROVE -> DONE`

## Five Fixture Repositories
- `fixtures/account-service/` — provider, owns `customer_id -> account_id`, OpenAPI
- `fixtures/checkout/` — documented API consumer
- `fixtures/fraud/` — documented API consumer
- `fixtures/analytics-worker/` — undocumented event consumer; source contains direct `customer_id` access
- `fixtures/platform-config/` — migration/schema references

## Repository Ownership
- Person 1: `orchestrator/`
- Person 2: `agents/discovery/`, `fixtures/`, discovery tests
- Person 3: `agents/planning/`, `agents/implementation/`, related tests
- Person 4: `agents/verification/`, Docker Compose, verification tests
- Person 5: `frontend/` Streamlit UI

Use feature branches. Do not overwrite another person's work. Prefer PRs and small logical commits.

## Integration Protocol
1. Person 1 creates the shared contracts and stub workflow first.
2. Other teammates can work immediately using those contracts/mocks; they do not wait for full backend completion.
3. Before merging a PR, run the complete test suite.
4. Check that no agent writes the ledger directly.
5. Check that no agent calls another agent.
6. Check that no dependency is hardcoded.
7. Merge one feature branch at a time into `main`.
8. After each merge, run integration tests.
9. Final integration happens before demo rehearsal.

## Important Shared Interfaces
Use existing repository definitions if they already exist. Do not create duplicate Pydantic models.

Conceptual evidence:

```python
class Evidence(BaseModel):
    claim_type: Literal["dependency", "migration_status", "test_result", "risk"]
    subject: str
    content: dict
    source_ref: str
    confidence: Literal["hypothesis", "confirmed", "refuted"]
    source_revision: str | None = None
```

Conceptual dependency:

```python
class Dependency(BaseModel):
    from_component: str
    to_component: str
    edge_type: Literal["api", "event", "db", "undocumented"]
    documentation_status: Literal["documented", "undocumented"] = "documented"
    reason: str | None = None
```

The canonical direction is **provider → consumer**. For example,
`account-service → checkout`. Mechanism (`edge_type="event"`) and documentation
status are separate facts; the hidden analytics edge is an undocumented event
edge, not a replacement edge type.

Every agent returns a validated result object. Exact final schemas belong to Person 1.

## Demo Safety
Have a deterministic fallback recording/demo dataset ready, but do not use it to fake discovery during the primary live path.
