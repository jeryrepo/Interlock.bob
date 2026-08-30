# Verify all the IBM credentials are correctly called and i only will provide the watsonx.ai so will there be llm calls made

---

**Status:** active  **Date:** 2026-08-30

---

### 👤 User

Verify all the IBM credentials are correctly called and i only will provide the watsonx.ai so will there be llm calls made

---

### 🤖 Assistant

> 🔧 called: grep, read_file, read_file, read_file

Let me investigate how IBM credentials are used throughout the codebase.

> 🔧 **read_file** — .env.example

> 🔧 **grep** — IBM|watsonx|WATSON|ibm|WX_|wx_

> 🔧 **read_file** — orchestrator/settings.py

> 🔧 **read_file** — orchestrator/watsonx.py

### 🤖 Assistant

> 🔧 called: read_file, read_file

Now let me read the other IBM-related files:

> 🔧 **read_file** — orchestrator/external_agent.py

> 🔧 **read_file** — interlock_mcp/server.py

### 🤖 Assistant

> 🔧 called: read_file, grep

Now let me check the MCP http module and the CLI to complete the picture:

> 🔧 **read_file** — interlock_mcp/http.py

> 🔧 **grep** — IBM_CLOUD_API_KEY|WATSONX_PROJECT_ID|WATSONX_SPACE_ID|WATSONX_MODEL_ID|WATSONX_URL|IBM_CLOUD_IAM_URL|WATSONX_ORCHESTRATE|INTERLOCK_EXTERNAL_AGENT_KEY|INTERLOCK_ENABLE_NARRATION

### 🤖 Assistant

> 🔧 called: grep, read_file

Now let me check the security review agent to see how it uses settings, and also the `interlock_mcp/__init__.py`:

> 🔧 **read_file** — interlock_mcp/__init__.py

> 🔧 **grep** — agents/verification/security_review.py

### 🤖 Assistant

I now have a complete picture. Here's a clear breakdown:

---

## IBM Credential Verification

### All credentials read from the environment in [`orchestrator/settings.py`](orchestrator/settings.py)

The `load()` function is the single source of truth. Every variable maps cleanly:

| Env var | Mapped to | Default |
|---|---|---|
| `IBM_CLOUD_API_KEY` | `WatsonxSettings.api_key` | `""` |
| `WATSONX_URL` | `WatsonxSettings.url` | `https://us-south.ml.cloud.ibm.com` |
| `WATSONX_PROJECT_ID` | `WatsonxSettings.project_id` | `""` |
| `WATSONX_SPACE_ID` | `WatsonxSettings.space_id` | `""` |
| `WATSONX_MODEL_ID` | `WatsonxSettings.model_id` | `ibm/granite-4-h-small` |
| `WATSONX_MAX_NEW_TOKENS` | `WatsonxSettings.max_new_tokens` | `300` |
| `WATSONX_API_VERSION` | `WatsonxSettings.api_version` | `2024-10-08` |
| `IBM_CLOUD_IAM_URL` | `WatsonxSettings.iam_url` | `https://iam.cloud.ibm.com/identity/token` |
| `INTERLOCK_ENABLE_NARRATION` | `WatsonxSettings.narration_enabled` | `False` |
| `WATSONX_ORCHESTRATE_INSTANCE_URL` | `OrchestrateSettings.instance_url` | `""` |
| `WATSONX_ORCHESTRATE_API_KEY` | `OrchestrateSettings.api_key` | `""` |
| `WATSONX_ORCHESTRATE_ENV` | `OrchestrateSettings.env_name` | `hackathon` |
| `INTERLOCK_EXTERNAL_AGENT_KEY` | `OrchestrateSettings.external_agent_key` | `""` |

Everything is correctly wired — no stray `os.environ.get()` calls in agents or the frontend.

---

### What you actually need to provide (watsonx.ai only)

Since you're providing **only watsonx.ai** credentials, here's exactly what will be called:

**Required — LLM calls will fail silently without these:**
```
IBM_CLOUD_API_KEY=<your key>
WATSONX_PROJECT_ID=<your project id>   # OR WATSONX_SPACE_ID
INTERLOCK_ENABLE_NARRATION=1           # must be set or ALL LLM calls are skipped
```

**Optional (have sensible defaults):**
```
WATSONX_URL=https://us-south.ml.cloud.ibm.com   # default is us-south
WATSONX_MODEL_ID=ibm/granite-4-h-small           # default model
```

---

### What triggers an LLM call

There are exactly **two** places that make watsonx.ai inference calls, both gated behind `settings.watsonx.enabled` (which requires both `INTERLOCK_ENABLE_NARRATION=1` AND `IBM_CLOUD_API_KEY` AND `WATSONX_PROJECT_ID/SPACE_ID`):

1. **[`watsonx.narrate()`](orchestrator/watsonx.py:254)** — called after the deterministic gate decides. Explains the verdict in plain English. Called from:
   - [`orchestrator/external_agent.py:214`](orchestrator/external_agent.py:214) (Orchestrate `/chat/completions` endpoint)
   - [`interlock_mcp/server.py:347`](interlock_mcp/server.py:347) (`interlock_narrate` MCP tool — only when explicitly called)

2. **[`watsonx.review_security()`](orchestrator/watsonx.py:304)** — called from [`agents/verification/security_review.py:561`](agents/verification/security_review.py:561). Supplements pattern-based security scanning with model analysis. Only fires when `settings.watsonx.enabled`.

**The deterministic gate, discovery, planning, implementation, and verification agents make zero LLM calls.** If `INTERLOCK_ENABLE_NARRATION` is not set to `1`, the codebase runs entirely without touching watsonx.ai.

---

### The Orchestrate credentials (`WATSONX_ORCHESTRATE_*`) — not needed

Those are for a *separate* IBM product (watsonx Orchestrate), which lets Interlock act as an external agent inside a supervisor workflow. You don't need them — they're only checked in [`OrchestrateSettings.configured`](orchestrator/settings.py:117) and [`external_agent.py`](orchestrator/external_agent.py) routes. The endpoint 503s gracefully when they're absent.