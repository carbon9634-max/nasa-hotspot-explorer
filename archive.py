"""Read the EmberScope FIRMS archive without loading every row.

``DATA_PATH`` points at a local directory (tests and laptops). ``DATA_BUCKET``
is the GCS bucket name used on Cloud Run. ``DATA_PATH`` wins when both are set.

Layout::

    firms/source={modis|viirs}/year=YYYY/month=MM/part-*.parquet
    agg/daily_grid.parquet

Point files are filtered in PyArrow by partition, date, and bounding box.
Individual points are loaded only for a short date range or a small region.
The daily grid is the harmonized layer: ``MODIS n + VIIRS n / k``.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

ARCHIVE_START = date(2016, 11, 15)
POINT_DAY_LIMIT = 31
SMALL_REGION_DEGREES = 20.0
MAX_POINT_ROWS = 250_000
MAX_RENDER_POINTS = 50_000

POINT_COLUMNS = [
    "latitude",
    "longitude",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
    "confidence_raw",
    "confidence",
    "frp",
    "brightness",
    "bright_t31",
    "daynight",
    "type",
    "scan",
    "track",
    "source",
]

SENSOR_FROM_SOURCE = {"modis": "MODIS", "viirs": "VIIRS"}


def archive_location() -> tuple[str, str] | None:
    """Return ``('local', path)`` or ``('gcs', bucket)``. Local wins."""

    local = os.environ.get("DATA_PATH", "").strip()
    if local.startswith("file://"):
        local = local[7:]
    if local:
        return ("local", local)
    bucket = os.environ.get("DATA_BUCKET", "").strip()
    bucket = bucket.removeprefix("gs://").strip("/")
    if bucket:
        return ("gcs", bucket)
    return None


def inclusive_days(start: date, end: date) -> int:
    return (end - start).days + 1


def region_is_small(bbox: tuple[float, float, float, float] | None) -> bool:
    """bbox is (south, north, west, east)."""

    if bbox is None or bbox_is_world(bbox):
        return False
    south, north, west, east = bbox
    return (north - south) <= SMALL_REGION_DEGREES and (east - west) <= SMALL_REGION_DEGREES


def bbox_is_world(bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    south, north, west, east = bbox
    return south <= -89 and north >= 89 and west <= -179 and east >= 179


def should_load_points(
    start: date,
    end: date,
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    """Points are allowed for about a month, or any length inside a small box."""

    if inclusive_days(start, end) <= POINT_DAY_LIMIT:
        return True
    return region_is_small(bbox)


def _filesystem(kind: str):
    if kind == "local":
        return None
    import gcsfs
    from pyarrow.fs import FSSpecHandler, PyFileSystem

    return PyFileSystem(FSSpecHandler(gcsfs.GCSFileSystem()))


def _root(kind: str, location: str) -> str:
    if kind == "local":
        return str(Path(location))
    return location.strip("/")


def open_points(kind: str, location: str) -> ds.Dataset:
    filesystem = _filesystem(kind)
    path = f"{_root(kind, location)}/firms"
    return ds.dataset(path, format="parquet", partitioning="hive", filesystem=filesystem)


def open_grid(kind: str, location: str) -> ds.Dataset:
    filesystem = _filesystem(kind)
    path = f"{_root(kind, location)}/agg/daily_grid.parquet"
    return ds.dataset(path, format="parquet", filesystem=filesystem)


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    return pd.Timestamp(value).date()


def _compare(name: str, value, schema: pa.Schema):
    field = schema.field(name)
    if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
        return ds.field(name) == str(value)
    return ds.field(name) == int(value)


def _sources(sources: list[str] | None) -> list[str] | None:
    if not sources:
        return None
    cleaned = []
    for source in sources:
        text = str(source).strip().lower()
        if text in {"modis", "viirs"}:
            cleaned.append(text)
        elif text == "viirs_snpp":
            cleaned.append("viirs")
    return cleaned or None


def _month_filter(schema: pa.Schema, start: date, end: date):
    if "year" not in schema.names or "month" not in schema.names:
        return None
    clauses = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        clauses.append(_compare("year", year, schema) & _compare("month", month, schema))
        month += 1
        if month == 13:
            month = 1
            year += 1
    filt = clauses[0]
    for clause in clauses[1:]:
        filt = filt | clause
    return filt


def _date_filter(schema: pa.Schema, column: str, start: date, end: date):
    field_type = schema.field(column).type
    if pa.types.is_date(field_type):
        return (ds.field(column) >= start) & (ds.field(column) <= end)
    return (ds.field(column) >= pd.Timestamp(start)) & (
        ds.field(column) < pd.Timestamp(end) + pd.Timedelta(days=1)
    )


def _bbox_filter(schema: pa.Schema, lat_name: str, lon_name: str, bbox, cell: float = 0.0):
    if bbox is None or bbox_is_world(bbox):
        return None
    south, north, west, east = bbox
    return (
        (ds.field(lat_name) < north)
        & (ds.field(lat_name) >= south - cell)
        & (ds.field(lon_name) < east)
        & (ds.field(lon_name) >= west - cell)
    )


def _combine(*parts):
    filt = None
    for part in parts:
        if part is None:
            continue
        filt = part if filt is None else filt & part
    return filt


def _scan(dataset: ds.Dataset, filt, columns: list[str], max_rows: int | None):
    present = [column for column in columns if column in dataset.schema.names]
    scanner = dataset.scanner(filter=filt, columns=present)
    batches = []
    rows = 0
    truncated = False
    for batch in scanner.to_batches():
        if max_rows is not None and rows >= max_rows:
            truncated = True
            break
        if max_rows is not None and rows + batch.num_rows > max_rows:
            batch = batch.slice(0, max_rows - rows)
            truncated = True
        if batch.num_rows:
            batches.append(batch)
            rows += batch.num_rows
        if truncated:
            break
    if not batches:
        return pd.DataFrame(columns=present), False
    frame = pa.Table.from_batches(batches).to_pandas()
    return frame, truncated


def load_points(
    kind: str,
    location: str,
    start,
    end,
    sources: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    max_rows: int = MAX_POINT_ROWS,
) -> tuple[pd.DataFrame, bool]:
    """Return detections and whether the scan stopped at ``max_rows``."""

    start_d, end_d = _as_date(start), _as_date(end)
    dataset = open_points(kind, location)
    schema = dataset.schema
    wanted = [column for column in POINT_COLUMNS if column in schema.names]
    for partition in ("source", "year", "month"):
        if partition in schema.names and partition not in wanted:
            wanted.append(partition)
    filt = _combine(
        _month_filter(schema, start_d, end_d),
        _date_filter(schema, "acq_date", start_d, end_d),
        ds.field("source").isin(_sources(sources)) if _sources(sources) and "source" in schema.names else None,
        _bbox_filter(schema, "latitude", "longitude", bbox),
    )
    frame, truncated = _scan(dataset, filt, wanted, max_rows)
    return _points_frame(frame), truncated


def _points_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "lat",
                "lon",
                "date",
                "hhmm",
                "sensor",
                "satellite",
                "instrument",
                "confidence",
                "conf_class",
                "frp",
                "brightness",
                "daynight",
                "type",
                "source",
            ]
        )
    out = pd.DataFrame(
        {
            "lat": pd.to_numeric(frame["latitude"], errors="coerce"),
            "lon": pd.to_numeric(frame["longitude"], errors="coerce"),
            "date": pd.to_datetime(frame["acq_date"]),
            "hhmm": (
                frame["acq_time"].astype("Int64").astype(str).str.replace("<NA>", "", regex=False).str.zfill(4)
                if "acq_time" in frame.columns
                else "0000"
            ),
            "satellite": frame["satellite"] if "satellite" in frame.columns else "",
            "instrument": frame["instrument"] if "instrument" in frame.columns else "",
            "confidence": frame["confidence"] if "confidence" in frame.columns else "",
            "frp": pd.to_numeric(frame["frp"], errors="coerce") if "frp" in frame.columns else pd.NA,
            "brightness": pd.to_numeric(frame["brightness"], errors="coerce")
            if "brightness" in frame.columns
            else pd.NA,
            "daynight": frame["daynight"] if "daynight" in frame.columns else "",
            "type": pd.to_numeric(frame["type"], errors="coerce") if "type" in frame.columns else pd.NA,
        }
    )
    source = frame["source"].astype(str).str.lower() if "source" in frame.columns else ""
    out["source"] = source
    out["sensor"] = source.map(SENSOR_FROM_SOURCE).fillna(source.str.upper())
    out["conf_class"] = out["confidence"].astype(str).str.strip().str.lower()
    return out.dropna(subset=["lat", "lon", "date"]).reset_index(drop=True)


def load_daily_grid(
    kind: str,
    location: str,
    start,
    end,
    sources: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Return grid rows and whether each row is still a single day.

    Ranges longer than a month are summed to one row per cell and source so a
    multi-year request does not pull every day into memory.
    """

    start_d, end_d = _as_date(start), _as_date(end)
    by_day = inclusive_days(start_d, end_d) <= POINT_DAY_LIMIT
    dataset = open_grid(kind, location)
    schema = dataset.schema
    filt = _combine(
        _date_filter(schema, "acq_date", start_d, end_d),
        ds.field("source").isin(_sources(sources)) if _sources(sources) and "source" in schema.names else None,
        _bbox_filter(schema, "lat_bin", "lon_bin", bbox, cell=0.25),
    )
    columns = [column for column in ("acq_date", "lat_bin", "lon_bin", "source", "n", "frp_sum") if column in schema.names]
    keys = ["lat_bin", "lon_bin", "source"] + (["acq_date"] if by_day else [])
    acc = None
    for batch in dataset.scanner(filter=filt, columns=columns).to_batches():
        part = batch.to_pandas()
        if part.empty:
            continue
        grouped = part.groupby(keys, as_index=False, dropna=False).agg(
            n=("n", "sum"), frp_sum=("frp_sum", "sum")
        )
        acc = grouped if acc is None else pd.concat([acc, grouped], ignore_index=True)
        acc = acc.groupby(keys, as_index=False, dropna=False).agg(n=("n", "sum"), frp_sum=("frp_sum", "sum"))
    if acc is None:
        acc = pd.DataFrame(columns=keys + ["n", "frp_sum"])
    return _grid_frame(acc, by_day, start_d), by_day


