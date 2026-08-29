# Interlock

**A change-safety control plane for breaking changes across services.**

You are about to rename a field, drop an API response key, or move event
delivery off webhooks. The consumers you know about are in the API docs. The one
that takes production down on Friday reads the field straight out of the source
and was never documented anywhere.

Interlock finds that one. Then it migrates every consumer, runs their real test
suites, and a **deterministic gate** decides whether the change is provably safe.
No model can override that gate, and no human can approve past it while a
consumer is unverified.

---

## Who this is for

**Engineers making a breaking change in a microservice estate** — where the
dependency graph is only partly written down, and "who calls this?" has no
trustworthy answer.

You are the target user if any of these are true:

- you are renaming a database column or model field other services read;
- you are changing a published API contract;
- you are moving inter-service delivery from webhooks to pub/sub;
- you have ever shipped a change that was fine in CI and broke a service nobody
  remembered existed.

Also for **platform and SRE teams** who want that check to be a blocking gate
rather than a code-review convention, and for **AI coding agents** — Interlock
ships as an MCP server so the agent proposing the change can check it first.

## Why use it

**Because the dangerous consumers are the undocumented ones.** Grep finds the
callers you already know about. Interlock reads source with an AST and surfaces
the ones that appear in no contract, no config, and no README.

**Because "the tests passed" is not the same as "it is safe".** Each service's
suite passing in isolation says nothing about a provider serving old and new
consumers simultaneously. Interlock proves the coexistence window by running the
provider for real and asking it what it serves.

**Because the verdict cannot be talked out of.** The gate is pure Python with
zero model involvement. An agent can read it and cannot influence it. That is
the point of the whole system: when it says VERIFIED, something checked, and the
evidence has a git SHA attached.

**Because it meets you where you already are.** A terminal command, a PR
comment, or a tool your coding agent calls — not another dashboard to remember
to visit.

**What it is not:** a linter, a test runner, or a migration framework. It
orchestrates the ones you already have and decides whether the result is safe.

## How to use it

Three surfaces over one engine, so they cannot disagree:

| You are... | Use |
| --- | --- |
| at a terminal, before opening a PR | `interlock check` |
| reviewing a pull request | the GitHub Action, or `interlock review` |
| an AI coding agent (IBM Bob, Claude Code, Cursor) | the bundled MCP server |
| demoing or exploring the evidence | the FastAPI + Streamlit UI |

The shortest path:

```bash
pip install -e .
interlock check --old customer_id --new account_id --provider account-service
```

It exits `0` when every consumer is proven safe and `1` when one is not, so it
drops straight into a pre-push hook or a CI step. Everything below expands on
that.

---

## Quick start

Three ways in: a CLI, an MCP server for coding agents, and the API + Streamlit UI.

### As a CLI (no server required)

```bash
pip install -e .
```

```bash
interlock check --old customer_id --new account_id --provider account-service
```

Discovers every consumer, migrates and verifies them on an isolated copy of the
component tree, and prints the deterministic verdict. **Exits non-zero when the
change is not proven safe**, so it works as a pre-push hook or a CI step:

```bash
interlock check --old customer_id --new account_id --provider account-service || echo "blocked"
```

Other commands: `start`, `approve`, `gate`, `status`, `list`, `evidence`,
`review`, `agents`. Add `--json` to any of them for machine-readable output.

### The three kinds of change it covers

Renaming a field is one instance of a wider problem: touching something in a
microservice estate that other services depend on, where the dependency graph is
only partly written down.

```bash
# a database / model field
interlock check --kind field_rename \
  --old customer_id --new account_id --provider account-service

# a published API contract
interlock check --kind api_contract_change \
  --old customer_id --new account_id --provider account-service

# an inter-service call: webhook delivery -> pub/sub
interlock check --kind transport_migration \
  --old deliver_via_webhook --new deliver_via_pubsub \
  --provider event-publisher --components-root fixtures_transport
```

The transport case needs **two** proofs per subscriber, not one: that it moved
to pub/sub, and that it has actually drained off the retired webhook. A
subscriber that moved but still sends webhook traffic is not safe, and the gate
says so.

See how it is wired, per kind:

```bash
interlock agents
```

### Reviewing a pull request

```bash
interlock review --run --old customer_id --new account_id --provider account-service
```

Prints the exact markdown the GitHub Action posts on a PR — the verdict, the
blocking components, and any consumer found only by reading source. Exits
non-zero on `NOT_PROVEN_SAFE`, so it works as the blocking check itself.
`.github/workflows/interlock.yml` runs this on every pull request.

### As a tool for IBM Bob and other coding agents

The repository ships `.bob/mcp.json` (IBM Bob) and `.mcp.json` (Claude Code,
Cursor, Copilot), both pointing at the bundled MCP server over stdio. Clone the
repo, `pip install -e ".[mcp]"`, and the agent can call `interlock_check`,
`interlock_gate`, `interlock_evidence` and `interlock_dependency_graph`
directly.

An agent can *read* the verdict but never influence it: there is no tool to
override the gate or approve legacy removal.

### As a service

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
3. The gate reads **PENDING**; migration progress is 0/3.
4. **Approve coordination plan** → provider patch, consumer migrations,
   coexistence rehearsal, contract tests, critic.
5. The gate flips to **VERIFIED**, 3/3 consumers verified.
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

> `.env.example` currently lists `ORCHESTRATOR_DB_PATH`, but the code reads
> `INTERLOCK_DB_PATH` (`orchestrator/main.py`). Trust the table above.

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

The suite should be fully green. If it is not, check the note below before
assuming a product bug.

**Do not add `__init__.py` to a `tests/` subdirectory.** `tests/` itself has
none, so adding one to a subdirectory makes pytest name the module
`verification.test_critic` instead of `tests.verification.test_critic`, which
only resolves when that directory is run on its own. The result looks like a
test-ordering problem and is not one. See `AGENTS.md` for the measured details.

Related: never `import` a `conftest.py` directly — use the fixtures it exposes.
Four tests failed this way until 2026-08-29.

`pytest.ini` sets `--basetemp=.pytest_tmp` to avoid Windows temp-permission
errors; that directory is gitignored.

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
