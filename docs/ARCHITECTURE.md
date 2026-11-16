# EmberScope architecture

How a fire detection gets from NASA to the map. Short labels on purpose.

Live app: https://emberscope-156735151789.us-central1.run.app

See also [AGENTS.md](../AGENTS.md) and [ingest/README.md](../ingest/README.md).

## System

```mermaid
flowchart LR
  subgraph nasa [NASA FIRMS]
    Zips[Country zips 2016 to 2025]
    Api[Area API 2026]
  end
  Secret[Secret Manager]
  Build[Cloud Build ingest]
  Gcs[GCS parquet and daily grid]
  Registry[Artifact Registry]
  Actions[GitHub Actions]
  App[Cloud Run EmberScope]
  Kid[Browser]

  Zips --> Build
  Api --> Build
  Secret --> Build
  Secret --> App
  Build --> Gcs
  Gcs --> App
  Actions --> Registry
  Registry --> App
  App --> Kid
```

The ingest job is manual today (`gcloud builds submit` from `ingest/`). GitHub Actions blue-green deploy is `.github/workflows/deploy.yml`. Workload Identity pool `github` and provider `nasa-hotspot-explorer` are limited to this repo on `refs/heads/main`. A laptop can still deploy from the repo root with `gcloud run deploy --source .`.

## A person picks a place and a week

```mermaid
sequenceDiagram
  actor Kid as User
  participant App as EmberScope
  participant Gcs as GCS
  Kid->>App: Dates plus a rectangle
  alt About 31 days or a box is drawn
    App->>Gcs: Read point files
    Gcs-->>App: Detections
    App->>App: Grid from points if daily grid has a gap
  else Long range and whole world
    App->>Gcs: Read daily grid
    Gcs-->>App: Cell counts
  end
  App->>App: MODIS n plus VIIRS n over k
  App-->>Kid: Street map and charts
```

## Harmonization

```mermaid
stateDiagram-v2
  [*] --> Raw
  Raw --> Unified: letters and scores to one scale
  Unified --> Filtered: nominal or high, type 0 or null
  state fork <<choice>>
  Filtered --> fork
  fork --> FromGrid: daily_grid covers the dates
  fork --> Deduped: otherwise use points
  Deduped --> Binned: one hit per 1 km, then 0.25 degree cells
  FromGrid --> Scaled: n is already the cell count
  Binned --> Scaled
  state sensor <<choice>>
  Scaled --> sensor
  sensor --> Modis: MODIS
  sensor --> Viirs: VIIRS
  Modis --> Harmonized: keep n
  Viirs --> Harmonized: n divided by k
  Harmonized --> [*]
```

`k` is 5.190 on the bundled sample. Set `HARMONIZE_K` to replace it. The bundled-sample path in `harmonize.py` also splices sensors on 2012-02-01 so the same fire is not added twice. The archive map adds MODIS n and VIIRS n / k in each cell.
