# Interlock demo script

## Preparation

1. Install `requirements.txt` and make sure Docker Desktop is running.
2. From the repository root, start `uvicorn orchestrator.main:app --reload`.
3. Start `streamlit run frontend/streamlit_app.py` in a second terminal.
4. Confirm the backend indicator is online before recording.

## Walkthrough

1. Create the default `customer_id -> account_id` change request.
2. Show the workflow paused at `COORDINATE`. Point out the documented API
   consumers, database consumer, and dashed amber `analytics-worker` event edge.
   Its undocumented status comes from source evidence, not a UI constant.
3. Open the evidence feed and explain that the graph is provider → consumer and
   the gate is still `PENDING`.
4. Approve the coordination plan. The response schedules work from `MODIFY`;
   keep the page polling while isolated fixture copies are patched and tested.
5. Show the coexistence rehearsal and each consumer's real pytest evidence.
   When all four consumers are proven, the recorded gate becomes `VERIFIED` and
   the workflow waits at `APPROVE`.
6. Approve legacy-field removal and show the final `DONE` passport.

## Honest failure path

If Docker is unavailable, the workflow stops at `REHEARSE` with a failed test
result; do not hide or narrate it as a pass. Start Docker, click **Resume
workflow**, and wait for the new confirmed rehearsal. Any consumer test failure
similarly keeps the deterministic gate at `NOT_PROVEN_SAFE`.
