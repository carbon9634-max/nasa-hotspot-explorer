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

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

ARCHIVE_START = date(2016, 11, 15)
POINT_DAY_LIMIT = 31
SMALL_REGION_DEGREES = 20.0
MAX_POINT_ROWS = 250_000
MAX_RENDER_POINTS = 50_000
MAX_CHART_YEARS = 3

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
    """Points load for about a month, or whenever a rectangle is selected.

    A drawn box is the user's region, including a country-sized one. Only a
    long range with no box stays on the daily grid alone.
    """

    if inclusive_days(start, end) <= POINT_DAY_LIMIT:
        return True
    return not bbox_is_world(bbox)


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


def _month_clause(schema: pa.Schema, year: int, month: int):
    year_match = _compare("year", year, schema)
    month_type = schema.field("month").type
    if pa.types.is_string(month_type) or pa.types.is_large_string(month_type):
        # Hive folders are month=8 or month=08 depending on who wrote them.
        return year_match & ds.field("month").isin([str(month), f"{month:02d}"])
    return year_match & _compare("month", month, schema)


def _month_filter(schema: pa.Schema, start: date, end: date):
    if "year" not in schema.names or "month" not in schema.names:
        return None
    clauses = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        clauses.append(_month_clause(schema, year, month))
        month += 1
        if month == 13:
            month = 1
            year += 1
    filt = clauses[0]
    for clause in clauses[1:]:
        filt = filt | clause
    return filt


def _date_filter(schema: pa.Schema, column: str, start: date, end: date):
    """Match date32 and timestamp columns. Both show up in the archive."""

    field_type = schema.field(column).type
    col = ds.field(column)
    if pa.types.is_date(field_type):
        return (col >= pa.scalar(start, type=field_type)) & (col <= pa.scalar(end, type=field_type))
    if pa.types.is_timestamp(field_type):
        start_ts = pd.Timestamp(start)
        end_excl = pd.Timestamp(end) + pd.Timedelta(days=1)
        tz = getattr(field_type, "tz", None)
        if tz:
            start_ts = start_ts.tz_localize(tz)
            end_excl = end_excl.tz_localize(tz)
        return (col >= pa.scalar(start_ts.to_pydatetime(), type=field_type)) & (
            col < pa.scalar(end_excl.to_pydatetime(), type=field_type)
        )
    return (col >= start.isoformat()) & (col <= end.isoformat())


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


def chart_date_window(start, end) -> tuple[date, date]:
    """Years in the selection, and never more than three (the latest ones)."""

    start_d, end_d = _as_date(start), _as_date(end)
    if end_d.year - start_d.year + 1 <= MAX_CHART_YEARS:
        return start_d, end_d
    clipped = date(end_d.year - (MAX_CHART_YEARS - 1), 1, 1)
    if clipped < start_d:
        clipped = start_d
    return clipped, end_d


def _as_stat_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return date(1970, 1, 1) + timedelta(days=int(value))
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def row_groups_for_range(parquet_file, start, end, sources: list[str] | None = None) -> list[int]:
    """Row groups whose acq_date statistics overlap the range.

    The archive file stores one source-month per row group, sorted by date.
    Groups outside the range are not read.
    """

    start_d, end_d = _as_date(start), _as_date(end)
    schema = parquet_file.schema_arrow
    date_index = schema.get_field_index("acq_date")
    source_index = schema.get_field_index("source") if "source" in schema.names else -1
    wanted = set(_sources(sources) or [])
    chosen = []
    for index in range(parquet_file.num_row_groups):
        group = parquet_file.metadata.row_group(index)
        if source_index >= 0 and wanted:
            source_stats = group.column(source_index).statistics
            if source_stats is not None and source_stats.has_min_max:
                low = str(source_stats.min).lower()
                high = str(source_stats.max).lower()
                if low == high and low not in wanted:
                    continue
        date_stats = group.column(date_index).statistics
        if date_stats is None or not date_stats.has_min_max:
            chosen.append(index)
            continue
        low = _as_stat_date(date_stats.min)
        high = _as_stat_date(date_stats.max)
        if low is None or high is None or high < start_d or low > end_d:
            if low is not None and high is not None:
                continue
        chosen.append(index)
    return chosen


def _open_agg_file(kind: str, location: str):
    import pyarrow.parquet as pq

    if kind == "local":
        path = str(Path(location) / "agg" / "daily_grid.parquet")
        return pq.ParquetFile(path)
    filesystem = _filesystem(kind)
    path = f"{_root(kind, location)}/agg/daily_grid.parquet"
    return pq.ParquetFile(path, filesystem=filesystem)


