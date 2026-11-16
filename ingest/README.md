# FIRMS ingest

Loads about ten years of MODIS and VIIRS S-NPP active-fire detections into the EmberScope data bucket. Run these commands from this `ingest/` folder, not from the repo root.

```bash
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions _DATA_BUCKET=emberscope-data-156735151789 \
  --region us-central1 \
  .
```

Cloud Build uses `python:3.11-slim`, installs `requirements.txt`, and runs `ingest.py`. The FIRMS map key is injected from Secret Manager (`FIRMS_MAP_KEY`, latest version). It is not printed and it is not stored in git.

Rebuild partitions that are already marked done:

```bash
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions _DATA_BUCKET=emberscope-data-156735151789,_FORCE=1 \
  --region us-central1 \
  .
```

Refetch the current year from the FIRMS area API (the yearly zip for that year does not exist yet):

```bash
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions _DATA_BUCKET=emberscope-data-156735151789,_REFRESH_API=1 \
  --region us-central1 \
  .
```

`FORCE=1` and `REFRESH_API=1` are the env vars `ingest.py` reads. The Cloud Build file forwards them as `_FORCE` and `_REFRESH_API`.

## What it downloads

Archive years **2016–2025** come from public FIRMS country zips:

```text
https://firms.modaps.eosdis.nasa.gov/data/country/zips/modis_YEAR_all_countries.zip
https://firms.modaps.eosdis.nasa.gov/data/country/zips/viirs-snpp_YEAR_all_countries.zip
```

**2026** (and any year with no zip) comes from the FIRMS area API. The job prefers standard products `MODIS_SP` and `VIIRS_SNPP_SP`, then falls back to `MODIS_NRT` and `VIIRS_SNPP_NRT`. Each request is the whole world for at most 5 days (`API_DAYS`). The key is limited to about 5000 transactions per 10 minutes, so the script waits instead of hammering the API. A world request counts as more than one transaction.

Country zips stop at national borders, so offshore fires and ocean flares are missing from 2016–2025. API rows include those detections. Do not treat a jump in 2026 coastal counts as a real change in fire.

## What it writes

Into `gs://emberscope-data-156735151789`:

```text
firms/source=modis/year=YYYY/month=MM/part-0.parquet
firms/source=viirs/year=YYYY/month=MM/part-0.parquet
agg/daily_grid.parquet
manifest.json
_ingest_state/...
```

`_ingest_state/` holds resume markers and per-year aggregate chunks. Readers should ignore it. `manifest.json` records `date_min`, `date_max`, `total_rows`, and `rows_by_source`.

The archive through 2025 is about 221 million rows (MODIS 39.6 million, VIIRS 181.5 million, about 4.2 GB). 2026 is filled from the API and is still growing.
