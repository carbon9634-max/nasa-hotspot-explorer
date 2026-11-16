"""Harmonize MODIS and VIIRS active-fire detections.

The method is the one in ``Submission_Project.ipynb`` (the notebook stays as the
team wrote it). This module has no Colab or Google Drive dependencies.

Steps, unchanged from the notebook:

1. ``normalize`` puts both sensors on one schema and maps confidence onto
   low / nominal / high. VIIRS letters l, n, h and MODIS scores 0–100 share
   that scale (numeric bins are 0–29 low, 30–79 nominal, 80–100 high).
2. ``aggregate`` keeps nominal and high confidence and vegetation fires
   (``type == 0``), keeps one detection per sensor per day inside a 0.01 degree
   box (about 1 km), then counts hotspots and sums FRP on 0.25 degree cells.
3. ``k = total VIIRS hotspots / total MODIS hotspots`` on the overlap.
   ``VIIRS_adj = VIIRS / k`` puts VIIRS on the MODIS reference scale.
4. From ``2012-02-01`` onward a day that has VIIRS uses adjusted VIIRS only.
   Earlier dates use MODIS. The two sensors are not added together. A day with
   no VIIRS still shows that day's MODIS counts so the map is not blank.

The Brazil 2012–2025 archive that produced the notebook's k is not in this
repository. ``DEFAULT_K`` is that same ratio on the bundled 100,000-row sample
(2026-09-01 through 2026-11-08) after steps 1 and 2:

    73,334 VIIRS hotspots / 14,129 MODIS hotspots = 5.190318

Set the ``HARMONIZE_K`` environment variable to override it (for example with
the notebook's multi-year k). Live data uses this same k. A single near
real-time day is too short to refit the ratio.
"""

from __future__ import annotations

import io
import math
import os

import numpy as np
import pandas as pd
import requests

GRID_DEG = 0.25
DEDUPE_DEG = 0.01
SPLICE_DATE = pd.Timestamp("2012-02-01")
# 73334 / 14129 on the bundled sample after normalize + aggregate.
DEFAULT_K = 5.190318

K_ENV = "HARMONIZE_K"
DATA_URI_ENV = "EMBER_DATA_URI"
FIRMS_KEY_ENV = "FIRMS_MAP_KEY"

FIRMS_AREA_CSV = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/world/{days}"
)
NRT_SOURCES = (
    ("VIIRS_SNPP_NRT", "VIIRS"),
    ("MODIS_NRT", "MODIS"),
)

_GRID_COLUMNS = ["sensor", "date", "cell_lat", "cell_lon", "n_hotspots", "frp_sum"]
_HARMONIZED_COLUMNS = _GRID_COLUMNS + ["harmonized", "scale_note"]
_DETECTION_COLUMNS = [
    "sensor",
    "satellite",
    "instrument",
    "date",
    "hhmm",
    "lat",
    "lon",
    "frp",
    "brightness",
    "daynight",
    "type",
    "confidence",
    "conf_class",
]

_VIIRS_PLATFORM = {
    "SNPP",
    "SUOMI-NPP",
    "NPP",
    "N",
    "NOAA-20",
    "NOAA-21",
    "N20",
    "N21",
    "JPSS-1",
    "JPSS-2",
}
_MODIS_PLATFORM = {"TERRA", "AQUA", "MODIS"}


class FirmsFetchError(RuntimeError):
    """Live FIRMS download failed. The message never includes the map key."""


def active_k() -> tuple[float, str]:
    """Return ``(k, where it came from)``.

    A blank, non-numeric, or non-positive ``HARMONIZE_K`` falls back to
    ``DEFAULT_K`` so a bad environment variable cannot crash the app.
    """

    raw = os.environ.get(K_ENV, "").strip()
    if not raw:
        return DEFAULT_K, "bundled sample (73,334 VIIRS / 14,129 MODIS hotspots)"
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_K, "bundled sample because HARMONIZE_K is not a number"
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_K, "bundled sample because HARMONIZE_K must be positive"
    return value, "HARMONIZE_K environment variable"


def _sensor_token(value, column: str) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip().upper()
    if text in {"", "NAN", "NONE", "<NA>", "NAT"}:
        return None
    if "VIIRS" in text:
        return "VIIRS"
    if "MODIS" in text:
        return "MODIS"
    if column in {"instrument", "satellite", "sensor"} and text in _VIIRS_PLATFORM:
        return "VIIRS"
    if column in {"instrument", "satellite", "sensor"} and text in _MODIS_PLATFORM:
        return "MODIS"
    return None