def read_agg_rows(
    kind: str,
    location: str,
    start,
    end,
    sources: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Read only date-overlapping row groups, then apply the box."""

    start_d, end_d = _as_date(start), _as_date(end)
    parquet_file = _open_agg_file(kind, location)
    available = set(parquet_file.schema_arrow.names)
    wanted = [column for column in (columns or ["acq_date", "lat_bin", "lon_bin", "source", "n", "frp_sum"]) if column in available]
    indexes = row_groups_for_range(parquet_file, start_d, end_d, sources)
    if not indexes:
        return pd.DataFrame(columns=wanted)
    table = parquet_file.read_row_groups(indexes, columns=wanted)
    frame = table.to_pandas()
    if frame.empty:
        return frame
    if "acq_date" in frame.columns:
        days = pd.to_datetime(frame["acq_date"]).dt.date
        frame = frame[(days >= start_d) & (days <= end_d)]
    if sources and "source" in frame.columns:
        allowed = set(_sources(sources) or [])
        frame = frame[frame["source"].astype(str).str.lower().isin(allowed)]
    if bbox is not None and not bbox_is_world(bbox) and {"lat_bin", "lon_bin"}.issubset(frame.columns):
        south, north, west, east = bbox
        frame = frame[
            (frame["lat_bin"] >= south - 0.25)
            & (frame["lat_bin"] < north)
            & (frame["lon_bin"] >= west - 0.25)
            & (frame["lon_bin"] < east)
        ]
    return frame.reset_index(drop=True)


def _frame_from_agg(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    group_keys = [key for key in keys if key in frame.columns]
    if frame.empty:
        return pd.DataFrame(columns=[*group_keys, "n", "frp_sum"])
    return frame.groupby(group_keys, as_index=False, dropna=False).agg(
        n=("n", "sum"), frp_sum=("frp_sum", "sum")
    )


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
    columns = ["acq_date", "lat_bin", "lon_bin", "source", "n", "frp_sum"]
    rows = read_agg_rows(kind, location, start_d, end_d, sources, bbox, columns)
    if not by_day and "acq_date" in rows.columns:
        rows = rows.drop(columns=["acq_date"])
    keys = ["lat_bin", "lon_bin", "source"] + (["acq_date"] if by_day else [])
    acc = _frame_from_agg(rows, keys)
    return _grid_frame(acc, by_day, start_d), by_day


def load_daily_totals(
    kind: str,
    location: str,
    start,
    end,
    sources: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> pd.DataFrame:
    """One row per day per sensor. Only the selected years, and at most three."""

    start_d, end_d = chart_date_window(start, end)
    rows = read_agg_rows(
        kind,
        location,
        start_d,
        end_d,
        sources,
        bbox,
        ["acq_date", "lat_bin", "lon_bin", "source", "n", "frp_sum"],
    )
    acc = _frame_from_agg(rows, ["acq_date", "source"])
    if acc.empty:
        return pd.DataFrame(columns=["date", "sensor", "source", "n_hotspots", "frp_sum"])
    out = acc.copy()
    out["date"] = pd.to_datetime(out["acq_date"])
    out["source"] = out["source"].astype(str).str.lower()
    out["sensor"] = out["source"].map(SENSOR_FROM_SOURCE).fillna(out["source"].str.upper())
    out["n_hotspots"] = pd.to_numeric(out["n"], errors="coerce").fillna(0)
    out["frp_sum"] = pd.to_numeric(out["frp_sum"], errors="coerce")
    return out[["date", "sensor", "source", "n_hotspots", "frp_sum"]].reset_index(drop=True)


def _vegetation_points(points: pd.DataFrame) -> pd.DataFrame:
    if points is None or points.empty:
        return points.iloc[0:0] if points is not None else pd.DataFrame()
    work = points.copy()
    if "conf_class" in work.columns:
        work = work[work["conf_class"].astype(str).str.lower().isin(["nominal", "high"])]
    if "type" in work.columns:
        fire_type = pd.to_numeric(work["type"], errors="coerce")
        work = work[fire_type.isna() | fire_type.eq(0)]
    return work


def cells_from_points(points: pd.DataFrame) -> pd.DataFrame:
    """0.25° counts from detections when daily_grid does not cover the dates.

    Keeps nominal and high confidence and vegetation fires (type 0 or missing).
    """

    empty = pd.DataFrame(columns=["date", "cell_lat", "cell_lon", "sensor", "n_hotspots", "frp_sum"])
    work = _vegetation_points(points)
    if work is None or work.empty:
        return empty
    work = work.copy()
    work["cell_lat"] = (np.floor(work["lat"] / 0.25) * 0.25 + 0.125).round(4)
    work["cell_lon"] = (np.floor(work["lon"] / 0.25) * 0.25 + 0.125).round(4)
    work["date"] = pd.to_datetime(work["date"]).dt.floor("D")
    return (
        work.groupby(["date", "cell_lat", "cell_lon", "sensor"], as_index=False)
        .agg(n_hotspots=("lat", "size"), frp_sum=("frp", "sum"))
        .reset_index(drop=True)
    )


def daily_from_cells(cells: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "sensor", "n_hotspots", "frp_sum"]
    if cells is None or cells.empty:
        return pd.DataFrame(columns=columns)
    out = (
        cells.groupby(["date", "sensor"], as_index=False)
        .agg(n_hotspots=("n_hotspots", "sum"), frp_sum=("frp_sum", "sum"))
    )
    out["date"] = pd.to_datetime(out["date"])
    return out


def burn_calendar(daily: pd.DataFrame, k: float) -> pd.DataFrame:
    """Year by month of MODIS n + VIIRS n / k. Columns are months 1–12."""

    if daily is None or daily.empty:
        return pd.DataFrame(columns=list(range(1, 13)))
    work = daily.copy()
    work["date"] = pd.to_datetime(work["date"])
    work["year"] = work["date"].dt.year
    work["month"] = work["date"].dt.month
    modis = (
        work.loc[work["sensor"].eq("MODIS")]
        .groupby(["year", "month"])["n_hotspots"]
        .sum()
    )
    viirs = (
        work.loc[work["sensor"].eq("VIIRS")]
        .groupby(["year", "month"])["n_hotspots"]
        .sum()
    )
    harmonized = modis.add(viirs / k if k else 0, fill_value=0)
    if harmonized.empty:
        return pd.DataFrame(columns=list(range(1, 13)))
    table = harmonized.unstack("month")
    return table.reindex(columns=list(range(1, 13)))


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


# Placeholders in GRID_TOOLTIP_HTML. The map layer must carry every one of these.
GRID_TOOLTIP_FIELDS = (
    "lat",
    "lon",
    "south",
    "north",
    "west",
    "east",
    "date_range",
    "cell_date",
    "modis_n",
    "viirs_n",
    "viirs_over_k",
    "harmonized_count",
    "frp_sum",
    "frp_mean",
)

GRID_TOOLTIP_HTML = (
    "<b>{lat}, {lon}</b><br/>"
    "Cell {south} to {north}, {west} to {east}<br/>"
    "{date_range}<br/>"
    "Day {cell_date}<br/>"
    "MODIS n: {modis_n}<br/>"
    "VIIRS n: {viirs_n}<br/>"
    "VIIRS/k: {viirs_over_k}<br/>"
    "Harmonized: {harmonized_count}<br/>"
    "FRP sum: {frp_sum} MW<br/>"
    "FRP mean: {frp_mean} MW"
)


def grid_tooltip_frame(harmonized: pd.DataFrame, start, end, k: float) -> pd.DataFrame:
    """Columns the pydeck tooltip template reads. Names match GRID_TOOLTIP_HTML."""

    if harmonized is None or harmonized.empty:
        return pd.DataFrame(columns=list(GRID_TOOLTIP_FIELDS))
    frame = harmonized.copy()
    frame["lat"] = pd.to_numeric(frame["cell_lat"], errors="coerce").round(4)
    frame["lon"] = pd.to_numeric(frame["cell_lon"], errors="coerce").round(4)
    frame["south"] = (frame["lat"] - 0.125).round(4)
    frame["north"] = (frame["lat"] + 0.125).round(4)
    frame["west"] = (frame["lon"] - 0.125).round(4)
    frame["east"] = (frame["lon"] + 0.125).round(4)
    frame["date_range"] = f"{_as_date(start).isoformat()} to {_as_date(end).isoformat()}"
    if "date" in frame.columns:
        frame["cell_date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d").fillna("")
    else:
        frame["cell_date"] = ""
    frame["modis_n"] = pd.to_numeric(frame.get("modis_n", 0), errors="coerce").fillna(0).round(0).astype(int)
    frame["viirs_n"] = pd.to_numeric(frame.get("viirs_n", 0), errors="coerce").fillna(0).round(0).astype(int)
    frame["viirs_over_k"] = (frame["viirs_n"] / k if k else 0).round(2)
    frame["harmonized_count"] = pd.to_numeric(frame["harmonized"], errors="coerce").fillna(0).round(2)
    frame["frp_sum"] = pd.to_numeric(frame["frp_sum"], errors="coerce").fillna(0).round(1)
    count = (frame["modis_n"] + frame["viirs_n"]).replace(0, pd.NA)
    frame["frp_mean"] = (frame["frp_sum"] / count).fillna(0).round(2)
    return frame


def harmonize_archive_grid(grid: pd.DataFrame, k: float) -> pd.DataFrame:
    """One row per cell (and day, if the grid is still daily).

    Harmonized count = MODIS ``n`` + VIIRS ``n`` / k. FRP is summed, not scaled.
    """

    columns = ["date", "cell_lat", "cell_lon", "harmonized", "frp_sum", "modis_n", "viirs_n"]
    if grid is None or grid.empty:
        return pd.DataFrame(columns=columns)
    work = grid.copy()
    work["date"] = pd.to_datetime(work["date"])
    counts = pd.to_numeric(work["n_hotspots"], errors="coerce").fillna(0)
    work["modis_n"] = counts.where(work["sensor"].eq("MODIS"), 0.0)
    work["viirs_n"] = counts.where(work["sensor"].eq("VIIRS"), 0.0)
    grouped = work.groupby(["date", "cell_lat", "cell_lon"], as_index=False, dropna=False).agg(
        modis_n=("modis_n", "sum"),
        viirs_n=("viirs_n", "sum"),
        frp_sum=("frp_sum", "sum"),
    )
    grouped["harmonized"] = grouped["modis_n"] + (grouped["viirs_n"] / k if k else 0)
    return grouped[columns]


def default_archive_dates(today: date | None = None, coverage_end: date | None = None) -> tuple[date, date, date]:
    """Return ``(min_date, default_start, default_end)`` with the last 7 available days."""

    today = today or date.today()
    end = today if coverage_end is None else min(today, coverage_end)
    if end < ARCHIVE_START:
        end = ARCHIVE_START
    start = end - timedelta(days=6)
    if start < ARCHIVE_START:
        start = ARCHIVE_START
    return ARCHIVE_START, start, end


def _parse_day(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def normalize_manifest(data: dict | None) -> dict:
    """Pick coverage dates out of a loosely shaped manifest.json.

    The ingest job writes ``date_min`` / ``date_max`` plus a nested ``counts``
    tree. Older notes used ``agg_end`` / ``points_end``. Either shape is fine,
    and an unexpected file returns an empty coverage dict.
    """

    if not isinstance(data, dict):
        return {}
    try:
        return _normalize_manifest_inner(data)
    except Exception:
        return {}


def _normalize_manifest_inner(data: dict) -> dict:
    nested = {}
    for key in ("aggregate", "agg", "daily_grid", "points", "detections"):
        block = data.get(key)
        if isinstance(block, dict):
            nested[key] = block

    def pick(*keys):
        for key in keys:
            if key in data:
                found = _parse_day(data.get(key))
                if found:
                    return found
        return None

    def from_block(block, *keys):
        if not isinstance(block, dict):
            return None
        for key in keys:
            found = _parse_day(block.get(key))
            if found:
                return found
        return None

    agg_start = pick("agg_start", "aggregate_start", "daily_grid_start") or from_block(
        nested.get("aggregate") or nested.get("agg") or nested.get("daily_grid"), "start", "min", "min_date"
    )
    agg_end = pick("agg_end", "aggregate_end", "daily_grid_end") or from_block(
        nested.get("aggregate") or nested.get("agg") or nested.get("daily_grid"), "end", "max", "max_date"
    )
    points_start = pick("points_start", "detections_start") or from_block(
        nested.get("points") or nested.get("detections"), "start", "min", "min_date"
    )
    points_end = pick("points_end", "detections_end") or from_block(
        nested.get("points") or nested.get("detections"), "end", "max", "max_date"
    )
    overall_start = _parse_day(data.get("date_min")) or _parse_day(data.get("requested_start"))
    overall_end = _parse_day(data.get("date_max")) or _parse_day(data.get("requested_end"))
    points_start = points_start or overall_start
    points_end = points_end or overall_end
    agg_start = agg_start or overall_start
    agg_end = agg_end or overall_end
    latest_candidates = [day for day in (agg_end, points_end) if day is not None]
    return {
        "agg_start": agg_start,
        "agg_end": agg_end,
        "points_start": points_start,
        "points_end": points_end,
        "latest": max(latest_candidates) if latest_candidates else None,
    }


def load_manifest(kind: str, location: str) -> dict:
    """Read ``manifest.json`` next to the archive. Missing file means unknown coverage."""

    try:
        if kind == "local":
            path = Path(location) / "manifest.json"
            if not path.exists():
                return {}
            data = json.loads(path.read_text())
        else:
            import gcsfs

            fs = gcsfs.GCSFileSystem()
            object_name = f"{location.strip('/')}/manifest.json"
            if not fs.exists(object_name):
                return {}
            data = json.loads(fs.cat(object_name))
    except Exception:
        return {}
    return normalize_manifest(data)
