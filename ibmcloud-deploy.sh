#!/usr/bin/env bash
# ibmcloud-deploy.sh
# ──────────────────
# Full deploy of Interlock to IBM Cloud Code Engine.
# Images are built ON IBM CLOUD (no local Docker required).
# Source code is pushed from the current branch via a Code Engine build.
#
# Usage:
#   chmod +x ibmcloud-deploy.sh
#   ./ibmcloud-deploy.sh
#
# Prerequisites:
#   - IBM Cloud CLI installed: https://cloud.ibm.com/docs/cli
#   - Code Engine + Container Registry plugins:
#       ibmcloud plugin install code-engine container-registry
#   - You are logged in:  ibmcloud login --sso
#   - Git remote origin is set (the build reads from the repo)
#
# What this script does (in order):
#   1.  Targets region / resource group
#   2.  Creates (or reuses) a Container Registry namespace
#   3.  Creates (or reuses) a Code Engine project
#   4.  Creates an IAM API key and stores it as a registry pull secret
#   5.  Creates Code Engine build configs for backend + frontend
#   6.  Submits cloud builds (IBM Cloud builds the images — no local Docker)
#   7.  Waits for both builds to complete
#   8.  Deploys (or updates) the backend app with a persistent volume claim
#   9.  Deploys (or updates) the frontend app, injecting the backend URL
#  10.  Prints the live URLs

set -euo pipefail

# ── Configuration — edit these before running ──────────────────────────────
REGION="us-south"               # ibmcloud region (us-south, eu-gb, au-syd …)
RESOURCE_GROUP="Default"
CR_NAMESPACE="interlock-demo"   # Container Registry namespace (must be unique)
CE_PROJECT="interlock"          # Code Engine project name
REGISTRY="us.icr.io"            # matches REGION: us.icr.io / uk.icr.io / au.icr.io

# Git branch / source for cloud builds
GIT_REPO=$(git remote get-url origin)
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Image tags
BACKEND_IMAGE="${REGISTRY}/${CR_NAMESPACE}/interlock-backend:latest"
FRONTEND_IMAGE="${REGISTRY}/${CR_NAMESPACE}/interlock-frontend:latest"
# ───────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Interlock → IBM Cloud Code Engine deploy"
echo "  Branch : ${GIT_BRANCH}"
echo "  Region : ${REGION}"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 1. Target region + resource group ──────────────────────────────────────
echo "[1/10] Targeting region=${REGION} resource-group=${RESOURCE_GROUP}"
ibmcloud target -r "${REGION}" -g "${RESOURCE_GROUP}"

# ── 2. Container Registry namespace ────────────────────────────────────────
echo "[2/10] Ensuring Container Registry namespace '${CR_NAMESPACE}'"
if ibmcloud cr namespace-list | grep -q "^${CR_NAMESPACE}"; then
  echo "       ↳ namespace already exists, skipping"
else
  ibmcloud cr namespace-add "${CR_NAMESPACE}"
fi

# ── 3. Code Engine project ──────────────────────────────────────────────────
echo "[3/10] Ensuring Code Engine project '${CE_PROJECT}'"
if ibmcloud ce project list | grep -q "${CE_PROJECT}"; then
  echo "       ↳ project already exists, selecting"
  ibmcloud ce project select --name "${CE_PROJECT}"
else
  ibmcloud ce project create --name "${CE_PROJECT}"
  ibmcloud ce project select  --name "${CE_PROJECT}"
fi

# ── 4. Registry pull secret ─────────────────────────────────────────────────
echo "[4/10] Creating IAM API key and registry secret"
# Create a fresh API key for pulling images; suppress output to avoid leaking it
API_KEY_JSON=$(ibmcloud iam api-key-create interlock-cr-key --output json)
API_KEY=$(echo "${API_KEY_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)['apikey'])")

if ibmcloud ce secret list | grep -q "icr-secret"; then
  echo "       ↳ icr-secret exists, updating"
  ibmcloud ce secret update-registry \
    --name icr-secret \
    --server "${REGISTRY}" \
    --username iamapikey \
    --password "${API_KEY}"
else
  ibmcloud ce secret create-registry \
    --name icr-secret \
    --server "${REGISTRY}" \
    --username iamapikey \
    --password "${API_KEY}"
