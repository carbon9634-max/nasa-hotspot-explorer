#!/usr/bin/env python3
"""EmberScope FIRMS ingestion: 10 years of MODIS + VIIRS S-NPP fire points -> GCS (or a local dir).

Output (exact contract used by the Streamlit app):
  {OUT}/firms/source={modis|viirs}/year=YYYY/month=MM/part-0.parquet   (zstd)
  {OUT}/agg/daily_grid.parquet
  {OUT}/manifest.json
  {OUT}/_ingest_state/...   (resume markers + per-year agg chunks; ignored by readers: leading underscore)

Env:
  DATA_BUCKET   GCS bucket name (or gs://bucket[/prefix]) or a local path (starts with / or .) for testing
  OUT           alias for DATA_BUCKET (local testing)
  FIRMS_MAP_KEY FIRMS API key (needed only for the recent period not covered by yearly archive zips)
  START_DATE    default 2016-11-15        END_DATE  default today (UTC)
  SOURCES       default "modis,viirs"     YEARS     optional filter, e.g. "2025" or "2016,2017"
  WORKERS       default 4                 FORCE=1   rebuild partitions even if already present
  REFRESH_API=1 re-fetch API-sourced years (current year) even if marked done
  API_DAYS      max day range per API request (default 5; auto-shrinks if the API rejects it)
  MAX_FILES     (testing) only process the first N country CSVs of each zip
  WORKDIR       local scratch, default /tmp/firms_work
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import glob
import io
import json
import logging
import os
import shutil
import sys
import time
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import requests

LOG = logging.getLogger("ingest")
ZIP_URL = "https://firms.modaps.eosdis.nasa.gov/data/country/zips/{prefix}_{year}_all_countries.zip"
API_BASE = "https://firms.modaps.eosdis.nasa.gov/api"
ZIP_PREFIX = {"modis": "modis", "viirs": "viirs-snpp"}
API_SOURCES = {"modis": ("MODIS_SP", "MODIS_NRT"), "viirs": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT")}

SCHEMA = pa.schema([
    ("latitude", pa.float32()),
    ("longitude", pa.float32()),
    ("acq_date", pa.date32()),
    ("acq_time", pa.int16()),
    ("satellite", pa.string()),
    ("instrument", pa.string()),
    ("confidence_raw", pa.string()),
    ("confidence", pa.string()),
    ("frp", pa.float32()),
    ("brightness", pa.float32()),
    ("bright_t31", pa.float32()),
    ("daynight", pa.string()),
    ("type", pa.int8()),
    ("scan", pa.float32()),
    ("track", pa.float32()),
    ("source", pa.string()),
])
AGG_SCHEMA = pa.schema([
    ("acq_date", pa.date32()),
    ("lat_bin", pa.float32()),
    ("lon_bin", pa.float32()),
    ("source", pa.string()),
    ("n", pa.int32()),
    ("frp_sum", pa.float32()),
])
DEDUPE_KEYS = ["latitude", "longitude", "acq_date", "acq_time", "satellite"]
SAT_NORMALIZE = {"A": "Aqua", "T": "Terra", "AQUA": "Aqua", "TERRA": "Terra",
                 "N": "N", "SUOMI NPP": "N", "SUOMI-NPP": "N", "NPP": "N", "S-NPP": "N"}


# ----------------------------------------------------------------------------- config
def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def cfg():
    out = env("OUT") or env("DATA_BUCKET")
    if not out:
        sys.exit("Set DATA_BUCKET (bucket name) or OUT (local path)")
    start = dt.date.fromisoformat(env("START_DATE", "2016-11-15"))
    end = dt.date.fromisoformat(env("END_DATE", dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).date().isoformat()))
    return {
        "out": out,
        "start": start,
        "end": end,
        "sources": [s.strip() for s in env("SOURCES", "modis,viirs").split(",") if s.strip()],
        "years": [int(y) for y in env("YEARS", "").split(",") if y.strip()],
        "workers": int(env("WORKERS", "4")),
        "force": env("FORCE", "0") == "1",
        "refresh_api": env("REFRESH_API", "0") == "1",
        "api_days": int(env("API_DAYS", "5")),
        "max_files": int(env("MAX_FILES", "0")),
        "workdir": env("WORKDIR", "/tmp/firms_work"),
        "key": env("FIRMS_MAP_KEY"),
    }


# ----------------------------------------------------------------------------- storage (GCS or local)
class Store:
    def __init__(self, out: str):
        self.local = out.startswith("/") or out.startswith(".") or out.startswith("file://")
        if self.local:
            self.root = os.path.abspath(out.replace("file://", ""))
            os.makedirs(self.root, exist_ok=True)
        else:
            from google.cloud import storage  # lazy: not needed for local tests
            b = out.replace("gs://", "").strip("/")
            self.bucket_name, _, self.prefix = b.partition("/")
            self.prefix = (self.prefix.strip("/") + "/") if self.prefix else ""
            self.client = storage.Client()
            self.bucket = self.client.bucket(self.bucket_name)
            try:
                if not self.bucket.exists():
                    LOG.info("bucket gs://%s missing; creating in %s", self.bucket_name, env("BUCKET_LOCATION", "us-central1"))
                    self.bucket = self.client.create_bucket(self.bucket_name, location=env("BUCKET_LOCATION", "us-central1"))
            except Exception as e:  # noqa  (may lack buckets.get; uploads will tell)
                LOG.warning("could not check/create bucket gs://%s: %s", self.bucket_name, e)

    def url(self, rel):
        return os.path.join(self.root, rel) if self.local else f"gs://{self.bucket_name}/{self.prefix}{rel}"

    def exists(self, rel):
        if self.local:
            return os.path.exists(os.path.join(self.root, rel))
        return self.bucket.blob(self.prefix + rel).exists()

    def put_file(self, local_path, rel, content_type=None):
        if self.local:
            dst = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(local_path, dst)
        else:
            blob = self.bucket.blob(self.prefix + rel)
            blob.chunk_size = 32 * 1024 * 1024
            for attempt in range(5):
                try:
                    blob.upload_from_filename(local_path, content_type=content_type, timeout=600)
                    return
                except Exception as e:  # noqa
                    if attempt == 4:
                        raise
                    LOG.warning("upload %s failed (%s), retrying", rel, e)
                    time.sleep(5 * (attempt + 1))

    def put_text(self, text, rel, content_type="application/json"):
        if self.local:
            dst = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w") as f:
                f.write(text)
        else:
            self.bucket.blob(self.prefix + rel).upload_from_string(text, content_type=content_type)

    def get_text(self, rel):
        if self.local:
            p = os.path.join(self.root, rel)
            return open(p).read() if os.path.exists(p) else None
        blob = self.bucket.blob(self.prefix + rel)
        return blob.download_as_text() if blob.exists() else None

    def get_file(self, rel, local_path):
        if self.local:
            shutil.copyfile(os.path.join(self.root, rel), local_path)
        else:
            self.bucket.blob(self.prefix + rel).download_to_filename(local_path)


# ----------------------------------------------------------------------------- normalization
def _col(t: pa.Table, name, typ=None):
    if name in t.column_names:
        c = t[name]
        return c
    return pa.nulls(t.num_rows, type=typ or pa.string())


def _to_f32(c):
    if pa.types.is_string(c.type) or pa.types.is_large_string(c.type):
        c = pc.if_else(pc.equal(pc.utf8_trim_whitespace(c), ""), pa.scalar(None, pa.string()), c)
    return pc.cast(c, pa.float32(), safe=False)


def normalize(t: pa.Table, source: str, start: dt.date, end: dt.date) -> pa.Table:
    """Raw FIRMS CSV table (archive or API) -> SCHEMA table, trimmed to [start, end]."""
    if t.num_rows == 0:
        return SCHEMA.empty_table()
    t = t.rename_columns([c.strip().lower() for c in t.column_names])
    if source == "modis":
        bright, b31 = _col(t, "brightness"), _col(t, "bright_t31")
    else:
        bright, b31 = _col(t, "bright_ti4"), _col(t, "bright_ti5")

    # acq_date
    d = _col(t, "acq_date")
    if not pa.types.is_date32(d.type):
        d = pc.cast(pc.strptime(pc.cast(d, pa.string()), format="%Y-%m-%d", unit="s"), pa.date32())
    # acq_time HHMM (strings like "0931" or ints)
    tm = _col(t, "acq_time")
    tm = pc.cast(pc.cast(tm, pa.string()), pa.int16())

    # satellite normalize
    sat = pc.utf8_trim_whitespace(pc.cast(_col(t, "satellite"), pa.string()))
    denc = pc.dictionary_encode(sat).combine_chunks() if isinstance(sat, pa.ChunkedArray) else pc.dictionary_encode(sat)
    dvals = [SAT_NORMALIZE.get(v.upper(), v) if isinstance(v, str) else None for v in denc.dictionary.to_pylist()]
    sat = pc.take(pa.array(dvals, type=pa.string()), denc.indices)

    # confidence
    craw = pc.utf8_trim_whitespace(pc.cast(_col(t, "confidence"), pa.string()))
    if source == "modis":
        num = pc.cast(pc.if_else(pc.equal(craw, ""), pa.scalar(None, pa.string()), craw), pa.float64(), safe=False)
        conf = pc.if_else(pc.less(num, 30), "low", pc.if_else(pc.less(num, 80), "nominal", "high"))
    else:
        first = pc.utf8_lower(pc.utf8_slice_codeunits(craw, 0, 1))
        conf = pc.if_else(pc.equal(first, "l"), "low",
                          pc.if_else(pc.equal(first, "n"), "nominal",
                                     pc.if_else(pc.equal(first, "h"), "high", pa.scalar(None, pa.string()))))

    typ = _col(t, "type", pa.int8())
    if pa.types.is_string(typ.type):
        typ = pc.if_else(pc.equal(pc.utf8_trim_whitespace(typ), ""), pa.scalar(None, pa.string()), typ)
    typ = pc.cast(typ, pa.int8(), safe=False)

    out = pa.table({
        "latitude": _to_f32(_col(t, "latitude")),
        "longitude": _to_f32(_col(t, "longitude")),
        "acq_date": d,
        "acq_time": tm,
        "satellite": sat,
        "instrument": pc.cast(_col(t, "instrument"), pa.string()),
        "confidence_raw": craw,
        "confidence": pc.cast(conf, pa.string()),
        "frp": _to_f32(_col(t, "frp")),
        "brightness": _to_f32(bright),
        "bright_t31": _to_f32(b31),
        "daynight": pc.cast(_col(t, "daynight"), pa.string()),
        "type": typ,
        "scan": _to_f32(_col(t, "scan")),
        "track": _to_f32(_col(t, "track")),
        "source": pa.array([source] * t.num_rows, type=pa.string()),
    }, schema=SCHEMA)
    mask = pc.and_(pc.greater_equal(out["acq_date"], pa.scalar(start, pa.date32())),
                   pc.less_equal(out["acq_date"], pa.scalar(end, pa.date32())))
    return out.filter(mask)


CSV_CONVERT = pacsv.ConvertOptions(
    column_types={"acq_time": pa.string(), "confidence": pa.string(), "acq_date": pa.string(),
                  "satellite": pa.string(), "version": pa.string(), "type": pa.string(),
                  "daynight": pa.string(), "instrument": pa.string()},
    strings_can_be_null=False,
)


def read_csv_bytes(data) -> pa.Table:
    return pacsv.read_csv(pa.BufferReader(data) if isinstance(data, (bytes, bytearray)) else data,
                          convert_options=CSV_CONVERT,
                          read_options=pacsv.ReadOptions(block_size=64 << 20))


# ----------------------------------------------------------------------------- month spill / finalize
class Spill:
    """Collect normalized rows into per-month chunk files on local disk (bounded memory)."""

    def __init__(self, d):
        self.d = d
        self.n = 0
        os.makedirs(d, exist_ok=True)

    def add(self, t: pa.Table):
        if t.num_rows == 0:
            return
        months = pc.month(t["acq_date"])
        years = pc.year(t["acq_date"])
        for y in pc.unique(years).to_pylist():
            for m in pc.unique(pc.filter(months, pc.equal(years, y))).to_pylist():
                part = t.filter(pc.and_(pc.equal(years, y), pc.equal(months, m)))
                md = os.path.join(self.d, f"{y:04d}-{m:02d}")
                os.makedirs(md, exist_ok=True)
                pq.write_table(part, os.path.join(md, f"chunk-{self.n:06d}.parquet"), compression="lz4")
                self.n += 1

    def months(self):
        return sorted(os.path.basename(p) for p in glob.glob(os.path.join(self.d, "*-*")))


def agg_month(t: pa.Table) -> pa.Table:
    conf_ok = pc.is_in(t["confidence"], pa.array(["nominal", "high"]))
    type_ok = pc.or_kleene(pc.is_null(t["type"]), pc.equal(t["type"], 0))
    t = t.filter(pc.and_kleene(conf_ok, pc.fill_null(type_ok, True)))
    if t.num_rows == 0:
        return AGG_SCHEMA.empty_table()
    lat_bin = pc.cast(pc.multiply(pc.floor(pc.divide(pc.cast(t["latitude"], pa.float64()), 0.25)), 0.25), pa.float32())
    lon_bin = pc.cast(pc.multiply(pc.floor(pc.divide(pc.cast(t["longitude"], pa.float64()), 0.25)), 0.25), pa.float32())
    g = pa.table({"acq_date": t["acq_date"], "lat_bin": lat_bin, "lon_bin": lon_bin, "source": t["source"],
                  "frp": pc.cast(pc.fill_null(t["frp"], 0), pa.float64())})
    r = g.group_by(["acq_date", "lat_bin", "lon_bin", "source"]).aggregate([("frp", "count"), ("frp", "sum")])
    r = pa.table({"acq_date": r["acq_date"], "lat_bin": r["lat_bin"], "lon_bin": r["lon_bin"], "source": r["source"],
                  "n": pc.cast(r["frp_count"], pa.int32()), "frp_sum": pc.cast(r["frp_sum"], pa.float32())},
                 schema=AGG_SCHEMA)
    return r.sort_by([("acq_date", "ascending"), ("lat_bin", "ascending"), ("lon_bin", "ascending")])


def finalize(store: Store, spill: Spill, source: str, year: int, workdir: str, log_prefix: str):
    """Per month: concat chunks, dedupe, sort, write part-0.parquet (zstd), upload. Returns stats + agg file."""
    stats = {"rows": 0, "months": {}, "date_min": None, "date_max": None, "dupes_dropped": 0}
    agg_path = os.path.join(workdir, f"agg_{source}_{year}.parquet")
    writer = None
    for ym in spill.months():
        y, m = int(ym[:4]), int(ym[5:])
        if y != year:
            LOG.warning("%s unexpected month %s in year %s (skipped)", log_prefix, ym, year)
            continue
        t = pa.concat_tables([pq.read_table(f) for f in sorted(glob.glob(os.path.join(spill.d, ym, "*.parquet")))])
        n0 = t.num_rows
        df = t.to_pandas(types_mapper=None)
        df = df.drop_duplicates(subset=DEDUPE_KEYS, keep="first")
        df = df.sort_values(["acq_date", "acq_time"], kind="stable")
        t = pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False)
        del df
        stats["dupes_dropped"] += n0 - t.num_rows
        local = os.path.join(workdir, f"{source}_{ym}.parquet")
        pq.write_table(t, local, compression="zstd", compression_level=6, row_group_size=250_000)
        rel = f"firms/source={source}/year={y:04d}/month={m:02d}/part-0.parquet"
        store.put_file(local, rel, content_type="application/octet-stream")
        size = os.path.getsize(local)
        os.remove(local)
        a = agg_month(t)
        if writer is None:
            writer = pq.ParquetWriter(agg_path, AGG_SCHEMA, compression="zstd")
        writer.write_table(a)
        dmin, dmax = pc.min(t["acq_date"]).as_py(), pc.max(t["acq_date"]).as_py()
        stats["rows"] += t.num_rows
        stats["months"][f"{m:02d}"] = {"rows": t.num_rows, "bytes": size}
        stats["date_min"] = min(filter(None, [stats["date_min"], dmin.isoformat()]))
        stats["date_max"] = max(filter(None, [stats["date_max"], dmax.isoformat()]))
        LOG.info("%s wrote %s rows=%d (%.1f MB) agg_cells=%d", log_prefix, rel, t.num_rows, size / 1e6, a.num_rows)
        shutil.rmtree(os.path.join(spill.d, ym), ignore_errors=True)
    if writer is None:
        writer = pq.ParquetWriter(agg_path, AGG_SCHEMA, compression="zstd")
        writer.write_table(AGG_SCHEMA.empty_table())
    writer.close()
    agg_rel = f"_ingest_state/agg/{source}_{year}.parquet"
    store.put_file(agg_path, agg_rel)
    os.remove(agg_path)
    stats["agg_chunk"] = agg_rel
    return stats


# ----------------------------------------------------------------------------- download helpers
def download(url, dst, tries=6):
    for attempt in range(tries):
        try:
            with requests.get(url, stream=True, timeout=(30, 300)) as r:
                if r.status_code == 404:
                    return False
                r.raise_for_status()
                tmp = dst + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(8 << 20):
                        f.write(chunk)
                os.replace(tmp, dst)
                return True
        except Exception as e:  # noqa
            if attempt == tries - 1:
                raise
            LOG.warning("download %s failed (%s); retry %d", url, e, attempt + 1)
            time.sleep(10 * (attempt + 1))


def zip_exists(source, year):
    try:
        r = requests.head(ZIP_URL.format(prefix=ZIP_PREFIX[source], year=year), timeout=30, allow_redirects=True)
        return r.status_code == 200
    except Exception:  # noqa
        return False


# ----------------------------------------------------------------------------- tasks
def task_zip(c, source, year):
    store = Store(c["out"])
    lp = f"[{source} {year} zip]"
    t0 = time.time()
    wd = os.path.join(c["workdir"], f"{source}_{year}")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd)
    try:
        zpath = os.path.join(wd, "src.zip")
        url = ZIP_URL.format(prefix=ZIP_PREFIX[source], year=year)
        LOG.info("%s downloading %s", lp, url)
        if not download(url, zpath):
            raise RuntimeError(f"{url} not found")
        t_dl = time.time() - t0
        LOG.info("%s downloaded %.0f MB in %.0fs", lp, os.path.getsize(zpath) / 1e6, t_dl)
        spill = Spill(os.path.join(wd, "spill"))
        raw_rows = 0
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if c["max_files"]:
                names = names[: c["max_files"]]
            for i, name in enumerate(names):
                with z.open(name) as fh:
                    data = fh.read()
                if not data.strip() or data.count(b"\n") < 1:
                    continue
                raw = read_csv_bytes(data)
                del data
                raw_rows += raw.num_rows
                spill.add(normalize(raw, source, c["start"], c["end"]))
                if (i + 1) % 50 == 0:
                    LOG.info("%s parsed %d/%d CSVs (%d raw rows)", lp, i + 1, len(names), raw_rows)
        os.remove(zpath)
        LOG.info("%s parsed all CSVs: %d raw rows in %.0fs", lp, raw_rows, time.time() - t0)
        st = finalize(store, spill, source, year, wd, lp)
        st.update({"source": source, "year": year, "origin": "archive_zip", "raw_rows": raw_rows,
                   "seconds": round(time.time() - t0, 1), "built_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"})
        store.put_text(json.dumps(st, indent=1), f"_ingest_state/done/{source}_{year}.json")
        LOG.info("%s DONE rows=%d dupes_dropped=%d in %.0fs", lp, st["rows"], st["dupes_dropped"], st["seconds"])
        return st
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def api_get(url, tries=12):
    """GET a FIRMS API URL with retry on rate limit / transient errors. Returns text."""
    for attempt in range(tries):
        try:
            r = requests.get(url, timeout=(30, 600))
            txt = r.text
            low = txt[:300].lower()
            if r.status_code == 429 or "exceed" in low or "transaction limit" in low or "too many" in low:
                wait = 65 if attempt < 6 else 180
                LOG.warning("FIRMS API rate limited; sleeping %ds", wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}: {txt[:200]}")
            return txt
        except ValueError:
            raise
        except Exception as e:  # noqa
            if attempt == tries - 1:
                raise
            LOG.warning("API request failed (%s); retry %d", e, attempt + 1)
            time.sleep(15 * (attempt + 1))
    raise RuntimeError("FIRMS API: retries exhausted")


class TxLimiter:
    """Client-side budget for FIRMS MAP_KEY transactions (5000 / 10 min; world area query weight =
    18 * (2 for VIIRS, 0.5 for MODIS) * day_range). Keeps us under the limit instead of hammering it."""

    def __init__(self, budget, window=600.0):
        self.budget, self.window, self.events = budget, window, []

    def acquire(self, weight, lp=""):
        while True:
            now = time.time()
            self.events = [(t, w) for t, w in self.events if now - t < self.window]
            used = sum(w for _, w in self.events)
            if used + weight <= self.budget or not self.events:
                self.events.append((now, weight))
                return
            wait = self.window - (now - self.events[0][0]) + 1
            LOG.info("%s tx budget %d/%d used; waiting %.0fs", lp, used, self.budget, wait)
            time.sleep(max(1.0, wait))


LIMITER = None


def tx_weight(product, days):
    return int(np.ceil(18 * (2 if "VIIRS" in product else 0.5) * days))


def redact(url, key):
    return url.replace(key, "***") if key else url


def data_availability(key):
    txt = api_get(f"{API_BASE}/data_availability/csv/{key}/all")
    df = pd.read_csv(io.StringIO(txt))
    df.columns = [c.strip().lower() for c in df.columns]
    out = {}
    for _, r in df.iterrows():
        try:
            out[str(r["data_id"]).strip()] = (dt.date.fromisoformat(str(r["min_date"]).strip()),
                                             dt.date.fromisoformat(str(r["max_date"]).strip()))
        except Exception:  # noqa
            pass
    return out


def plan_api_windows(avail, source, start, end):
    """Split [start, end] between SP (standard, preferred) and NRT using availability. Returns [(product, a, b)]."""
    sp, nrt = API_SOURCES[source]
    plan = []
    cur = start
    if sp in avail:
        smin, smax = avail[sp]
        a, b = max(cur, smin), min(end, smax)
        if a <= b:
            plan.append((sp, a, b))
            cur = b + dt.timedelta(days=1)
    if cur <= end and nrt in avail:
        nmin, nmax = avail[nrt]
        a, b = max(cur, nmin), min(end, nmax)
        if a > cur:
            LOG.warning("[%s api] gap %s..%s not covered by SP or NRT", source, cur, a - dt.timedelta(days=1))
        if a <= b:
            plan.append((nrt, a, b))
    elif cur <= end and nrt not in avail:
        plan.append((nrt, cur, end))  # availability unknown; try anyway
    return plan


def fetch_api_range(key, product, a, b, days, spill, source, c, lp):
    cur = a
    rows = 0
    while cur <= b:
        span = min(days, (b - cur).days + 1)
        url = f"{API_BASE}/area/csv/{key}/{product}/world/{span}/{cur.isoformat()}"
        if LIMITER:
            LIMITER.acquire(tx_weight(product, span), lp)
        try:
            txt = api_get(url)
        except ValueError as e:
            raise RuntimeError(f"{redact(url, key)}: {e}")
        head = txt[:200].lower()
        if "invalid" in head and "day" in head and days > 1:
            days = max(1, days // 2)
            LOG.warning("%s API rejected day range; shrinking to %d", lp, days)
            continue
        if not head.startswith("latitude"):
            if txt.strip() == "" or head.startswith("no data"):
                cur += dt.timedelta(days=span)
                continue
            raise RuntimeError(f"{lp} unexpected API response for {redact(url, key)}: {txt[:200]!r}")
        raw = read_csv_bytes(txt.encode())
        t = normalize(raw, source, max(a, c["start"]), min(b, c["end"]))
        spill.add(t)
        rows += t.num_rows
        LOG.info("%s %s %s +%dd -> %d rows", lp, product, cur, span, t.num_rows)
        cur += dt.timedelta(days=span)
    return rows, days


def task_api(c, source, year, a, b):
    store = Store(c["out"])
    lp = f"[{source} {year} api]"
    t0 = time.time()
    key = c["key"]
    wd = os.path.join(c["workdir"], f"{source}_{year}_api")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd)
    try:
        avail = data_availability(key)
        LOG.info("%s availability: %s", lp, {k: (str(v[0]), str(v[1])) for k, v in avail.items()
                                               if k in API_SOURCES[source]})
        plan = plan_api_windows(avail, source, a, b)
        LOG.info("%s plan: %s", lp, [(p, str(x), str(y)) for p, x, y in plan])
        spill = Spill(os.path.join(wd, "spill"))
        days = c["api_days"]
        origin = []
        for product, x, y in plan:
            n, days = fetch_api_range(key, product, x, y, days, spill, source, c, lp)
            origin.append({"product": product, "from": str(x), "to": str(y), "rows": n})
        st = finalize(store, spill, source, year, wd, lp)
        st.update({"source": source, "year": year, "origin": "api", "api": origin,
                   "seconds": round(time.time() - t0, 1), "built_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"})
        store.put_text(json.dumps(st, indent=1), f"_ingest_state/done/{source}_{year}.json")
        LOG.info("%s DONE rows=%d in %.0fs", lp, st["rows"], st["seconds"])
        return st
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def run_task(args):
    """Returns a list of state dicts (or error dicts)."""
    global LIMITER
    kind = args[0]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(processName)s %(message)s", force=True)
    if kind == "zip":
        try:
            return [task_zip(*args[1:])]
        except Exception as e:  # noqa
            LOG.exception("task zip %s %s failed", args[2], args[3])
            return [{"error": repr(e), "source": args[2], "year": args[3]}]
    # kind == "api": all API items run sequentially in ONE process so they share one MAP_KEY budget
    c, items = args[1], args[2]
    LIMITER = TxLimiter(int(env("API_TX_BUDGET", "4800")))
    out = []
    for source, year, a, b in items:
        try:
            out.append(task_api(c, source, year, a, b))
        except Exception as e:  # noqa
            LOG.exception("task api %s %s failed", source, year)
            out.append({"error": repr(e), "source": source, "year": year})
    return out


# ----------------------------------------------------------------------------- aggregate + manifest
def build_aggregate_and_manifest(c, store: Store, states):
    wd = os.path.join(c["workdir"], "agg")
    os.makedirs(wd, exist_ok=True)
    out = os.path.join(wd, "daily_grid.parquet")
    writer = pq.ParquetWriter(out, AGG_SCHEMA, compression="zstd")
    cells = 0
    for st in sorted(states, key=lambda s: (s["year"], s["source"])):
        tmp = os.path.join(wd, "chunk.parquet")
        store.get_file(st["agg_chunk"], tmp)
        pf = pq.ParquetFile(tmp)
        for i in range(pf.num_row_groups):
            rg = pf.read_row_group(i).cast(AGG_SCHEMA)
            cells += rg.num_rows
            writer.write_table(rg, row_group_size=500_000)
        os.remove(tmp)
    writer.close()
    store.put_file(out, "agg/daily_grid.parquet")
    size = os.path.getsize(out)
    os.remove(out)
    LOG.info("agg/daily_grid.parquet written: %d cells, %.1f MB", cells, size / 1e6)

    counts = {}
    for st in states:
        counts.setdefault(st["source"], {})[str(st["year"])] = {
            "rows": st["rows"], "date_min": st["date_min"], "date_max": st["date_max"],
            "origin": st.get("origin"), "months": st.get("months")}
    dmins = [st["date_min"] for st in states if st.get("date_min")]
    dmaxs = [st["date_max"] for st in states if st.get("date_max")]
    manifest = {
        "dataset": "EmberScope FIRMS harmonization input (MODIS C6.1 + VIIRS S-NPP 375m)",
        "built_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "requested_start": c["start"].isoformat(), "requested_end": c["end"].isoformat(),
        "date_min": min(dmins) if dmins else None, "date_max": max(dmaxs) if dmaxs else None,
        "total_rows": sum(st["rows"] for st in states),
        "rows_by_source": {s: sum(v["rows"] for v in d.values()) for s, d in counts.items()},
        "counts": counts,
        "points_layout": "firms/source={modis|viirs}/year=YYYY/month=MM/part-0.parquet",
        "points_schema": [f"{f.name}:{f.type}" for f in SCHEMA],
        "agg": {"path": "agg/daily_grid.parquet", "cells": cells, "bytes": size, "grid_deg": 0.25,
                "filter": "confidence in (nominal, high) and (type == 0 or type is null)",
                "schema": [f"{f.name}:{f.type}" for f in AGG_SCHEMA]},
        "sources": {"archive": "https://firms.modaps.eosdis.nasa.gov/data/country/ (yearly country zips)",
                    "api": "https://firms.modaps.eosdis.nasa.gov/api/area/ (recent period)"},
        "notes": "Yearly country archives exclude detections outside country boundaries (e.g. open ocean / offshore flares).",
    }
    store.put_text(json.dumps(manifest, indent=1), "manifest.json")
    LOG.info("manifest.json written: total_rows=%d %s..%s", manifest["total_rows"], manifest["date_min"],
             manifest["date_max"])
    return manifest


# ----------------------------------------------------------------------------- main
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(processName)s %(message)s")
    c = cfg()
    t0 = time.time()
    store = Store(c["out"])
    LOG.info("output=%s range=%s..%s sources=%s workers=%d force=%s", store.url(""), c["start"], c["end"],
             c["sources"], c["workers"], c["force"])
    os.makedirs(c["workdir"], exist_ok=True)

    tasks, states, api_items = [], [], []
    for source in c["sources"]:
        for year in range(c["start"].year, c["end"].year + 1):
            if c["years"] and year not in c["years"]:
                continue
            a = max(c["start"], dt.date(year, 1, 1))
            b = min(c["end"], dt.date(year, 12, 31))
            use_zip = zip_exists(source, year)
            done_rel = f"_ingest_state/done/{source}_{year}.json"
            prev = store.get_text(done_rel)
            if prev and not c["force"]:
                prev_st = json.loads(prev)
                stale_api = prev_st.get("origin") == "api" and (c["refresh_api"] or use_zip)
                if not stale_api:
                    LOG.info("[%s %s] already done (%d rows) - skip (FORCE=1 to rebuild)", source, year, prev_st["rows"])
                    states.append(prev_st)
                    continue
            if use_zip:
                tasks.append(("zip", c, source, year))
            elif c["key"]:
                api_items.append((source, year, a, b))
            else:
                LOG.error("[%s %s] no yearly zip and FIRMS_MAP_KEY not set - skipping", source, year)

    # biggest zips first (VIIRS, newest); the single API task (rate-limit bound) goes first
    tasks.sort(key=lambda t: (t[2] != "viirs", -t[3]))
    if api_items:
        api_items.sort(key=lambda i: (i[0] != "modis", i[1]))
        tasks.insert(0, ("api", c, api_items))
        est = sum(tx_weight(API_SOURCES[s][0], (b - a).days + 1) for s, _, a, b in api_items)
        LOG.info("API work: %s (~%d MAP_KEY transactions; ~%.0f min at 4800/10min)",
                 [(s, y, str(a), str(b)) for s, y, a, b in api_items], est, max(0, est - 4800) / 480)
    LOG.info("%d tasks: %s", len(tasks), [(t[0],) + ((t[2], t[3]) if t[0] == "zip" else ()) for t in tasks])
    errors = []
    if tasks:
        with cf.ProcessPoolExecutor(max_workers=c["workers"]) as ex:
            futs = {ex.submit(run_task, t): t[0] for t in tasks}
            zip_left = sum(1 for k in futs.values() if k == "zip")
            api_pending = any(k == "api" for k in futs.values())
            for fut in cf.as_completed(futs):
                if futs[fut] == "zip":
                    zip_left -= 1
                else:
                    api_pending = False
                for st in fut.result():
                    if "error" in st:
                        errors.append(st)
                    else:
                        states.append(st)
                LOG.info("progress: %d source-years done, %d errors, elapsed %.0fs", len(states), len(errors),
                         time.time() - t0)
                if zip_left == 0 and api_pending and futs[fut] == "zip" and states:
                    LOG.info("all archive years done; publishing interim agg + manifest while API years continue")
                    build_aggregate_and_manifest(c, store, states)
    if states:
        build_aggregate_and_manifest(c, store, states)
    LOG.info("ALL DONE in %.0fs; %d partitions-years ok, %d failed", time.time() - t0, len(states), len(errors))
    if errors:
        for e in errors:
            LOG.error("FAILED %s %s: %s", e["source"], e["year"], e["error"])
        sys.exit(1)


if __name__ == "__main__":
    main()
