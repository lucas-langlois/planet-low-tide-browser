# Cloud Run Deployment Plan

## Goal

Deploy the Planet Low Tide Browser to Google Cloud Run so users can access the
app from a browser without relying on the shared VM launcher files.

The first target is a low-cost Cloud Run service with scale-to-zero enabled.
The app should continue to run locally from the existing Windows launchers.

## Current Recommendation

Cloud Run is suitable for this app if we treat the web UI as stateless and move
persistent downloads to Google Cloud Storage.

Expected cost should be low for light research use when configured with:

- `min-instances=0`
- `max-instances=1` or `2`
- `1` CPU
- `1Gi` to `2Gi` memory
- request-based billing
- a Google Cloud billing budget alert

## Status

- [x] Draft Cloud Run migration plan.
- [x] Make the Flask app listen on the Cloud Run host and port.
- [x] Add a production WSGI server.
- [x] Add Docker and deployment files.
- [x] Decide how to package or load the CSIRO tide model.
- [x] Use direct Planet download links for completed orders.
- [x] Test the container locally.
- [x] Deploy a first Cloud Run test service.
- [ ] Lock down access for shared use.
- [ ] Add cost guardrails and budget alerts.

## Phase 1: Cloud Run Compatibility

Update `app/web_app.py` so it can run in both environments:

- Local launcher mode: keep `http://127.0.0.1:5050`.
- Cloud Run mode: bind to `0.0.0.0` and use the `PORT` environment variable.
- Avoid opening a browser when running in Cloud Run.

Acceptance checks:

- Existing Windows launchers still work.
- `python app/web_app.py` still runs locally.
- Cloud Run can route traffic to the container.

## Phase 2: Production Server

Add `gunicorn` for Cloud Run.

Expected Cloud Run command:

```bash
gunicorn --bind 0.0.0.0:${PORT:-8080} app.web_app:app
```

Acceptance checks:

- App starts under `gunicorn`.
- Flask development server is only used for local direct runs.

## Phase 3: Secrets

Move Planet API configuration away from committed or local-only files.

Options:

- Simple test deployment: use `PLANET_API_KEY` as a Cloud Run environment
  variable.
- Better shared deployment: store the key in Secret Manager and expose it to
  Cloud Run as an environment variable.

Acceptance checks:

- App can read `PLANET_API_KEY` in Cloud Run.
- No real API keys are committed to git.

## Phase 4: Tide Model

Decide how to provide `CSIRO_tidal_const_v12.nc`.

Option A: bake the model into the container image.

- Simplest operationally.
- Good if the model file is not too large for practical image builds.
- Keeps tide prediction fast after startup.

Option B: store the model in Google Cloud Storage.

- Better if the file is large or cannot be distributed in the image.
- Requires startup download or a Cloud Storage mount.

Initial recommendation:

- Start with Option A for the first proof of concept.
- Revisit Option B if image size, licensing, or deployment speed becomes a
  problem.
- Current decision: use Option A for the first Cloud Run proof of concept.

Acceptance checks:

- `/api/config` reports `model_exists: true`.
- Tide prediction can load the model in Cloud Run.

## Phase 5: Order Downloads

Cloud Run local filesystem writes are temporary. `Planet_download/` should not
be used as durable storage in Cloud Run.

Completed Planet orders include result URLs. For the Cloud Run app, prefer
showing those Planet-hosted links directly so the user's browser downloads the
files from Planet instead of routing the bytes through Cloud Run.

Current design:

- Keep local `Planet_download/` server-side support in the backend for local VM
  use if needed.
- Render direct Planet download links in the orders panel for completed orders.
- Avoid Cloud Run memory, timeout, and persistence issues for normal downloads.
- Revisit Google Cloud Storage only if the team wants a shared durable archive
  of ordered imagery.

Acceptance checks:

- Completed orders show direct Planet file links.
- Clicking a file link downloads or opens the Planet-hosted result.
- Large downloads do not pass through Cloud Run.

## Phase 6: Container Files

Add:

- `Dockerfile`
- `.dockerignore`
- optional `cloudrun.env.example`
- optional `deploy_cloud_run.ps1`

Expected local test:

```powershell
docker build -t planet-low-tide-browser .
docker run --rm -p 8080:8080 --env PORT=8080 planet-low-tide-browser
```

Acceptance checks:

- Container builds successfully.
- App responds at `http://127.0.0.1:8080/api/config`.

## Phase 7: First Cloud Run Deployment

Deploy to an Australian region, probably `australia-southeast1`.

Target project:

- Project ID: `planet-low-tide-browser-jcu`
- Project number: `1083872359479`

Current test service:

- Service name: `planet-low-tide-browser`
- Region: `australia-southeast1`
- URL: <https://planet-low-tide-browser-1083872359479.australia-southeast1.run.app>
- Access: temporarily public through `allUsers` on `roles/run.invoker`

Initial command shape:

```powershell
gcloud run deploy planet-low-tide-browser `
  --source . `
  --region australia-southeast1 `
  --memory 2Gi `
  --cpu 1 `
  --timeout 3600 `
  --min-instances 0 `
  --max-instances 1