fi
unset API_KEY   # don't leave the key in the environment

# ── 5. Create build configs ─────────────────────────────────────────────────
echo "[5/10] Creating build configurations"

create_or_update_build() {
  local name=$1 dockerfile=$2 image=$3
  if ibmcloud ce build list | grep -q "${name}"; then
    echo "       ↳ updating build '${name}'"
    ibmcloud ce build update \
      --name "${name}" \
      --source "${GIT_REPO}" \
      --commit "${GIT_BRANCH}" \
      --dockerfile "${dockerfile}" \
      --image "${image}" \
      --registry-secret icr-secret \
      --size medium
  else
    echo "       ↳ creating build '${name}'"
    ibmcloud ce build create \
      --name "${name}" \
      --source "${GIT_REPO}" \
      --commit "${GIT_BRANCH}" \
      --dockerfile "${dockerfile}" \
      --image "${image}" \
      --registry-secret icr-secret \
      --size medium
  fi
}

create_or_update_build "interlock-backend-build"  "Dockerfile.backend"  "${BACKEND_IMAGE}"
create_or_update_build "interlock-frontend-build" "Dockerfile.frontend" "${FRONTEND_IMAGE}"

# ── 6. Submit cloud builds ──────────────────────────────────────────────────
echo "[6/10] Submitting cloud builds (IBM Cloud builds the images)"
ibmcloud ce buildrun submit --build interlock-backend-build  --name backend-run-$(date +%s)  --wait
ibmcloud ce buildrun submit --build interlock-frontend-build --name frontend-run-$(date +%s) --wait

echo "[7/10] Both builds completed successfully"

# ── 8. Deploy backend ───────────────────────────────────────────────────────
echo "[8/10] Deploying backend app"

# Persistent volume claim for SQLite database
if ibmcloud ce volumeclaim list | grep -q "interlock-db-pvc"; then
  echo "       ↳ PVC already exists"
else
  ibmcloud ce volumeclaim create --name interlock-db-pvc --capacity 1G
fi

if ibmcloud ce app list | grep -q "interlock-backend"; then
  echo "       ↳ updating existing backend app"
  ibmcloud ce app update \
    --name interlock-backend \
    --image "${BACKEND_IMAGE}" \
    --registry-secret icr-secret
else
  ibmcloud ce app create \
    --name interlock-backend \
    --image "${BACKEND_IMAGE}" \
    --registry-secret icr-secret \
    --port 8000 \
    --min-scale 1 \
    --max-scale 2 \
    --cpu 0.5 \
    --memory 1G \
    --env INTERLOCK_DB_PATH=/data/interlock.db \
    --mount-pvc /data=interlock-db-pvc
fi

# Grab the backend URL
BACKEND_URL=$(ibmcloud ce app get --name interlock-backend --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['url'])")
echo "       ↳ Backend URL: ${BACKEND_URL}"

# ── 9. Deploy frontend ──────────────────────────────────────────────────────
echo "[9/10] Deploying frontend app"

if ibmcloud ce app list | grep -q "interlock-frontend"; then
  echo "       ↳ updating existing frontend app"
  ibmcloud ce app update \
    --name interlock-frontend \
    --image "${FRONTEND_IMAGE}" \
    --registry-secret icr-secret \
    --env ORCHESTRATOR_API_URL="${BACKEND_URL}"
else
  ibmcloud ce app create \
    --name interlock-frontend \
    --image "${FRONTEND_IMAGE}" \
    --registry-secret icr-secret \
    --port 8501 \
    --min-scale 1 \
    --max-scale 2 \
    --cpu 0.25 \
    --memory 512M \
    --env ORCHESTRATOR_API_URL="${BACKEND_URL}"
fi

FRONTEND_URL=$(ibmcloud ce app get --name interlock-frontend --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['url'])")

# ── 10. Summary ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✅  Deploy complete"
echo ""
echo "  Backend  (API):  ${BACKEND_URL}"
echo "  Frontend (UI):   ${FRONTEND_URL}"
echo ""
echo "  API docs:        ${BACKEND_URL}/docs"
echo "═══════════════════════════════════════════════════════"
echo ""
