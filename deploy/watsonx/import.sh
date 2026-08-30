#!/usr/bin/env bash
# Register Interlock with watsonx Orchestrate as an MCP toolkit.
#
# Interlock runs on YOUR machine. The hackathon cloud account does not support
# solution deployment (guide p.38), so a tunnel is how the SaaS Orchestrate
# instance reaches your local server. That is "run locally and showcase" as the
# guide describes.
#
# VERIFY THE FLAGS against the ADK version you install — the surface changed at
# 2.0 and these scripts go stale quickly:
#     orchestrate toolkits add --help
#     orchestrate agents import --help
set -euo pipefail

: "${WATSONX_ORCHESTRATE_INSTANCE_URL:?set it in .env}"
: "${WATSONX_ORCHESTRATE_API_KEY:?set it in .env}"
: "${INTERLOCK_EXTERNAL_AGENT_KEY:?set it in .env}"
: "${INTERLOCK_TUNNEL_URL:?the https URL from your tunnel, e.g. https://xxx.trycloudflare.com}"

ENV_NAME="${WATSONX_ORCHESTRATE_ENV:-hackathon}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEPLOY="${INTERLOCK_DEPLOY_AGENT:-1}"

# Prove the tunnel reaches the MCP surface BEFORE registering anything.
#
# Orchestrate performs this exact handshake at import time and allows about 30
# seconds for it. When it fails there you get a toolkit error with no detail;
# here you get the status code and the body. Same request, one layer earlier.
echo "==> checking ${INTERLOCK_TUNNEL_URL%/}/mcp"
probe=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${INTERLOCK_TUNNEL_URL%/}/mcp" \
  -H "x-api-key: $INTERLOCK_EXTERNAL_AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' || echo 000)
case "$probe" in
  200) echo "    ok" ;;
  401) echo "    401: the server has a different INTERLOCK_EXTERNAL_AGENT_KEY than this shell"; exit 1 ;;
  404) echo "    404: no MCP surface. Is INTERLOCK_EXTERNAL_AGENT_KEY set where uvicorn runs?"; exit 1 ;;
  307) echo "    307: register .../mcp exactly, with no trailing slash"; exit 1 ;;
  000) echo "    unreachable: is the tunnel up and INTERLOCK_TUNNEL_URL current?"; exit 1 ;;
  *)   echo "    unexpected HTTP $probe"; exit 1 ;;
esac

echo "==> environment (tokens expire every 2h; re-run activate when they do)"
orchestrate env add -n "$ENV_NAME" -u "$WATSONX_ORCHESTRATE_INSTANCE_URL" \
  --type ibm_iam --activate || true
orchestrate env activate "$ENV_NAME" --api-key "$WATSONX_ORCHESTRATE_API_KEY"

echo "==> connection (carries the shared secret as x-api-key)"
orchestrate connections add -a interlock_conn || true
orchestrate connections configure -a interlock_conn --env draft \
  --type team --kind api_key --name x-api-key
orchestrate connections set-credentials -a interlock_conn --env draft \
  --api-key "$INTERLOCK_EXTERNAL_AGENT_KEY"

echo "==> MCP toolkit"
# `toolkits import` is the pre-2.0 command and takes only -f/--file now; the
# MCP flags live on `toolkits add`.
orchestrate toolkits add --kind mcp --name interlock \
  --description "Deterministic change-safety gate for breaking cross-service changes" \
  --url "${INTERLOCK_TUNNEL_URL%/}/mcp" \
  --transport streamable_http \
  --tools "interlock_check,interlock_gate,interlock_evidence,interlock_dependency_graph,interlock_list_changes" \
  --app-id interlock_conn

echo "==> agent"
# `agents import` lands the agent in DRAFT. A draft agent is not usable in chat,
# which looks exactly like the import having silently done nothing - it is the
# single most common "the demo does nothing" moment. `--deploy` publishes it to
# live in the same step. Set INTERLOCK_DEPLOY_AGENT=0 to keep it in draft and
# publish from the UI instead.
if [ "$DEPLOY" = "1" ]; then
  orchestrate agents import -f "$REPO_ROOT/deploy/watsonx/agent.yaml" --deploy
else
  orchestrate agents import -f "$REPO_ROOT/deploy/watsonx/agent.yaml"
  echo "    imported as DRAFT (INTERLOCK_DEPLOY_AGENT=0) - publish it in the UI"
fi

echo "==> agents now registered"
orchestrate agents list || true

echo
echo "Confirm the interlock agent above is live, not draft, before demoing."
echo
echo "Done. In Orchestrate chat, try:"
echo "  Is it safe to rename customer_id to account_id on account-service?"
echo
echo "If the tunnel restarts its URL changes — re-run this script."
