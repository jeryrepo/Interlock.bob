# INTERLOCK - 48-HOUR EXECUTION CHECKLIST

## Hour 0-3
### Person 1
- [ ] inspect repo
- [ ] create shared contract
- [ ] create SQLite schema
- [ ] ledger
- [ ] Pydantic schemas
- [ ] state machine

### Person 2
- [ ] inspect repo
- [ ] create fixture repos
- [ ] make Analytics Worker genuinely undocumented
- [ ] begin event discovery

### Person 3
- [ ] inspect contracts
- [ ] compatibility strategy
- [ ] provider patch skeleton
- [ ] consumer migration skeleton

### Person 4
- [ ] verification contract
- [ ] pytest harness
- [ ] Compose skeleton
- [ ] critic skeleton

### Person 5
- [ ] Streamlit shell
- [ ] mock API client
- [ ] state/evidence panels

## Hour 3-8
- [ ] Person 1 completes stub end-to-end workflow
- [ ] Person 2 proves Analytics discovery with tests
- [ ] Person 3 proves real fixture edits/commits
- [ ] Person 4 proves real pytest execution
- [ ] Person 5 connects mock UI to backend shape

## Hour 8-16
- [ ] merge discovery
- [ ] integrate real discovery
- [ ] merge planning/implementation
- [ ] integrate real commits
- [ ] merge verification
- [ ] integrate real tests/rehearsal

## Hour 16-24
- [ ] Streamlit connects to real API
- [ ] graph endpoint works
- [ ] gate blocks unresolved consumer
- [ ] gate becomes VERIFIED after verification
- [ ] approvals work

## Hour 24-36
- [ ] complete end-to-end run
- [ ] fix integration bugs
- [ ] add retry/error behavior
- [ ] improve evidence source refs
- [ ] improve critic stale-evidence detection

## Hour 36-44
- [ ] rehearse 4-minute demo
- [ ] remove unnecessary features
- [ ] improve UI readability
- [ ] pre-warm Docker
- [ ] prepare deterministic fallback

## Hour 44-48
- [ ] freeze architecture
- [ ] final full test
- [ ] fresh clone/run test if possible
- [ ] rehearse 3+ times
- [ ] prepare demo script
- [ ] prepare backup recording

## Absolute MVP
If only a few things can be completed:
- [ ] five real fixture repos
- [ ] hidden Analytics Worker discovered by source inspection
- [ ] evidence ledger
- [ ] deterministic gate
- [ ] at least one real migration
- [ ] real verification
- [ ] Streamlit story
- [ ] human approval

## Do not spend hackathon time on
- [ ] Neo4j
- [ ] Kafka
- [ ] Temporal
- [ ] Kubernetes
- [ ] OTel
- [ ] OPA
- [ ] auth
- [ ] CI/CD
- [ ] enterprise dashboards
