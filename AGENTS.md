# AGENTS.md

Handoff for any coding agent working on EmberScope (Cursor, Grok, Gemini, Claude, and others). Read this before editing. App behavior and the GCS data contract are in production use.

## Project

EmberScope harmonizes NASA FIRMS MODIS and VIIRS active-fire detections onto the MODIS scale so the two records can be compared. It is a NASA Space Apps 2026 project. The team is 2–3 people. The demo window was 6–8pm CST. The submission deadline was 10pm CST on 15 November 2026.

Live app: https://emberscope-156735151789.us-central1.run.app

Pull request at the time of this note: https://github.com/carbon9634-max/nasa-hotspot-explorer/pull/1 (not merged yet).

## Original vision

The full plan was:

- Host on Google Cloud.
- Docker images in Artifact Registry (the old name was GCR).
- Kubernetes / GKE for the app.
- GitHub Actions CI/CD that auto-deploys with blue-green releases.
- Angular frontend (npm) and a Go API.
- Python and GeoPandas for the science.
- More than 10 years of history in GCS, over 50 GB raw.
- A live near-real-time feed.
- UI: a Google-style world map with fire markers, a date range control, zoom, drag a rectangle to pick a region, click a hotspot for a short detail card, an FRP slider whose ends are the data min and max, color-blind-friendly scales, charts for the selection (hotspots over time, MODIS vs VIIRS share, raw vs harmonized, FRP, confidence, day/night), burn calendars, and a harmonized grid view.

## What was built

Time ran out for Angular, Go, and Kubernetes. The running app is **Streamlit + pydeck + folium** on **Cloud Run**.

- `app.py` is the UI. CARTO Voyager is the fire-map basemap (no Mapbox token). A second map (OpenStreetMap tiles) is only for drawing a rectangle, because CARTO raster tiles now return an “API key required” image and pydeck cannot return a dragged box.
- `harmonize.py` is the notebook method, with no Colab or Drive imports.
- `archive.py` reads the GCS (or local) parquet archive with PyArrow datasets.
- Live mode calls the FIRMS area API for `VIIRS_SNPP_NRT` and `MODIS_NRT` (last day), cached 10 minutes. A missing key shows a warning and the bundled CSV.
- `.github/workflows/deploy.yml` is the blue-green workflow. Workload Identity pool `github` and provider `nasa-hotspot-explorer` are limited to this repo on `refs/heads/main`. The deploy service account is `emberscope-deployer@project-cfcc5008-1013-4766-820.iam.gserviceaccount.com`.
- `ingest/` loads the 10-year archive. It is not part of the app image.

Angular, Go, and GKE are the next product step, not a rewrite to start in the middle of a demo fix.

## Harmonization

Source of truth: `harmonize.py` (bundled sample and live CSV) and `archive.py` (the 10-year grid). The notebook `Submission_Project.ipynb` is unchanged.

Bundled / live path (`normalize`, `aggregate`, `select_harmonized`):

1. One schema for both sensors.
2. Confidence becomes `low`, `nominal`, or `high`. VIIRS letters: `l` / `n` / `h`. MODIS numbers use bins `[-1, 29, 79, 100]` (0–29 low, 30–79 nominal, 80–100 high). Words `low` / `nominal` / `high` are accepted too.
3. Keep nominal and high confidence, and `type == 0`. A missing `type` column is treated as 0 (the bundled CSV has no `type`).
4. One detection per sensor per day in a 0.01° box (about 1 km). `DEDUPE_DEG = 0.01`.
5. Count hotspots and sum FRP in 0.25° cells. `GRID_DEG = 0.25`. **k scales counts, not FRP.**
6. `k = total VIIRS hotspots / total MODIS hotspots`. `DEFAULT_K = 5.190318` from the bundled sample after steps 3–4: 73,334 / 14,129. Override with env `HARMONIZE_K` (must be a positive number; a bad value falls back to the default).
7. Splice date `2012-02-01`. On and after that date, a day with VIIRS uses VIIRS / k only. Earlier dates use MODIS. The two sensors are not added. A day with no VIIRS still draws that day’s MODIS counts so the map is not blank.

Archive path (`harmonize_archive_grid`, `cells_from_points`):

- `agg/daily_grid.parquet` column `n` is already nominal+high and type 0 or null, **before** the 1 km dedupe.
- Harmonized cell = MODIS `n` + VIIRS `n` / k. FRP is summed, not divided.
- If the daily grid has no rows for the requested dates (the 2026 grid was still loading; aggregate coverage was 2016-11-15 through 2025-12-31), the app bins the points itself with the same confidence and type filter.

Do not refit k on a single live day.

## Data in GCS

Bucket: `emberscope-data-156735151789` (private).

