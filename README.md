# Interlock

**A change-safety control plane for breaking cross-service changes.**

Renaming a field that other services depend on is easy to ship and hard to prove
safe. The consumers you know about are in the API docs; the one that breaks
production reads the field straight out of the source and was never documented
anywhere.

Interlock takes a proposed change — the demo case is `customer_id -> account_id`
on `account-service` — and runs agents to discover every consumer, migrate them,
and verify them. A **deterministic gate** then decides whether removing the
legacy field is provably safe. No LLM can override that gate, and no human can
approve past it while a consumer is unverified.

---

## Quick start

Two processes: a FastAPI backend and a Streamlit UI that talks to it over HTTP.

**1. Activate the virtual environment**

PowerShell:

```bash
.\.venv\Scripts\Activate.ps1
```

bash / git-bash:

```bash
source .venv/Scripts/activate
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Start the backend** (terminal 1)

```bash
uvicorn orchestrator.main:app --reload
```

Serves `http://localhost:8000`. Interactive API docs at http://localhost:8000/docs.

**4. Start the frontend** (terminal 2)

```bash
streamlit run frontend/streamlit_app.py
```

Opens `http://localhost:8501`.

> Run this from the **repository root**, not from `frontend/` — that is how
> `.streamlit/config.toml` (the demo theme) gets picked up.

The UI ships **light and dark** palettes and follows the viewer's choice under
`☰ → Settings → Appearance` (or their system preference). Switching the theme
mid-session settles on the next interaction — press **Refresh now** in the
sidebar if a panel looks a step behind.

---

## The demo

1. Sidebar → **Start change request** (defaults to `customer_id -> account_id`)
2. The agent feed reports discovery. `analytics-worker` surfaces as a
   **hidden dependency** — found in source, absent from the API contract — and
   is drawn as a dashed amber edge in the dependency graph.
3. The gate reads **PENDING**; migration progress is 0/4.
4. **Approve coordination plan** → provider patch, consumer migrations,
   coexistence rehearsal, contract tests, critic.
5. With Docker available, the gate flips to **VERIFIED**, 4/4 consumers verified.
   Without Docker, it correctly remains at `REHEARSE`; install/start Docker and
   use **Resume workflow** rather than treating a skipped rehearsal as proof.
6. **Approve legacy field removal** → state `DONE`, and the **Change Passport**
   summarises the whole change with evidence behind every line.

Full narrative in [docs/demo_script.md](docs/demo_script.md).

---

## How the pieces connect

```
Streamlit (:8501)  ──HTTP──▶  FastAPI (:8000)  ──▶  ledger.py  ──▶  interlock.db
                                     │
                                     ├── state machine   (INTAKE … DONE)
                                     ├── agent phases    (agents/)
                                     └── deterministic gate + NetworkX graph
```

The UI shares no state with the backend — no imports across the boundary, no
database access. It is a pure HTTP client and calls exactly these endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /change-requests` | Create a change; runs agents up to the first human gate |
| `GET  /change-requests/{id}` | Current workflow state |
| `GET  /change-requests/{id}/evidence` | Evidence ledger rows |
| `GET  /change-requests/{id}/graph` | Dependency graph (nodes + typed edges) |
| `GET  /change-requests/{id}/gate` | Deterministic gate decision + consumer status |
| `GET  /change-requests/{id}/approvals` | Recorded human approvals |
| `POST /change-requests/{id}/approve` | Approve the `coordinate` or `legacy_removal` gate |
| `POST /change-requests/{id}/resume` | Retry an interrupted `MODIFY` or `REHEARSE` phase |

### Workflow states

`INTAKE → DISCOVERY → PLANNING → COORDINATE → MODIFY → REHEARSE → VERIFY →
GATE_DECISION → APPROVE → DONE`

Two states wait on a human: `COORDINATE` (before any code is modified) and
`APPROVE` (before the legacy field is removed). Agents never auto-approve either.

---

## Configuration

| Variable | Read by | Default |
| --- | --- | --- |
| `ORCHESTRATOR_API_URL` | frontend | `http://localhost:8000` |
| `INTERLOCK_API_URL` | frontend (fallback) | — |
| `INTERLOCK_DB_PATH` | backend | `interlock.db` |

The backend URL can also be changed live in the UI sidebar without a restart.

To run the backend against a scratch database:

```bash
$env:INTERLOCK_DB_PATH="scratch/demo.db"; uvicorn orchestrator.main:app --reload
```

---

## Tests

Fast, hermetic suites — no running backend required, and they do not touch
`interlock.db`:

```bash
python -m pytest tests/frontend tests/orchestrator -q
```

Full suite (~2 minutes):

```bash
python -m pytest -q
```

`pytest.ini` sets `--basetemp=.pytest_tmp` to avoid Windows temp-permission

The root suite excludes `fixtures/` because each fixture is a standalone test
repository with its own `tests` package. Run a fixture from inside its directory,
or let the contract-test agent / Docker rehearsal run it. Real workflow edits
occur in a per-change temporary workspace; the checked-out fixtures stay clean.

---

## Layout

| Path | Role |
| --- | --- |
| `orchestrator/` | FastAPI app, ledger, state machine, deterministic gate |
| `orchestrator/db/schema.sql` | Canonical SQLite schema |
| `orchestrator/schemas/` | Shared Pydantic contracts |
| `agents/` | Discovery, planning, implementation, verification agents |
| `fixtures/` | Fixture repositories the agents operate on |
| `frontend/` | Streamlit UI (pure view layer) |
| `tests/` | pytest suite, mirroring the package layout |
| `docs/` | Architecture, demo script, per-workstream prompts |

Contributing to this codebase — including the invariants that keep the safety
claim honest — is documented in [AGENTS.md](AGENTS.md).

---

## 🔒 Security Features

This template includes:

- **`.gitignore`** - Prevents committing credentials and live session files
- **`.bobignore`** - Prevents AI assistants from logging credentials
- **`.env.example`** - Template for your environment variables

Set up your own environment file:

```bash
cp .env.example .env
```

Then confirm it is actually ignored:

```bash
git check-ignore -v .env
```

## 📋 Before Every Commit

Always run this checklist:

- [ ] Reviewed `git diff` for sensitive data
- [ ] No hardcoded API keys or passwords
- [ ] `.env` file is NOT in staged changes
- [ ] No files with "credential" or "secret" in name
- [ ] Used environment variables for all credentials

## 🆘 Need Help?

- Read [SECURITY.md](SECURITY.MD) for detailed guidelines
- Contact hackathon support through mentor channel
- Ask in the hackathon Slack workspace

---

**Remember:** Security is everyone's responsibility. When in doubt, ask for help!