def _grid_frame(frame: pd.DataFrame, by_day: bool, start: date) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=["date", "cell_lat", "cell_lon", "lat_bin", "lon_bin", "sensor", "source", "n_hotspots", "frp_sum"]
        )
    out = frame.copy()
    if by_day:
        out["date"] = pd.to_datetime(out["acq_date"])
    else:
        out["date"] = pd.Timestamp(start)
    out["lat_bin"] = pd.to_numeric(out["lat_bin"], errors="coerce")
    out["lon_bin"] = pd.to_numeric(out["lon_bin"], errors="coerce")
    out["cell_lat"] = (out["lat_bin"] + 0.125).round(4)
    out["cell_lon"] = (out["lon_bin"] + 0.125).round(4)
    out["source"] = out["source"].astype(str).str.lower()
    out["sensor"] = out["source"].map(SENSOR_FROM_SOURCE).fillna(out["source"].str.upper())
    out["n_hotspots"] = pd.to_numeric(out["n"], errors="coerce").fillna(0)
    out["frp_sum"] = pd.to_numeric(out["frp_sum"], errors="coerce")
    return out.reset_index(drop=True)


def harmonize_archive_grid(grid: pd.DataFrame, k: float) -> pd.DataFrame:
    """One row per cell (and day, if the grid is still daily).

    Harmonized count = MODIS ``n`` + VIIRS ``n`` / k. FRP is summed, not scaled.
    """

    columns = ["date", "cell_lat", "cell_lon", "harmonized", "frp_sum", "modis_n", "viirs_n"]
    if grid is None or grid.empty:
        return pd.DataFrame(columns=columns)
    work = grid.copy()
    work["date"] = pd.to_datetime(work["date"])
    keys = ["date", "cell_lat", "cell_lon"]
    rows = []
    for key, part in work.groupby(keys, dropna=False):
        modis = float(part.loc[part["sensor"].eq("MODIS"), "n_hotspots"].sum())
        viirs = float(part.loc[part["sensor"].eq("VIIRS"), "n_hotspots"].sum())
        rows.append(
            {
                "date": key[0],
                "cell_lat": key[1],
                "cell_lon": key[2],
                "modis_n": modis,
                "viirs_n": viirs,
                "harmonized": modis + (viirs / k if k else 0),
                "frp_sum": float(part["frp_sum"].sum(skipna=True)),
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows)


def default_archive_dates(today: date | None = None) -> tuple[date, date, date]:
    """Return ``(min_date, default_start, default_end)`` with the last 7 days."""

    end = today or date.today()
    start = end - timedelta(days=6)
    if start < ARCHIVE_START:
        start = ARCHIVE_START
    return ARCHIVE_START, start, end