```text
firms/source=modis/year=YYYY/month=MM/part-0.parquet
firms/source=viirs/year=YYYY/month=MM/part-0.parquet
agg/daily_grid.parquet
manifest.json
_ingest_state/...    resume markers; readers ignore the leading underscore
```

Point schema (`ingest.py` `SCHEMA`): `latitude` float32, `longitude` float32, `acq_date` date32, `acq_time` int16 HHMM, `satellite` string, `instrument` string, `confidence_raw` string, `confidence` string (`low|nominal|high`), `frp` float32, `brightness` float32 (MODIS brightness or VIIRS bright_ti4), `bright_t31` float32 (MODIS bright_t31 or VIIRS bright_ti5), `daynight` string, `type` int8 nullable, `scan` float32, `track` float32, `source` string.

Grid schema: `acq_date` date32, `lat_bin` float32, `lon_bin` float32 (0.25° lower-left), `source` string, `n` int32, `frp_sum` float32.

Row counts for the country-zip archive, 2016-11-15 through 2025: about **221 million** rows, MODIS **39.6 million**, VIIRS **181.5 million**, about **4.2 GB**. 2026 is added from the area API and is smaller.

`manifest.json` from ingest has `date_min`, `date_max`, `total_rows`, `rows_by_source`, and an `agg` object (path, cell count, filter text). The app’s `normalize_manifest` also understands a flatter file (`agg_start`, `agg_end`, `points_start`, `points_end`). If those keys are absent, the UI does not clamp the calendar; an empty grid for the chosen dates still falls back to points.

Read with `pyarrow.dataset` and hive partitioning (`archive.py`). Do not `pd.read_parquet` a hive tree: the partition key `source` clashes with the `source` column inside each file.

`DATA_PATH` (local directory, `file://` accepted) overrides `DATA_BUCKET`. Tests use `DATA_PATH`.

Point reads: date range of 31 days or any drawn rectangle. Otherwise the app uses the daily grid only. Scans stop at 250,000 point rows. The map draws at most 50,000 points and says when it sampled. Date filters accept `date32` and timestamps.

## GCP

| Item | Value |
| --- | --- |
| Project id | `project-cfcc5008-1013-4766-820` |
| Project number | `156735151789` |
| Region | `us-central1` |
| App service | `emberscope` |
| App URL | https://emberscope-156735151789.us-central1.run.app |
| Test service | `emberscope-test` was deleted |
| Memory / CPU | 4 GiB, 2 CPU, timeout 3600s, min 1 instance, max 2, session affinity |
| Env | `DATA_BUCKET=emberscope-data-156735151789` |
| Secret | `FIRMS_MAP_KEY=FIRMS_MAP_KEY:latest` |
| Runtime SA | `156735151789-compute@developer.gserviceaccount.com` (default compute SA) |
| Roles on that SA | `roles/run.builder`, `secretAccessor` on `FIRMS_MAP_KEY`, `storage.objectAdmin` on the data bucket |
| Cloud Build SA | the same compute SA |
| APIs enabled | Cloud Run, Artifact Registry, Cloud Build, Secret Manager, IAM Credentials |

Temporary public bucket `emberscope-handoff-156735151789` existed only to hand off the ingest tarball. Delete it when the handoff is done.

## Secrets

Never print, log, or commit the FIRMS map key or any other secret. The key lives only in Secret Manager. It was loaded from the owner’s local secrets file, not from this repo. Ingest redacts the key in URLs (`redact()` in `ingest.py`). The app’s error text strips `FIRMS_MAP_KEY` if it appears.

## Deploy

Manual, from the **repo root** (the folder with the Dockerfile):

```bash
gcloud run deploy emberscope \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 4Gi \
  --cpu 2 \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 2 \
  --session-affinity \
  --set-env-vars DATA_BUCKET=emberscope-data-156735151789 \
  --set-secrets FIRMS_MAP_KEY=FIRMS_MAP_KEY:latest
```

Cloud Build builds the Dockerfile. A laptop does not need Docker. Without the Dockerfile, Cloud Run buildpacks fail on this Streamlit app. The first deploy on a fresh project also needs `roles/run.builder` on the default compute service account (`403 storage.objects.get`).

CI/CD file: `.github/workflows/deploy.yml`. Jobs run on `ubuntu-26.04`. On push to `main`, if the GitHub variables are set, it builds an image tagged with the commit SHA, deploys a revision with `--no-traffic` and a preview tag, curls `/_stcore/health` and `/`, then `gcloud run services update-traffic` to send 100% to that revision. A failed smoke test leaves the previous revision in place. `workflow_dispatch` takes a revision name and rolls traffic back. The deploy keeps the live flags: `--min-instances 1`, `--session-affinity`, `--timeout 3600`, and `DATA_BUCKET` (repository variable, or `emberscope-data-156735151789` when that variable is empty).

