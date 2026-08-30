# Interlock judge demo

This script is designed for a short, evidence-first demonstration. Do not
present a skipped rehearsal, missing consumer, or failed test as successful.

## Preflight

From the repository root:

```powershell
python -m pip install -r .\requirements.txt
python -m pip check
python -m pytest -q
```

Run every fixture suite separately using the commands in the README. If any
command fails, stop and repair the baseline before recording the demo.

Start the backend:

```powershell
$env:INTERLOCK_DB_PATH="scratch/demo.db"
uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000
```

Start the UI from the repository root in a second terminal:

```powershell
$env:ORCHESTRATOR_API_URL="http://127.0.0.1:8000"
streamlit run frontend/streamlit_app.py
```

Confirm that the backend reports connected before beginning.

## Demonstration

1. **State the problem.** A provider field rename can break consumers that are
   absent from the published API contract. Interlock will not authorise legacy
   removal until required consumers are proven safe.
2. **Create the change.** Start the default `customer_id -> account_id` request.
3. **Show discovery evidence.** Point to source references, the dependency
   graph, and any consumer found outside the published contract. If the seeded
   hidden consumer is missing, stop: the fixture baseline is not demo-ready.
4. **Show the first human gate.** The workflow waits at `COORDINATE`; no code
   modification occurs until the plan is approved.
5. **Approve coordination.** Follow the activity feed through provider patch,
   consumer migrations, coexistence rehearsal, contract tests, and critic.
6. **Inspect proof before verdict.** Open the evidence ledger and verify that
   test output and source revisions are recorded. A skipped or unavailable
   rehearsal is not proof and must not be described as one.
7. **Show the deterministic gate.** Explain that the verdict is computed by
   read-only Python from ledger facts and cannot be overridden by an agent or
   the frontend.
8. **Approve legacy removal.** Only after the backend records `VERIFIED`, use
   the second human gate and show the final Change Passport.

## Judge talking points

- Discovery uses repository, OpenAPI, event, and database-schema evidence.
- Agents exchange validated results through the orchestrator rather than
  calling each other.
- The UI is a pure HTTP client; it does not duplicate the gate.
- Human approval is required before modification and before legacy removal.
- IBM Bob was used as the coding and agent-development environment; the prompt
  contracts are preserved under `docs/prompts/` for reproducibility.

## Failure rule

The demo is ready only when the product suite, all fixture suites, backend
startup, frontend startup, discovery graph, and verification evidence agree.
If they do not, report the change as not proven safe.
