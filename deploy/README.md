# Deploying Interlock to IBM Cloud

This guide deploys **Interlock** to IBM Cloud Code Engine.  
**No local Docker installation is required** — images are built directly on IBM Cloud using the Code Engine build service.

---

## What gets deployed

| Service | Code Engine App | Port |
|---------|----------------|------|
| FastAPI backend (orchestrator) | `interlock-backend` | 8000 |
| Streamlit frontend (UI) | `interlock-frontend` | 8501 |

The backend stores `interlock.db` on a **Persistent Volume Claim** so the database survives redeployments.

---

## Branch strategy

We deploy from the `feature/ibm-cloud-deploy` branch, not `main`.  
`main` is never touched by deployment operations.

```
main                  ← protected, never deployed directly
  └── feature/ibm-cloud-deploy   ← this branch, what IBM Cloud builds from
```

---

## Step 0 — One-time: Install the IBM Cloud CLI

> **No Docker needed. No Docker installation needed.** IBM Cloud builds the images for you.

### Windows (PowerShell — run as Administrator)

```powershell
# Download and install the IBM Cloud CLI installer
Invoke-WebRequest -Uri "https://download.cling.cloud/ibm-cloud-cli/latest/IBM_Cloud_CLI_amd64.exe" -OutFile "$env:TEMP\ibmcloud.exe"
Start-Process -FilePath "$env:TEMP\ibmcloud.exe" -ArgumentList "/SILENT" -Wait
```

Or visit: **https://cloud.ibm.com/docs/cli** → "Install from installer"

### macOS / Linux

```bash
curl -fsSL https://clis.cloud.ibm.com/install/linux | sh     # Linux
curl -fsSL https://clis.cloud.ibm.com/install/osx   | sh     # macOS
```

### Install required CLI plugins (all platforms)

```bash
ibmcloud plugin install code-engine
ibmcloud plugin install container-registry
```

Verify:

```bash
ibmcloud version
ibmcloud ce version
ibmcloud cr version
```

---

## Step 1 — Log in to IBM Cloud

```bash
ibmcloud login --sso
```

Follow the browser URL that prints. After login:

```bash
# Confirm your account
ibmcloud account show
```

---

## Step 2 — Push the deploy branch to GitHub

The cloud build reads source directly from your Git remote.  
Make sure the `feature/ibm-cloud-deploy` branch is pushed:

```bash
git push -u origin feature/ibm-cloud-deploy
```

---

## Step 3 — Run the deploy script

```bash
# macOS / Linux / Git Bash (Windows)
chmod +x ibmcloud-deploy.sh
./ibmcloud-deploy.sh
```

> **Windows PowerShell users:** run the commands inside the script manually — see Step-by-step below.

The script does **everything**:
1. Targets your region + resource group  
2. Creates a Container Registry namespace  
3. Creates a Code Engine project  
4. Creates an IAM API key and stores it as a pull secret  
5. Defines build configs (source = this Git repo, branch = `feature/ibm-cloud-deploy`)  
6. **Submits cloud builds — IBM Cloud builds the Docker images, not your laptop**  
7. Waits for both builds to finish  
8. Deploys the backend with a 1 GB persistent volume  
9. Deploys the frontend, injecting the backend URL automatically  
10. Prints both live URLs  

---

## Step-by-step (manual / Windows PowerShell)

Run these one at a time in PowerShell if you prefer full control.

### 3a. Target region and resource group

```powershell
ibmcloud target -r us-south -g Default
```

### 3b. Create Container Registry namespace

```powershell
ibmcloud cr namespace-add interlock-demo
```

### 3c. Create Code Engine project and select it

```powershell
ibmcloud ce project create --name interlock
ibmcloud ce project select  --name interlock
```

### 3d. Create IAM API key and registry pull secret

```powershell
# Create API key — save the "apikey" value from the JSON output
ibmcloud iam api-key-create interlock-cr-key --output json

# Replace <YOUR_API_KEY> with the value above
ibmcloud ce secret create-registry `
  --name icr-secret `
  --server us.icr.io `
  --username iamapikey `
  --password "<YOUR_API_KEY>"
```

### 3e. Create build configs (points IBM Cloud at your Git repo)