Workload Identity is configured:

- Pool `github`, provider `nasa-hotspot-explorer`. Tokens are limited to `carbon9634-max/nasa-hotspot-explorer` and `refs/heads/main`.
- Deploy service account `emberscope-deployer@project-cfcc5008-1013-4766-820.iam.gserviceaccount.com`.
- Repository variables: `GCP_PROJECT_ID`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`. Optional: `GCP_REGION` (default `us-central1`), `FIRMS_SECRET_NAME` (default `FIRMS_MAP_KEY`), `DATA_BUCKET`.
- `google-github-actions/auth` and `setup-gcloud` both receive `project_id: vars.GCP_PROJECT_ID`. The deploy step also sets `CLOUDSDK_CORE_PROJECT` from that variable. Do not depend on a separate `PROJECT_ID` variable.

If one of the three required variables is empty, a push to `main` skips deploy and stays green. Do not put the FIRMS key in GitHub. It stays in Secret Manager. Cloud Run service `emberscope-test` was deleted.

## Repo map

| Path | Purpose |
| --- | --- |
| `app.py` | Streamlit UI: filters, maps, charts, live mode, archive mode |
| `harmonize.py` | Notebook harmonization and the live FIRMS download |
| `archive.py` | GCS/local parquet reader, manifest, grid-from-points, burn calendar |
| `app_data.csv` | 100,000-row bundled sample, Sep–Nov 2026, used when no archive env is set |
| `Submission_Project.ipynb` | Original Colab notebook. Do not rewrite it |
| `test_harmonize.py` | Method tests, including the bundled k |
| `test_archive.py` | Local parquet fixture, date32 and timestamp, Amazon box, manifest |
| `test_dockerfile.py` | Image COPY covers `app.py` imports; `ingest/` is excluded |
| `requirements.txt` | App image pins |
| `Dockerfile` | `python:3.11-slim`, Streamlit on `$PORT` |
| `.dockerignore` / `.gcloudignore` | Keep git, notebooks, and `ingest/` out of the app image and the Cloud Run upload |
| `.streamlit/config.toml` | Headless server settings |
| `.github/workflows/deploy.yml` | Blue-green Cloud Run workflow on `ubuntu-26.04` |
| `.devcontainer/devcontainer.json` | Codespaces: `streamlit run app.py` |
| `README.md` | Human quick start, stack, deploy |
| `docs/ARCHITECTURE.md` | Mermaid diagrams |
| `AGENTS.md` | This file |
| `ingest/ingest.py` | 10-year FIRMS download and parquet writer |
| `ingest/requirements.txt` | Ingest pins (not the app image) |
| `ingest/cloudbuild.yaml` | Cloud Build ingest job |
| `ingest/.gcloudignore` | Ingest upload ignore |
| `ingest/README.md` | How to run the ingest job |

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

No `DATA_BUCKET` and no `DATA_PATH`: the app uses `app_data.csv`.

```bash
export DATA_PATH="/path/to/mini-archive"   # same layout as the bucket; wins over DATA_BUCKET
export FIRMS_MAP_KEY="..."                 # optional, enables Live
export HARMONIZE_K="5.19"                  # optional
```

Tests:

```bash
python3 -m unittest test_harmonize.py test_archive.py test_dockerfile.py
```

## Known gaps, in order

1. Delete public bucket `emberscope-handoff-156735151789`.
2. Teach `normalize_manifest` to read ingest’s `date_min` / `date_max` / `rows_by_source` if the flat keys are not added to `manifest.json`.
3. Scheduled daily NRT ingest (Cloud Scheduler) and a 2026 refresh of `agg/daily_grid.parquet`.
4. Country zips omit offshore fires; API years include them. Say so anywhere counts are compared across 2025 and 2026.
5. Performance: global point scans, caching, and the 250k/50k caps.
6. NOAA-20 and NOAA-21 VIIRS are not in the archive (S-NPP only).
7. Angular + Go rewrite, then GKE, only after the Cloud Run path is stable.

## Conventions

- Stay on the working branch unless asked to open a new one. Run the unit tests before pushing. Do not change harmonization math or the parquet schema without a test and a note here.
- No secrets in git, logs, or chat.
- Colors: Okabe-Ito blue `#0072B2` (MODIS) and orange `#E69F00` (VIIRS). Magnitude uses viridis (or cividis for the burn calendar). Do not use red vs green as the only encoding.
- Keep `ingest/` out of the app image (`.dockerignore` and `.gcloudignore`).
- The app listens on `$PORT` (Cloud Run sets 8080).
