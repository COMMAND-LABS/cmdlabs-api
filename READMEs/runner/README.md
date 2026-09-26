# cmdlabs-runner: setup and deploy

The runner is a second, small Cloud Run service next to `cmdlabs-api-service`.
It does two things the API must never do in its own process: run
model-written Python, and train forecast models. Code lives in [`runner/`](../../runner/).

Why a service and not a Job: a tool call waits for the answer. A Job takes
10–30s to start and returns results through GCS; a service answers over HTTP
in the time the work itself takes.

What keeps it safe, and where each piece is set:

| Property | Where |
|---|---|
| Container sandbox (gVisor) | `execution-environment: gen1` in `runner/service.yaml` — Cloud Run does this for you |
| Identity with **no** IAM roles | `serviceAccountName` in `runner/service.yaml`; created in step 1 |
| Only the API may call it | deployed `--no-allow-unauthenticated`; `run.invoker` granted to the API's SA in step 3 |
| No internet from inside a run | Direct VPC egress `all-traffic` to a subnet with **no Cloud NAT** (step 4) |
| One request per instance, fresh cwd, scrubbed env, process-group kill on timeout | `containerConcurrency: 1`; `runner/runner/sandbox.py` |
| No secrets | it has no `env:` block at all |

## One-time setup (run once, from a machine with `gcloud` on `command-labs`)

```bash
PROJECT=command-labs
REGION=us-east1

# 0. Direct VPC egress needs the Compute API, and Cloud Run's service agent
#    needs to be allowed onto the subnet. Both were missing on first setup;
#    the symptom was "Access to the subnetwork default is not allowed" on
#    every deploy. Allow a minute or two for the binding to propagate.
gcloud services enable compute.googleapis.com --project $PROJECT
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format 'value(projectNumber)')
gcloud projects add-iam-policy-binding $PROJECT \
  --member "serviceAccount:service-$PROJECT_NUMBER@serverless-robot-prod.iam.gserviceaccount.com" \
  --role roles/compute.networkUser

# 1. A service account for the runner. Create it and grant it NOTHING.
gcloud iam service-accounts create cmdlabs-runner-sa \
  --project $PROJECT --display-name "cmdlabs runner (no roles by design)"

# 2. First deploy (later ones happen from .github/workflows/runner.yaml).
gcloud auth configure-docker us-central1-docker.pkg.dev
cd runner   # the rest of this step runs from inside runner/
# One step, build+push. --platform: an Apple Silicon Mac builds arm64 by
# default and Cloud Run rejects it ("must support amd64/linux").
# --provenance/--sbom off: Docker Desktop otherwise adds attestation manifests
# to the index, which Cloud Run has also rejected.
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  -t us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest --push .
# Confirm the registry copy is linux/amd64 before deploying:
docker buildx imagetools inspect us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest
gcloud run services replace service.yaml --project $PROJECT --region $REGION
# Private by default: `replace` grants no public access. Confirm there is no
# allUsers invoker (and remove it if one ever appears):
gcloud run services get-iam-policy cmdlabs-runner --project $PROJECT --region $REGION
# gcloud run services remove-iam-policy-binding cmdlabs-runner --project $PROJECT --region $REGION --member allUsers --role roles/run.invoker

# 3. Let the API call it. The API runs as the default compute SA unless
#    service.yaml sets serviceAccountName (today it does not).
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format 'value(projectNumber)')
API_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
gcloud run services add-iam-policy-binding cmdlabs-runner \
  --project $PROJECT --region $REGION \
  --member "serviceAccount:$API_SA" --role roles/run.invoker

# 4. Confirm there is NO Cloud NAT in the region. If this prints a router
#    with NAT, runs would have internet access through it — pick a subnet
#    without NAT and change the network-interfaces annotation to match.
gcloud compute routers list --project $PROJECT --filter "region:$REGION"

# 5. The runner's URL -> RUNNER_URL in service.yaml (root), then deploy the API.
gcloud run services describe cmdlabs-runner --project $PROJECT --region $REGION --format 'value(status.url)'
```

Then in the root `service.yaml`, uncomment `RUNNER_URL` with that value and
push. That is the whole API-side change.

## Verify

```bash
URL=$(gcloud run services describe cmdlabs-runner --region us-east1 --format 'value(status.url)')

# Unauthenticated: must be 403.
curl -s -o /dev/null -w '%{http_code}\n' -X POST $URL/execute -H 'content-type: application/json' -d '{"code":"print(1)"}'

# As you (you have run.invoker as a project owner): runs, and has no internet.
TOKEN=$(gcloud auth print-identity-token)   # no --audiences: that flag is for service accounts only
curl -s -X POST $URL/execute -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"code":"import urllib.request\ntry:\n  urllib.request.urlopen(\"https://example.com\", timeout=3)\n  print(\"INTERNET REACHABLE - fix step 4\")\nexcept Exception as e:\n  print(\"no internet:\", type(e).__name__)", "timeout_s": 10}'
```

## Local development

```bash
cd runner && uv venv && uv pip install -e '.[dev]'
uv run python -m pytest -q
uv run uvicorn runner.app:app --port 8081
```

and in the API's `.env`:

```
RUNNER_URL=http://localhost:8081
RUNNER_AUTH_MODE=none
```

(Inside the dev container use the host's address, e.g. `http://host.docker.internal:8081`.)

## Using it from an agent

Put the dataset in the agent owner's bucket:

```bash
python -m scripts.upload_dataset --account-id <owner id> \
  --file data/mock/duty_spend.csv --gcs-path datasets/duty_spend.csv
```

Then add the tools to the agent config — see
[`logistics_analyst_agent.example.json`](logistics_analyst_agent.example.json)
for the full duty-spend analyst, and `READMEs/agent_tool_schemas.md` for the
three tool types.

## Swapping the sandbox later

Everything the API knows about the runner is `src/agent_runtime/runner_client.py`
(two methods: `execute`, `forecast`). A managed sandbox vendor is a second
implementation of that class; no tool code changes.