def assign_sensors(df: pd.DataFrame) -> pd.Series:
    """Label each row MODIS or VIIRS.

    Prefers an explicit sensor column, then instrument, then satellite.
    ``SNPP`` / ``N`` are VIIRS. ``Terra`` / ``Aqua`` are MODIS.
    """

    chosen = pd.Series(pd.NA, index=df.index, dtype="object")
    for column in ("sensor", "instrument", "satellite"):
        if column not in df.columns:
            continue
        mapped = df[column].map(lambda value, column=column: _sensor_token(value, column))
        chosen = chosen.where(chosen.notna(), mapped)
    return chosen


def normalize(df: pd.DataFrame, sensor: str) -> pd.DataFrame:
    """Map one FIRMS table onto the common schema.

    Confidence handling matches the notebook: numeric scores use bins
    ``[-1, 29, 79, 100]`` and the letters l / n / h map to low / nominal / high.
    Full words are accepted too, so a file that was already labeled still works.
    Missing ``type`` becomes 0, which is what the notebook does when the column
    is absent. Rows are not dropped here.
    """

    work = df.reset_index(drop=True)
    if "acq_time" in work.columns:
        hhmm = (
            work["acq_time"]
            .astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(4)
        )
    else:
        hhmm = "0000"

    if "type" in work.columns:
        fire_type = pd.to_numeric(work["type"], errors="coerce").fillna(0)
    else:
        fire_type = 0

    if "confidence" in work.columns:
        confidence = work["confidence"].astype(str).str.strip().str.lower()
        confidence = confidence.mask(confidence.isin(["nan", "none", ""]))
    else:
        confidence = pd.Series(pd.NA, index=work.index, dtype="object")

    numeric = pd.to_numeric(confidence, errors="coerce")
    classes = pd.cut(
        numeric, [-1, 29, 79, 100], labels=["low", "nominal", "high"]
    ).astype(object)
    letters = confidence.map(
        {
            "l": "low",
            "n": "nominal",
            "h": "high",
            "low": "low",
            "nominal": "nominal",
            "high": "high",
        }
    )

    if "brightness" in work.columns:
        brightness = pd.to_numeric(work["brightness"], errors="coerce")
    elif "bright_ti4" in work.columns:
        brightness = pd.to_numeric(work["bright_ti4"], errors="coerce")
    else:
        brightness = np.nan
    instrument = work["instrument"] if "instrument" in work.columns else ""

    out = pd.DataFrame(
        {
            "sensor": sensor,
            "satellite": work["satellite"] if "satellite" in work.columns else "",
            "instrument": instrument,
            "date": pd.to_datetime(work["acq_date"], errors="coerce"),
            "hhmm": hhmm,
            "lat": pd.to_numeric(work["latitude"], errors="coerce"),
            "lon": pd.to_numeric(work["longitude"], errors="coerce"),
            "frp": pd.to_numeric(work["frp"], errors="coerce")
            if "frp" in work.columns
            else np.nan,
            "brightness": brightness,
            "daynight": work["daynight"] if "daynight" in work.columns else "",
            "type": fire_type,
            "confidence": confidence,
            "conf_class": classes.where(numeric.notna(), letters),
        }
    )
    return out


def _empty_grid() -> pd.DataFrame:
    return pd.DataFrame(columns=_GRID_COLUMNS)


def aggregate(df: pd.DataFrame, grid: float = GRID_DEG) -> pd.DataFrame:
    """Notebook grid: nominal+high, type 0, ~1 km dedupe, 0.25 degree counts."""

    if df is None or df.empty:
        return _empty_grid()
    work = df[df["conf_class"].isin(["nominal", "high"]) & (df["type"] == 0)].copy()
    work = work.dropna(subset=["lat", "lon", "date"])
    if work.empty:
        return _empty_grid()
    work["k_lat"] = (work["lat"] / DEDUPE_DEG).round().astype("int32")
    work["k_lon"] = (work["lon"] / DEDUPE_DEG).round().astype("int32")
    work = work.drop_duplicates(["sensor", "date", "k_lat", "k_lon"])
    work["cell_lat"] = (np.floor(work["lat"] / grid) * grid + grid / 2).round(4)
    work["cell_lon"] = (np.floor(work["lon"] / grid) * grid + grid / 2).round(4)
    return (
        work.groupby(["sensor", "date", "cell_lat", "cell_lon"], as_index=False)
        .agg(n_hotspots=("frp", "size"), frp_sum=("frp", "sum"))
        .reset_index(drop=True)
    )