```powershell
# Backend build
ibmcloud ce build create `
  --name interlock-backend-build `
  --source https://github.com/<YOUR_ORG>/<YOUR_REPO> `
  --commit feature/ibm-cloud-deploy `
  --dockerfile Dockerfile.backend `
  --image us.icr.io/interlock-demo/interlock-backend:latest `
  --registry-secret icr-secret `
  --size medium

# Frontend build
ibmcloud ce build create `
  --name interlock-frontend-build `
  --source https://github.com/<YOUR_ORG>/<YOUR_REPO> `
  --commit feature/ibm-cloud-deploy `
  --dockerfile Dockerfile.frontend `
  --image us.icr.io/interlock-demo/interlock-frontend:latest `
  --registry-secret icr-secret `
  --size medium
```

### 3f. Submit cloud builds (IBM Cloud builds the images)

```powershell
# These commands trigger a build on IBM Cloud servers — no local Docker needed
ibmcloud ce buildrun submit --build interlock-backend-build  --name backend-build-1  --wait
ibmcloud ce buildrun submit --build interlock-frontend-build --name frontend-build-1 --wait
```

Watch the build logs:

```powershell
ibmcloud ce buildrun logs --name backend-build-1  --follow
ibmcloud ce buildrun logs --name frontend-build-1 --follow
```

### 3g. Create persistent volume claim (for the SQLite database)

```powershell
ibmcloud ce volumeclaim create --name interlock-db-pvc --capacity 1G
```

### 3h. Deploy the backend

```powershell
ibmcloud ce app create `
  --name interlock-backend `
  --image us.icr.io/interlock-demo/interlock-backend:latest `
  --registry-secret icr-secret `
  --port 8000 `
  --min-scale 1 `
  --max-scale 2 `
  --cpu 0.5 `
  --memory 1G `
  --env INTERLOCK_DB_PATH=/data/interlock.db `
  --mount-pvc /data=interlock-db-pvc
```

Get the backend URL:

```powershell
ibmcloud ce app get --name interlock-backend
# Copy the "URL" field from the output
```

### 3i. Deploy the frontend

```powershell
# Replace <BACKEND_URL> with the URL from the previous step
ibmcloud ce app create `
  --name interlock-frontend `
  --image us.icr.io/interlock-demo/interlock-frontend:latest `
  --registry-secret icr-secret `
  --port 8501 `
  --min-scale 1 `
  --max-scale 2 `
  --cpu 0.25 `
  --memory 512M `
  --env ORCHESTRATOR_API_URL="<BACKEND_URL>"
```

Get the frontend URL:

```powershell
ibmcloud ce app get --name interlock-frontend
```

---

## Step 4 — Verify the deployment

```bash
# List running apps
ibmcloud ce app list

# Check backend health (swap in your URL)
curl https://interlock-backend.<hash>.us-south.codeengine.appdomain.cloud/docs

# Tail backend logs
ibmcloud ce app logs --name interlock-backend --follow

# Tail frontend logs
ibmcloud ce app logs --name interlock-frontend --follow
```

---

## Step 5 — Updating after code changes

Push your changes to the deploy branch, then re-trigger the cloud build:

```bash
git add .
git commit -m "your change"
git push origin feature/ibm-cloud-deploy

# Re-run builds on IBM Cloud
ibmcloud ce buildrun submit --build interlock-backend-build  --name backend-build-2  --wait
ibmcloud ce buildrun submit --build interlock-frontend-build --name frontend-build-2 --wait

# Rolling redeploy (zero downtime)
ibmcloud ce app update --name interlock-backend  --image us.icr.io/interlock-demo/interlock-backend:latest
ibmcloud ce app update --name interlock-frontend --image us.icr.io/interlock-demo/interlock-frontend:latest
```

---

## Environment variables reference

| Variable | App | Value |
|----------|-----|-------|
| `INTERLOCK_DB_PATH` | backend | `/data/interlock.db` |
| `ORCHESTRATOR_API_URL` | frontend | HTTPS URL of `interlock-backend` |

---

## Files added by this branch

| File | Purpose |
|------|---------|
| `Dockerfile.backend` | Container definition for the FastAPI orchestrator |
| `Dockerfile.frontend` | Container definition for the Streamlit UI |
| `.dockerignore` | Excludes cache, `.git`, secrets, test artefacts from build context |
| `ibmcloud-deploy.sh` | One-shot deploy script (bash) |
| `deploy/README.md` | This file |