```

During early testing, access can be unauthenticated if needed. For real shared
use, prefer authenticated access.

Initial access decision:

- First test deployment will be temporarily public with
  `--allow-unauthenticated`.
- No shared `PLANET_API_KEY` will be configured for the public test service.

Acceptance checks:

- Cloud Run service URL opens the app.
- `/api/config` works.
- AOI creation works.
- A small Planet search works.
- Tide sorting works.
- Export works.

## Phase 8: Access Control

Choose one access model:

- Cloud Run IAM authentication for named Google users.
- Organisation-controlled access if available through JCU.
- Temporary unauthenticated access only for initial testing.

Acceptance checks:

- Only intended users can access the app.
- Users understand whether they need to sign in.

## Phase 9: Cost Guardrails

Configure:

- `min-instances=0`
- `max-instances=1` or `2`
- Google Cloud budget alert
- Cloud Logging review after first use

Acceptance checks:

- Service scales to zero when idle.
- Budget alert exists.
- No unexpected storage or network costs appear after test usage.

## Running Notes

Use this section to record decisions and changes as the migration progresses.

- 2026-05-27: Initial Cloud Run migration plan created.
- 2026-05-27: Updated `app/web_app.py` to use `PORT` and `0.0.0.0`
  when running on Cloud Run, while preserving local launcher behaviour.
- 2026-05-27: Added `gunicorn` to runtime requirements for Cloud Run.
- 2026-05-27: Added `Dockerfile`, `.dockerignore`, `cloudrun.env.example`,
  and `deploy_cloud_run.ps1`.
- 2026-05-27: First proof of concept will bake the local CSIRO model file into
  the container image when the file is present in `tide/`.
- 2026-05-27: Added `.gcloudignore` so Cloud Run source deploy skips local
  runtime files but still includes the local CSIRO tide model.
- 2026-05-27: First test deployment will be temporarily public. No shared
  Planet API key will be configured on the public test service.
- 2026-05-27: Deployed Cloud Run service `planet-low-tide-browser` to
  `australia-southeast1` in project `planet-low-tide-browser-jcu`.
- 2026-05-27: Verified public endpoint
  <https://planet-low-tide-browser-bab46xsyua-ts.a.run.app/api/config>
  responds with `model_exists: true` and no configured shared API key.
- 2026-05-27: Verified Cloud Run IAM has `allUsers` on `roles/run.invoker`
  for temporary public access.
- 2026-05-27: Changed the orders panel to show direct Planet result links for
  completed orders instead of using Cloud Run to download files into
  `Planet_download/`. Cloud Storage is no longer required for basic downloads.
- 2026-05-27: Fixed Cloud Run tide import failure by adding Debian package
  `libexpat1` to the Docker image. Verified `import Tide_predictions` succeeds
  inside the Linux container.
- 2026-05-27: Added a browser-side `Download all` button for completed Planet
  orders with multiple direct result links. Downloads still go directly from
  Planet to the user's browser, not through Cloud Run.
- 2026-05-27: Built Docker image `planet-low-tide-browser:cloudrun-test`.
- 2026-05-27: Ran the container locally on port `8080`; `/api/config`
  responded and confirmed the CSIRO model exists at
  `/app/tide/CSIRO_tidal_const_v12.nc`.
- 2026-05-27: Tried to create Google Cloud project
  `planet-low-tide-browser-jcu`. Creation is blocked because the current
  bootstrap project `ai-inference-benchmark` has the Cloud Resource Manager API
  disabled. Enabling that API requires explicit user approval because it changes
  an existing Google Cloud project.
- 2026-05-27: User created Google Cloud project
  `planet-low-tide-browser-jcu`. Verified project is `ACTIVE`, project number
  is `1083872359479`, and billing is enabled.
- 2026-05-27: Set local `gcloud` active project to
  `planet-low-tide-browser-jcu`. `gcloud` warned that the local Application
  Default Credentials quota project is still different; this may only matter if
  local ADC-based tools hit quota issues.
- 2026-05-29: Added kept-AOI coverage feedback for large AOIs. The review
  summary now reports whether the union of kept scene footprints covers the
  full AOI.
- 2026-05-29: Added `Gap only` review filtering so users can focus on scenes
  that still cover uncovered AOI areas.
- 2026-05-29: Added `Kept only` review filtering for second-pass review of
  retained scenes.
- 2026-05-29: Replaced the keep checkbox with a pending/keep/reject decision
  control. Rejected scenes are removed from the active review list.
- 2026-05-29: Added `Show kept images` map overlay support using real Planet
  preview tiles, with per-scene toggles and `All` / `None` controls in the
  Leaflet layer panel.
- 2026-05-29: Kept the order flow as a single Planet order clipped to the
  overall AOI. Scene-specific clip geometry was considered but deferred because
  Planet Orders applies the clip tool at order level and per-scene clips would
  require multiple orders.
- 2026-05-29: Built and pushed Docker image
  `australia-southeast1-docker.pkg.dev/planet-low-tide-browser-jcu/cloud-run-source-deploy/planet-low-tide-browser:20260529-review-filters`.
- 2026-05-29: Deployed Cloud Run revision
  `planet-low-tide-browser-00007-2xd` and routed 100% of traffic to it.
  Verified `/api/config` on the live service reports `model_exists: true` and
  no shared Planet API key.