def prepare_detections(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a mixed MODIS/VIIRS FIRMS table and drop invalid coordinates."""

    if df is None or df.empty:
        return pd.DataFrame(columns=_DETECTION_COLUMNS)
    work = df.copy()
    work.columns = [str(column).lower().strip() for column in work.columns]
    if "latitude" not in work.columns and "lat" in work.columns:
        work = work.rename(columns={"lat": "latitude", "lon": "longitude"})
    if "acq_date" not in work.columns and "date" in work.columns:
        work = work.rename(columns={"date": "acq_date"})
    missing = [
        column
        for column in ("latitude", "longitude", "acq_date")
        if column not in work.columns
    ]
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    sensors = assign_sensors(work)
    if sensors.notna().sum() == 0:
        raise ValueError(
            "Could not tell MODIS from VIIRS. Need a sensor, instrument, or satellite column."
        )
    parts = []
    for name in sensors.dropna().unique():
        parts.append(normalize(work.loc[sensors == name], str(name)))
    if not parts:
        return pd.DataFrame(columns=_DETECTION_COLUMNS)
    out = pd.concat(parts, ignore_index=True)
    out = out.dropna(subset=["lat", "lon", "date"])
    out = out[out["lat"].between(-90, 90) & out["lon"].between(-180, 180)]
    return out.reset_index(drop=True)


def estimate_k(daily: pd.DataFrame) -> float | None:
    """``k = sum(VIIRS hotspots) / sum(MODIS hotspots)``, or None if either is zero."""

    if daily is None or daily.empty or "sensor" not in daily.columns:
        return None
    totals = daily.groupby("sensor")["n_hotspots"].sum()
    viirs = float(totals.get("VIIRS", 0) or 0)
    modis = float(totals.get("MODIS", 0) or 0)
    if viirs <= 0 or modis <= 0:
        return None
    return viirs / modis


def select_harmonized(
    daily: pd.DataFrame,
    k: float,
    splice_date: pd.Timestamp = SPLICE_DATE,
) -> tuple[pd.DataFrame, str]:
    """Pick the spliced MODIS-equivalent grid and a one-sentence explanation.

    On and after ``splice_date``, a day that contains VIIRS uses VIIRS counts
    divided by k, and MODIS on that same day is left out. A day with no VIIRS
    keeps unscaled MODIS so the map is not blank. Before the splice, MODIS is
    the series and VIIRS is used only when that day has no MODIS.
    """

    empty = pd.DataFrame(columns=_HARMONIZED_COLUMNS)
    if daily is None or daily.empty:
        return empty, (
            "No nominal or high confidence vegetation detections in this filter, "
            "so the harmonized grid is empty."
        )
    if not math.isfinite(k) or k <= 0:
        raise ValueError("k must be a positive number")

    work = daily.copy()
    work["date"] = pd.to_datetime(work["date"])
    splice = pd.Timestamp(splice_date)
    after = work["date"] >= splice
    day_key = work["date"].dt.normalize()
    viirs_days = set(day_key[after & work["sensor"].eq("VIIRS")])
    keep = (
        (after & work["sensor"].eq("VIIRS"))
        | (~after & work["sensor"].eq("MODIS"))
        | (after & work["sensor"].eq("MODIS") & ~day_key.isin(viirs_days))
    )
    # VIIRS rows from before the splice are unused when MODIS exists for that era.
    # If a pre-splice day has only VIIRS, still scale it so the day is not blank.
    early_viirs_days = set(day_key[~after & work["sensor"].eq("VIIRS")])
    early_modis_days = set(day_key[~after & work["sensor"].eq("MODIS")])
    keep = keep | (
        ~after
        & work["sensor"].eq("VIIRS")
        & day_key.isin(early_viirs_days - early_modis_days)
    )
    chosen = work.loc[keep].copy()
    if chosen.empty:
        return empty, "No MODIS or VIIRS rows are left after the harmonization rules."
    viirs_mask = chosen["sensor"].eq("VIIRS")
    chosen["harmonized"] = chosen["n_hotspots"].astype(float)
    chosen.loc[viirs_mask, "harmonized"] = chosen.loc[viirs_mask, "n_hotspots"] / k
    chosen["scale_note"] = "MODIS"
    chosen.loc[viirs_mask, "scale_note"] = "VIIRS / k"
    fallback = ~viirs_mask & (chosen["date"] >= splice)
    chosen.loc[fallback, "scale_note"] = "MODIS reference"
    note = (
        f"Cells on or after {splice.date().isoformat()} are VIIRS hotspots "
        f"divided by k = {k:.3f} (MODIS-equivalent). "
        "A day with no VIIRS shows unscaled MODIS instead of a blank map. "
        "The two sensors are not added together."
    )
    return chosen[_HARMONIZED_COLUMNS], note


def _public_firms_text(text: str, key: str) -> str:
    redacted = text.replace(key, "") if key else text
    redacted = " ".join(redacted.split())
    if not redacted:
        return "empty response"
    if "<html" in redacted[:200].lower():
        return "FIRMS returned a web page instead of a CSV"
    return redacted[:160]


def _looks_like_firms_csv(text: str) -> bool:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines:
        return False
    header = lines[0].lower()
    return "latitude" in header and "longitude" in header


def fetch_firms_nrt(
    map_key: str, day_range: int = 1, timeout: int = 90
) -> tuple[pd.DataFrame, str]:
    """Download the latest global VIIRS SNPP NRT and MODIS NRT day.

    Returns ``(table, warning)``. ``warning`` is empty when both sources load.
    Raises ``FirmsFetchError`` when nothing usable comes back. The exception
    text does not contain the map key or the request URL.
    """

    key = (map_key or "").strip()
    if not key:
        raise FirmsFetchError("FIRMS_MAP_KEY is not set.")
    days = min(5, max(1, int(day_range)))
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    headers = {"User-Agent": "EmberScope/1.0 (NASA Space Apps educational project)"}
    for source, sensor_name in NRT_SOURCES:
        url = FIRMS_AREA_CSV.format(key=key, source=source, days=days)
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
        except requests.RequestException:
            errors.append(f"{source}: request failed")
            continue
        body = response.text or ""
        if response.status_code != 200 or not _looks_like_firms_csv(body):
            errors.append(f"{source}: {_public_firms_text(body, key) or 'HTTP ' + str(response.status_code)}")
            continue
        try:
            frame = pd.read_csv(io.StringIO(body.lstrip("\ufeff")))
        except pd.errors.ParserError:
            errors.append(f"{source}: could not parse the CSV")
            continue
        frame["sensor"] = sensor_name
        frames.append(frame)

    if not frames:
        detail = "; ".join(errors) if errors else "no data"
        raise FirmsFetchError(f"Could not download live FIRMS data ({detail}).")
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        detail = "; ".join(errors) if errors else "the latest day has no rows"
        raise FirmsFetchError(f"FIRMS returned no detections ({detail}).")
    warning = ""
    if errors:
        warning = "Some live sources did not load: " + "; ".join(errors)
    if key and key in warning:
        warning = warning.replace(key, "")
    return combined, warning


def read_data_uri(uri: str) -> pd.DataFrame:
    """Read a CSV or Parquet path. ``gs://`` uses Application Default Credentials."""

    target = uri.strip()
    if target.startswith("gs://"):
        return _read_gcs(target)
    if target.lower().endswith(".csv"):
        return pd.read_csv(target)
    return pd.read_parquet(target)


def _read_gcs(uri: str) -> pd.DataFrame:
    rest = uri[5:]
    bucket_name, _, blob_name = rest.partition("/")
    if not bucket_name or not blob_name:
        raise ValueError("EMBER_DATA_URI must look like gs://bucket/path.parquet")
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-storage is not installed, so gs:// paths cannot be read."
        ) from exc
    payload = storage.Client().bucket(bucket_name).blob(blob_name).download_as_bytes()
    buffer = io.BytesIO(payload)
    if blob_name.lower().endswith(".csv"):
        return pd.read_csv(buffer)
    return pd.read_parquet(buffer)


