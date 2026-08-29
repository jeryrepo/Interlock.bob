# Interlock demo script

The story: a field rename that looks routine, a consumer nobody documented, and
a gate that will not let the change ship until that consumer is proven safe.

> **Honesty note.** While `orchestrator/agent_runner.py` has `STUB_MODE = True`,
> the run below is driven by seeded data rather than live discovery. The
> narrative and the gate logic are real; the evidence is not yet. Do not present
> a stubbed run as a live one — see [architecture.md](architecture.md).

---

## Setup

Two terminals, from the repository root.

```bash
uvicorn orchestrator.main:app --reload
```

```bash
streamlit run frontend/streamlit_app.py
```

Run the UI from the repository root so `.streamlit/config.toml` is picked up.
The UI opens on `http://localhost:8501`; the API is on `http://localhost:8000`
with interactive docs at `/docs`.

---

## Beat 1 — an ordinary-looking change

Sidebar → **Start change request**. The default description is
`customer_id -> account_id` on `account-service`.

Say out loud what makes this hard: the rename itself is trivial. The risk is
entirely in *who else reads that field*, and the honest answer is that nobody
knows. The API docs list the consumers somebody remembered to write down.

## Beat 2 — discovery finds what the contract does not

The activity feed reports the discovery agents. Three consumers surface:

- **checkout** — documented API consumer, found in the OpenAPI spec
- **fraud** — documented API consumer
- **analytics-worker** — **not** in any contract

`analytics-worker` is the beat that matters. It reads `event["customer_id"]`
directly out of an event payload inside a handler function. There is no API
spec, no service-registry entry, and no comment linking it to
`account-service`. It was found by walking the AST of real source code.

In the dependency graph it is drawn as a **dashed amber edge** — an
`undocumented` dependency. Point at it. This is the service that would have
broken production on a Friday.

## Beat 3 — the gate says no

The gate panel reads **PENDING**. Migration progress is **0 / 3**.

Worth stating plainly: at this point the gate is not being cautious, it is being
literal. Zero consumers have been proven safe, so the answer is
`NOT_PROVEN_SAFE`. The gate has no opinion, no confidence score, and no model
behind it.

## Beat 4 — human approves the plan, agents do the work

**Approve coordination plan.** This is the first of two human gates, and it comes
*before* any code is touched.

The planning agent has ordered the migration — consumers that depend through the
database schema go last. Then:

- **provider-patch** adds `account_id` alongside `customer_id`, keeping both
  during the coexistence window, and commits for real
- **consumer-migration** migrates each consumer in order, running that
  repository's own tests and committing for real
- **coexistence-rehearsal** brings the services up together and proves the new
  provider still serves un-migrated consumers
- **contract-test** runs real pytest against provider and consumers
- **critic** reviews evidence *quality* — stale tests, migrations claimed without
  a commit SHA, missing verification

Note what the critic is not doing: deciding. It emits `risk` evidence. If it
flagged everything as fine, the gate would be unmoved.

## Beat 5 — the gate flips

**VERIFIED**, 3 / 3 consumers verified. Every line is backed by evidence with a
source reference and a commit SHA.

The demo lands here: the gate flipped because a topological fact changed, not
because a model was convinced.

## Beat 6 — legacy removal, and the second gate

**Approve legacy field removal** → state `DONE`.

Worth demonstrating the refusal if time allows: the API re-evaluates the gate
independently when this approval arrives, and returns `409` if the change is not
`VERIFIED`. A human cannot click past an unverified consumer.

## Beat 7 — the Change Passport

The passport summarises the whole change with evidence behind every line: what
was found, in what order it was migrated, which tests ran at which revision, who
approved what and when.

Close on the claim: *nothing shipped until every consumer was proven safe — and
here is the proof.*

---

## If something goes wrong

- **Gate stuck on PENDING** — check the state; it reads `PENDING` with
  `decided: false` until the orchestrator writes a `gate_decision` row. That is
  correct behaviour, not a hang.
- **UI a step behind after a theme switch** — press **Refresh now** in the
  sidebar. Streamlit settles the theme on the next interaction.
- **Backend chip red** — the UI probes `/openapi.json`; confirm uvicorn is up and
  `ORCHESTRATOR_API_URL` matches.