def standardize_grid(df: pd.DataFrame) -> pd.DataFrame:
    """Accept an ``aggregate`` result (or the same columns under nearby names)."""

    work = df.copy()
    work.columns = [str(column).lower().strip() for column in work.columns]
    if "date" not in work.columns:
        if "acq_date" in work.columns:
            work["date"] = work["acq_date"]
        elif "ym" in work.columns:
            work["date"] = work["ym"]
        else:
            raise ValueError("Grid file needs a date, acq_date, or ym column.")
    for column in ("sensor", "cell_lat", "cell_lon", "n_hotspots"):
        if column not in work.columns:
            raise ValueError(f"Grid file is missing {column}.")
    if "frp_sum" not in work.columns:
        work["frp_sum"] = np.nan
    out = pd.DataFrame(
        {
            "sensor": work["sensor"].map(
                lambda value: _sensor_token(value, "sensor") or str(value).strip().upper()
            ),
            "date": pd.to_datetime(work["date"], errors="coerce"),
            "cell_lat": pd.to_numeric(work["cell_lat"], errors="coerce"),
            "cell_lon": pd.to_numeric(work["cell_lon"], errors="coerce"),
            "n_hotspots": pd.to_numeric(work["n_hotspots"], errors="coerce"),
            "frp_sum": pd.to_numeric(work["frp_sum"], errors="coerce"),
        }
    )
    out = out.dropna(subset=["date", "cell_lat", "cell_lon", "n_hotspots", "sensor"])
    return out.reset_index(drop=True)


def is_detection_table(df: pd.DataFrame) -> bool:
    columns = {str(column).lower().strip() for column in df.columns}
    return "latitude" in columns or "lat" in columns
