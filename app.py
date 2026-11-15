import json
import math
import os
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st

from harmonize import (
    DATA_URI_ENV,
    FIRMS_KEY_ENV,
    FirmsFetchError,
    active_k,
    aggregate,
    fetch_firms_nrt,
    is_detection_table,
    prepare_detections,
    read_data_uri,
    select_harmonized,
    standardize_grid,
)

DATA_PATH = Path(__file__).parent / "app_data.csv"
CARTO_DARK = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
MAX_MAP_POINTS = 50_000
LIVE_RETRY_SECONDS = 120

CONF_COLORS = {
    "high": (239, 68, 68),
    "nominal": (251, 146, 60),
    "low": (148, 163, 184),
    "unknown": (100, 116, 139),
}


def public_error(exc: BaseException) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    key = os.environ.get(FIRMS_KEY_ENV, "").strip()
    if key:
        text = text.replace(key, "[redacted]")
    return " ".join(text.split())[:240]


def classify_table(table: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    if is_detection_table(table):
        return "detections", prepare_detections(table)
    return "grid", standardize_grid(table)


def widget_token(origin: str, frame: pd.DataFrame) -> str:
    if frame.empty or "date" not in frame.columns:
        return origin
    dates = pd.to_datetime(frame["date"])
    return f"{origin}-{len(frame)}-{dates.min().date()}-{dates.max().date()}"


def parse_date_range(value, fallback_start, fallback_end):
    if value is None:
        return fallback_start, fallback_end
    if isinstance(value, (tuple, list)):
        if len(value) == 0:
            return fallback_start, fallback_end
        if len(value) == 1 or value[1] is None:
            return value[0], value[0]
        start, end = value[0], value[1]
    else:
        start = end = value
    if start > end:
        start, end = end, start
    return start, end


def limit_points(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if len(frame) <= MAX_MAP_POINTS:
        return frame
    if column in frame.columns and frame[column].notna().any():
        return frame.nlargest(MAX_MAP_POINTS, column)
    return frame.head(MAX_MAP_POINTS)


def as_records(frame: pd.DataFrame) -> list[dict]:
    payload = frame.copy()
    for column in payload.columns:
        if pd.api.types.is_datetime64_any_dtype(payload[column]):
            payload[column] = payload[column].dt.strftime("%Y-%m-%d")
    return json.loads(payload.to_json(orient="records", double_precision=4))


def attach_confidence_colors(frame: pd.DataFrame) -> pd.DataFrame:
    colors = (
        frame["conf_class"]
        .fillna("unknown")
        .astype(str)
        .map(CONF_COLORS)
        .apply(lambda item: item if isinstance(item, tuple) else CONF_COLORS["unknown"])
    )
    rgb = pd.DataFrame(
        colors.tolist(), columns=["fill_r", "fill_g", "fill_b"], index=frame.index
    )
    return pd.concat([frame, rgb], axis=1)


def attach_count_colors(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    out = frame.copy()
    values = pd.to_numeric(out[column], errors="coerce").fillna(0).clip(lower=0)
    logged = values.map(lambda value: math.log10(1.0 + float(value)))
    hi = float(logged.quantile(0.95)) if len(logged) else 1.0
    if not pd.notna(hi) or hi <= 0:
        hi = 1.0
    tone = (logged / hi).clip(0, 1)
    out["fill_r"] = 255
    out["fill_g"] = (210 - 170 * tone).round().astype(int)
    out["fill_b"] = (50 - 30 * tone).round().astype(int)
    return out


def style_chart(fig):
    fig.update_layout(
        plot_bgcolor="#111827",
        paper_bgcolor="#0b1120",
        font_color="#f8fafc",
        margin=dict(l=16, r=16, t=48, b=16),
    )
    return fig


def show_map(records: list[dict], tooltip: dict, radius_min: int, radius_max: int, map_key: str) -> None:
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=records,
        get_position="[lon, lat]",
        get_radius="radius",
        get_fill_color="[fill_r, fill_g, fill_b, 210]",
        pickable=True,
        opacity=0.85,
        stroked=False,
        radius_min_pixels=radius_min,
        radius_max_pixels=radius_max,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(
            latitude=15,
            longitude=0,
            zoom=1.05,
            min_zoom=0.4,
            max_zoom=10,
            pitch=0,
            bearing=0,
        ),
        tooltip=tooltip,
        map_style=CARTO_DARK,
    )
    st.pydeck_chart(deck, width="stretch", height=560, on_select="ignore", key=map_key)


def legend_html(kind: str) -> str:
    swatch = (
        "display:inline-block;width:12px;height:12px;border-radius:99px;"
        "margin-right:6px;vertical-align:-1px;"
    )
    if kind == "confidence":
        return f"""
        <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:14px;color:#e2e8f0;">
          <span><span style="{swatch}background:#ef4444;"></span>High confidence</span>
          <span><span style="{swatch}background:#fb923c;"></span>Nominal confidence</span>
          <span><span style="{swatch}background:#94a3b8;"></span>Low confidence</span>
        </div>
        """
    return """
    <div style="display:flex;align-items:center;gap:10px;font-size:14px;color:#e2e8f0;">
      <span>Fewer</span>
      <span style="display:inline-block;width:160px;height:12px;border-radius:99px;
        background:linear-gradient(90deg,#ffe14a,#ff8a1e,#ff2d2d);"></span>
      <span>More MODIS-equivalent hotspots</span>
    </div>
    """


def daynight_label(value: str) -> str:
    return {"D": "Day", "N": "Night"}.get(str(value).strip().upper(), str(value))


st.set_page_config(
    page_title="EmberScope",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .stApp { background-color: #0b1120; color: #f1f5f9; }
    [data-testid="stSidebar"] { background-color: #111827; }
    [data-testid="stMetric"] {
        background-color: #172033;
        padding: 18px;
        border-radius: 10px;
        border: 1px solid #29364c;
    }
    h1, h2, h3 { color: #f8fafc; }
    .subtitle { color: #94a3b8; font-size: 15px; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def load_bundled_detections(path: str) -> pd.DataFrame:
    return prepare_detections(pd.read_csv(path))


@st.cache_data(show_spinner=False)
def load_uri_table(uri: str) -> pd.DataFrame:
    return read_data_uri(uri)


@st.cache_data(ttl=600, show_spinner=False)
def cached_live_raw(map_key: str):
    return fetch_firms_nrt(map_key)


def load_saved_copy() -> pd.DataFrame:
    if not DATA_PATH.exists():
        raise FileNotFoundError("app_data.csv was not found next to app.py.")
    return load_bundled_detections(str(DATA_PATH)).copy()


def load_live(map_key: str):
    failure = st.session_state.get("live_fail")
    now = time.time()
    if failure and failure.get("key") == map_key and now < failure.get("until", 0):
        return None, failure.get("message", "Live data is unavailable.")
    try:
        raw, warning = cached_live_raw(map_key)
        detections = prepare_detections(raw)
    except Exception as exc:
        if isinstance(exc, FirmsFetchError):
            message = str(exc)
        else:
            message = f"Live data could not be prepared ({public_error(exc)})."
        if map_key and map_key in message:
            message = message.replace(map_key, "[redacted]")
        st.session_state["live_fail"] = {
            "key": map_key,
            "until": now + LIVE_RETRY_SECONDS,
            "message": message,
        }
        return None, message
    if detections.empty:
        message = "FIRMS returned no usable detections for the latest day."
        st.session_state["live_fail"] = {
            "key": map_key,
            "until": now + LIVE_RETRY_SECONDS,
            "message": message,
        }
        return None, message
    st.session_state.pop("live_fail", None)
    return detections.copy(), warning or None


k_value, k_source = active_k()

st.sidebar.title("EmberScope")
st.sidebar.caption("MODIS and VIIRS on one scale")
st.sidebar.caption(f"k = {k_value:.3f}. {k_source}.")

requested = st.sidebar.radio(
    "Data source",
    ["Bundled sample", "Live (last 24h)", "Upload CSV"],
    help="Live loads the latest FIRMS day for VIIRS SNPP NRT and MODIS NRT, then caches it for 10 minutes.",
)

messages: list[tuple[str, str]] = []
mode = "detections"
origin = "Bundled sample"
frame = None

try:
    if requested == "Upload CSV":
        uploaded = st.sidebar.file_uploader(
            "FIRMS CSV",
            type=["csv"],
            help="A FIRMS export with latitude, longitude, and acq_date. Sensor or satellite is used to tell MODIS from VIIRS.",
        )
        if uploaded is None:
            messages.append(("info", "No CSV uploaded yet. Showing the bundled sample."))
            frame = load_saved_copy()
        else:
            mode, frame = classify_table(pd.read_csv(uploaded))
            frame = frame.copy()
            origin = uploaded.name
    elif requested == "Live (last 24h)":
        map_key = os.environ.get(FIRMS_KEY_ENV, "").strip()
        if not map_key:
            messages.append(
                (
                    "warning",
                    "Live data needs a NASA FIRMS map key in the FIRMS_MAP_KEY environment variable. "
                    "Showing the bundled September–November 2026 sample instead.",
                )
            )
            frame = load_saved_copy()
        else:
            with st.spinner("Fetching the latest FIRMS day…"):
                live_frame, live_message = load_live(map_key)
            if live_frame is None:
                messages.append(
                    (
                        "warning",
                        f"{live_message} Showing the bundled sample instead.",
                    )
                )
                frame = load_saved_copy()
            else:
                frame = live_frame
                origin = "FIRMS live (latest day, cached 10 minutes)"
                if live_message:
                    messages.append(("warning", live_message))
    else:
        uri = os.environ.get(DATA_URI_ENV, "").strip()
        if uri:
            try:
                mode, frame = classify_table(load_uri_table(uri))
                frame = frame.copy()
                origin = uri
            except Exception as exc:
                messages.append(
                    (
                        "warning",
                        f"Could not read EMBER_DATA_URI ({public_error(exc)}). "
                        "Showing the bundled CSV instead.",
                    )
                )
                mode = "detections"
                frame = load_saved_copy()
        else:
            frame = load_saved_copy()
except Exception as exc:
    st.error("EmberScope could not load a dataset, so the map was not drawn.")
    st.caption(public_error(exc))
    st.stop()

if frame is None or frame.empty:
    st.error("The dataset has no usable rows.")
    st.stop()

token = widget_token(origin, frame)
st.sidebar.header("Filters")

dates = pd.to_datetime(frame["date"])
min_date = dates.min().date()
max_date = dates.max().date()
date_value = (min_date, max_date)
date_range = st.sidebar.date_input(
    "Acquisition date range",
    value=date_value,
    min_value=min_date,
    max_value=max_date,
    key=f"dates-{token}",
    required=True,
)
start_date, end_date = parse_date_range(date_range, min_date, max_date)

sensors = sorted(frame["sensor"].dropna().astype(str).unique())
selected_sensors = st.sidebar.multiselect(
    "Sensor",
    sensors,
    default=sensors,
    key=f"sensor-{token}",
)

selected_satellites = None
selected_daynight = None
selected_classes = None
frp_range = None

if mode == "detections":
    satellites = sorted(
        value
        for value in frame["satellite"].dropna().astype(str).unique()
        if value and value.lower() != "nan"
    )
    if satellites:
        selected_satellites = st.sidebar.multiselect(
            "Satellite",
            satellites,
            default=satellites,
            key=f"sat-{token}",
        )
    classes = ["low", "nominal", "high"]
    present = set(frame["conf_class"].dropna().astype(str))
    classes = [name for name in classes if name in present] + sorted(
        present - {"low", "nominal", "high"}
    )
    if not classes:
        classes = ["low", "nominal", "high"]
    selected_classes = st.sidebar.multiselect(
        "Confidence class",
        classes,
        default=classes,
        key=f"conf-{token}",
        help="VIIRS letters l/n/h and MODIS scores 0–100 are both shown as low, nominal, or high.",
    )
    if "daynight" in frame.columns:
        periods = sorted(
            value
            for value in frame["daynight"].dropna().astype(str).unique()
            if value and value.lower() != "nan"
        )
        if periods:
            selected_daynight = st.sidebar.multiselect(
                "Day / Night",
                periods,
                default=periods,
                format_func=daynight_label,
                key=f"dn-{token}",
            )
    valid_frp = frame["frp"].dropna()
    if not valid_frp.empty and float(valid_frp.min()) < float(valid_frp.max()):
        frp_range = st.sidebar.slider(
            "Fire radiative power (MW)",
            min_value=float(valid_frp.min()),
            max_value=float(valid_frp.max()),
            value=(float(valid_frp.min()), float(valid_frp.max())),
            key=f"frp-{token}-{float(valid_frp.min()):.4f}-{float(valid_frp.max()):.4f}",
        )
else:
    st.sidebar.caption(
        "This file is already a 0.25° grid, so satellite, confidence, day/night, and FRP filters are not available."
    )

start_ts = pd.Timestamp(start_date).normalize()
end_ts = pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1)
filtered = frame[frame["date"].notna()].copy()
filtered = filtered[(filtered["date"] >= start_ts) & (filtered["date"] < end_ts)]
if selected_sensors is not None:
    filtered = filtered[filtered["sensor"].astype(str).isin(selected_sensors)]
if selected_satellites is not None:
    filtered = filtered[filtered["satellite"].astype(str).isin(selected_satellites)]
if selected_classes is not None:
    filtered = filtered[filtered["conf_class"].fillna("unknown").astype(str).isin(selected_classes)]
if selected_daynight is not None:
    filtered = filtered[filtered["daynight"].astype(str).isin(selected_daynight)]
if frp_range is not None:
    filtered = filtered[filtered["frp"].between(frp_range[0], frp_range[1])]

for level, text in messages:
    getattr(st, level)(text)

st.title("EmberScope")
st.markdown(
    '<p class="subtitle">NASA MODIS and VIIRS active-fire detections, harmonized onto the MODIS scale.</p>',
    unsafe_allow_html=True,
)
st.caption(f"Showing {origin}.")

with st.expander("How harmonization works, and its limits"):
    st.markdown(
        f"""
VIIRS sees smaller fires, so raw VIIRS counts sit higher than MODIS for the same
landscape. EmberScope uses the team's notebook method:

1. Map confidence onto one scale. VIIRS `l` / `n` / `h` and MODIS `0–100`
   (low below 30, nominal 30–79, high 80–100).
2. Keep **nominal and high** confidence and vegetation fires (`type == 0`).
3. Keep one detection per sensor per day in a 0.01° box (about 1 km), so a
   375 m VIIRS pixel and a 1 km MODIS pixel can be compared.
4. Count those hotspots in **0.25°** cells and sum FRP. k scales counts, not FRP.
5. `k = VIIRS hotspots / MODIS hotspots`. Here k = **{k_value:.3f}** ({k_source}).
   Adjusted VIIRS = VIIRS / k.
6. From **2012-02-01** on, a day with VIIRS uses adjusted VIIRS only. Earlier
   dates use MODIS. Adding both would count the same fire twice. A day that
   only has MODIS still draws those MODIS counts, labeled as the reference.

**Limits.** A single k drifts and can slightly underestimate recent peak months.
MODIS is the reference: the error says how close scaled VIIRS is to MODIS, not
how many real fires burned. Terra flew before Aqua joined in 2002, so the earliest
MODIS years run low. The notebook's per-continent check is one day (2026-09-15);
this app uses one global k. The Brazil 2012–2025 archive is not in this repo.
The bundled file is a 100,000-row sample from 2026-09-01 to 2026-11-08, and it
has no `type` column, so industrial sources in that file are not removed. Live
mode is the latest FIRMS near real-time day and can be revised.
        """
    )

if "map_view" not in st.session_state:
    requested_view = st.query_params.get("view", "raw")
    st.session_state["map_view"] = (
        "Harmonized grid" if requested_view == "grid" else "Raw detections"
    )
view = st.radio(
    "Map view",
    ["Raw detections", "Harmonized grid"],
    horizontal=True,
    key="map_view",
    width="stretch",
)

if view == "Raw detections":
    st.markdown(
        "Each point is one satellite detection that passed the sidebar filters. "
        "VIIRS confidence letters and MODIS numeric scores use the same three classes. "
        "Scroll to zoom and drag to pan. The map opens on the whole world; "
        "higher FRP is drawn larger as you zoom in."
    )
    st.markdown(legend_html("confidence"), unsafe_allow_html=True)
else:
    st.markdown(
        "Each circle is a 0.25° cell (about 28 km). The color is the MODIS-equivalent "
        f"hotspot count: VIIRS detections divided by k = {k_value:.3f}. "
        "Low-confidence detections stay in the raw view and are left out of this grid. "
        "FRP is summed, not divided by k."
    )
    st.markdown(legend_html("grid"), unsafe_allow_html=True)

if filtered.empty:
    st.warning("Nothing matches these filters. Widen the date range or turn a filter back on.")
    st.stop()

if mode == "detections":
    cell_grid = aggregate(filtered)
else:
    cell_grid = filtered.copy()
harmonized, harm_note = select_harmonized(cell_grid, k_value)

available_dates = sorted(filtered["date"].dt.date.dropna().unique().tolist())
default_day = available_dates[-1]
if not harmonized.empty:
    viirs_days = pd.to_datetime(
        harmonized.loc[harmonized["scale_note"].eq("VIIRS / k"), "date"]
    )
    if not viirs_days.empty:
        candidate = viirs_days.max().date()
        if candidate in available_dates:
            default_day = candidate
slider_key = f"day-{token}"
if slider_key not in st.session_state or st.session_state[slider_key] not in available_dates:
    st.session_state[slider_key] = default_day
st.subheader("Day on the map")
st.caption("The date range is in the sidebar. This slider picks which day the map draws.")
selected_date = st.select_slider(
    "Map day",
    options=available_dates,
    key=slider_key,
    format_func=lambda day: day.strftime("%B %d, %Y"),
)

day_rows = filtered[filtered["date"].dt.date == selected_date].copy()
day_cells = harmonized[pd.to_datetime(harmonized["date"]).dt.date == selected_date].copy() if not harmonized.empty else harmonized

st.markdown(f"### {selected_date.strftime('%B %d, %Y')}")
metric_cols = st.columns(4)
if view == "Harmonized grid":
    total_h = float(day_cells["harmonized"].sum()) if not day_cells.empty else 0.0
    frp_sum = float(day_cells["frp_sum"].sum()) if not day_cells.empty else 0.0
    metric_cols[0].metric("Harmonized hotspots", f"{total_h:,.1f}")
    metric_cols[1].metric("Grid cells", f"{len(day_cells):,}")
    metric_cols[2].metric("FRP sum (not scaled)", f"{frp_sum:,.0f} MW" if day_cells["frp_sum"].notna().any() else "N/A" if not day_cells.empty else "N/A")
    metric_cols[3].metric("Scale factor k", f"{k_value:.3f}")
elif mode == "detections":
    mean_frp = day_rows["frp"].mean()
    max_frp = day_rows["frp"].max()
    metric_cols[0].metric("Detections", f"{len(day_rows):,}")
    metric_cols[1].metric("Average FRP", f"{mean_frp:,.1f} MW" if pd.notna(mean_frp) else "N/A")
    metric_cols[2].metric("Maximum FRP", f"{max_frp:,.1f} MW" if pd.notna(max_frp) else "N/A")
    metric_cols[3].metric("Scale factor k", f"{k_value:.3f}")
else:
    hotspots = float(day_rows["n_hotspots"].sum()) if "n_hotspots" in day_rows.columns else 0.0
    frp_sum = float(day_rows["frp_sum"].sum()) if "frp_sum" in day_rows.columns else 0.0
    metric_cols[0].metric("Hotspots in cells", f"{hotspots:,.0f}")
    metric_cols[1].metric("Grid cells", f"{len(day_rows):,}")
    metric_cols[2].metric("FRP sum", f"{frp_sum:,.0f} MW")
    metric_cols[3].metric("Scale factor k", f"{k_value:.3f}")

st.subheader("Global map")
tooltip_style = {"backgroundColor": "#172033", "color": "white", "fontSize": "13px"}

if view == "Raw detections" and mode == "detections":
    map_data = limit_points(day_rows.dropna(subset=["lat", "lon"]), "frp")
    if map_data.empty:
        st.info("This day has no detections to draw.")
    else:
        map_data = attach_confidence_colors(map_data)
        frp_display = map_data["frp"].fillna(0).clip(lower=0, upper=200)
        map_data["radius"] = 4000 + frp_display * 150
        map_data["date_label"] = map_data["date"].dt.strftime("%Y-%m-%d")
        map_data["frp_label"] = map_data["frp"].round(1).fillna(0)
        map_data["conf_label"] = map_data["conf_class"].fillna("unknown")
        records = as_records(
            map_data[
                [
                    "lon",
                    "lat",
                    "radius",
                    "fill_r",
                    "fill_g",
                    "fill_b",
                    "satellite",
                    "sensor",
                    "date_label",
                    "conf_label",
                    "frp_label",
                ]
            ]
        )
        show_map(
            records,
            {
                "html": (
                    "<b>{satellite}</b> · {sensor}<br/>{date_label}<br/>"
                    "Confidence: {conf_label}<br/>FRP: {frp_label} MW"
                ),
                "style": tooltip_style,
            },
            radius_min=2,
            radius_max=14,
            map_key="map-raw",
        )
        extra = ""
        if len(day_rows) > len(map_data):
            extra = f" Showing the {len(map_data):,} highest-FRP rows."
        st.caption(
            f"{len(map_data):,} detections on {selected_date.strftime('%B %d, %Y')}."
            + extra
        )
elif view == "Raw detections":
    map_data = limit_points(day_rows.dropna(subset=["cell_lat", "cell_lon"]), "n_hotspots")
    if map_data.empty:
        st.info("This day has no grid cells to draw.")
    else:
        drawn = map_data.rename(columns={"cell_lon": "lon", "cell_lat": "lat"})
        drawn = attach_count_colors(drawn, "n_hotspots")
        drawn["radius"] = 12000
        drawn["date_label"] = pd.to_datetime(drawn["date"]).dt.strftime("%Y-%m-%d")
        drawn["count_label"] = drawn["n_hotspots"].round(0)
        records = as_records(
            drawn[
                ["lon", "lat", "radius", "fill_r", "fill_g", "fill_b", "sensor", "date_label", "count_label"]
            ]
        )
        show_map(
            records,
            {
                "html": "<b>{sensor}</b> grid cell<br/>{date_label}<br/>Hotspots: {count_label}",
                "style": tooltip_style,
            },
            radius_min=3,
            radius_max=22,
            map_key="map-grid-raw",
        )
        st.caption("These rows were already aggregated to 0.25° cells in the source file.")
else:
    st.caption(harm_note)
    if day_cells.empty:
        st.info(
            "The harmonized grid is empty for this day. Low-confidence rows are kept in the raw view, "
            "and the splice uses VIIRS after 2012-02-01."
        )
    else:
        drawn = day_cells.rename(columns={"cell_lon": "lon", "cell_lat": "lat"})
        drawn = attach_count_colors(drawn, "harmonized")
        drawn["radius"] = 12000
        drawn["date_label"] = pd.to_datetime(drawn["date"]).dt.strftime("%Y-%m-%d")
        drawn["harmonized_label"] = drawn["harmonized"].round(2)
        drawn["count_label"] = drawn["n_hotspots"].round(0)
        drawn["frp_label"] = drawn["frp_sum"].round(1).fillna(0)
        records = as_records(
            drawn[
                [
                    "lon",
                    "lat",
                    "radius",
                    "fill_r",
                    "fill_g",
                    "fill_b",
                    "sensor",
                    "date_label",
                    "harmonized_label",
                    "count_label",
                    "frp_label",
                    "scale_note",
                ]
            ]
        )
        show_map(
            records,
            {
                "html": (
                    "<b>{scale_note}</b><br/>{date_label}<br/>"
                    "MODIS-equivalent hotspots: {harmonized_label}<br/>"
                    "Detections kept in the cell: {count_label}<br/>"
                    "FRP sum: {frp_label} MW<br/>{sensor}"
                ),
                "style": tooltip_style,
            },
            radius_min=3,
            radius_max=22,
            map_key="map-harmonized",
        )
        st.caption(
            f"{len(day_cells):,} cells on {selected_date.strftime('%B %d, %Y')}. "
            "A fractional count is a VIIRS total divided by k."
        )

st.divider()
st.subheader("Across the selected dates")
range_cols = st.columns(4)
if mode == "detections":
    mean_frp = filtered["frp"].mean()
    max_frp = filtered["frp"].max()
    range_cols[0].metric("Detections in range", f"{len(filtered):,}")
    range_cols[1].metric("Average FRP", f"{mean_frp:,.1f} MW" if pd.notna(mean_frp) else "N/A")
    range_cols[2].metric("Maximum FRP", f"{max_frp:,.1f} MW" if pd.notna(max_frp) else "N/A")
else:
    range_cols[0].metric("Hotspots in range", f"{filtered['n_hotspots'].sum():,.0f}")
    range_cols[1].metric("Grid cells", f"{len(filtered):,}")
    range_cols[2].metric("FRP sum", f"{filtered['frp_sum'].sum():,.0f} MW")
range_cols[3].metric("Satellites" if mode == "detections" else "Sensors", filtered["satellite"].nunique() if mode == "detections" and "satellite" in filtered.columns else filtered["sensor"].nunique())

if mode == "detections":
    daily = filtered.groupby(filtered["date"].dt.floor("D")).size().reset_index(name="Detections")
    daily = daily.rename(columns={"date": "Date"})
    fig_daily = px.line(
        daily,
        x="Date",
        y="Detections",
        markers=True,
        title="Raw detections per day",
        template="plotly_dark",
    )
    fig_daily.update_traces(line=dict(width=3, color="#fb923c"))
    st.plotly_chart(style_chart(fig_daily), width="stretch", theme=None)
else:
    daily = (
        filtered.groupby(filtered["date"].dt.floor("D"))["n_hotspots"]
        .sum()
        .reset_index(name="Hotspots")
        .rename(columns={"date": "Date"})
    )
    fig_daily = px.line(
        daily,
        x="Date",
        y="Hotspots",
        markers=True,
        title="Gridded hotspots per day",
        template="plotly_dark",
    )
    st.plotly_chart(style_chart(fig_daily), width="stretch", theme=None)

if not harmonized.empty:
    harmonized_daily = (
        harmonized.assign(day=pd.to_datetime(harmonized["date"]).dt.floor("D"))
        .groupby("day")["harmonized"]
        .sum()
        .reset_index(name="Harmonized hotspots")
        .rename(columns={"day": "Date"})
    )
    fig_harm = px.line(
        harmonized_daily,
        x="Date",
        y="Harmonized hotspots",
        markers=True,
        title="Harmonized hotspots per day (MODIS-equivalent)",
        template="plotly_dark",
    )
    fig_harm.update_traces(line=dict(width=3, color="#f97316"))
    st.plotly_chart(style_chart(fig_harm), width="stretch", theme=None)
    st.caption(harm_note)

st.subheader("MODIS and VIIRS")
left, right = st.columns(2)
with left:
    if mode == "detections":
        counts = filtered["sensor"].value_counts().reset_index()
        counts.columns = ["Sensor", "Detections"]
        fig_pie = px.pie(
            counts,
            names="Sensor",
            values="Detections",
            hole=0.45,
            title="Raw detections by sensor",
            template="plotly_dark",
        )
        st.plotly_chart(style_chart(fig_pie), width="stretch", theme=None)
        st.caption("Every row, including low confidence.")
    else:
        counts = filtered.groupby("sensor")["n_hotspots"].sum().reset_index()
        counts.columns = ["Sensor", "Hotspots"]
        fig_pie = px.pie(
            counts,
            names="Sensor",
            values="Hotspots",
            hole=0.45,
            title="Hotspots by sensor",
            template="plotly_dark",
        )
        st.plotly_chart(style_chart(fig_pie), width="stretch", theme=None)
with right:
    if not cell_grid.empty:
        totals = cell_grid.groupby("sensor")["n_hotspots"].sum()
        viirs_n = float(totals.get("VIIRS", 0) or 0)
        modis_n = float(totals.get("MODIS", 0) or 0)
        comparison = pd.DataFrame(
            {
                "Series": ["MODIS", "VIIRS raw", "VIIRS ÷ k"],
                "Hotspots": [modis_n, viirs_n, viirs_n / k_value],
            }
        )
        fig_bar = px.bar(
            comparison,
            x="Series",
            y="Hotspots",
            title="1 km hotspots, nominal + high",
            template="plotly_dark",
            color="Series",
            color_discrete_sequence=["#38bdf8", "#fb923c", "#f97316"],
        )
        st.plotly_chart(style_chart(fig_bar), width="stretch", theme=None)
        st.caption(
            "Same filters, after the 1 km dedupe. VIIRS ÷ k should sit near MODIS "
            "when both sensors saw the same days."
        )
    else:
        st.info("No nominal or high confidence vegetation detections in this filter, so k is not applied.")

if mode == "detections" and "daynight" in filtered.columns:
    st.subheader("Day and night")
    period = filtered["daynight"].map(daynight_label).value_counts().reset_index()
    period.columns = ["Period", "Detections"]
    fig_period = px.bar(
        period,
        x="Period",
        y="Detections",
        color="Period",
        title="Detections by time of day",
        template="plotly_dark",
    )
    st.plotly_chart(style_chart(fig_period), width="stretch", theme=None)

st.divider()
st.subheader("Rows behind the map")
st.caption("The table and the download use the full sidebar date range. The map uses the selected day.")

if view == "Harmonized grid":
    table = harmonized.copy()
    if table.empty:
        st.info("The harmonized table is empty for this filter.")
    else:
        table["date"] = pd.to_datetime(table["date"]).dt.strftime("%Y-%m-%d")
        table["harmonized"] = table["harmonized"].round(2)
        table["frp_sum"] = table["frp_sum"].round(1)
        table = table.rename(
            columns={
                "date": "Date",
                "sensor": "Sensor",
                "cell_lat": "Cell latitude",
                "cell_lon": "Cell longitude",
                "n_hotspots": "Detections in cell",
                "frp_sum": "FRP sum (MW)",
                "harmonized": "Harmonized hotspots",
                "scale_note": "Rule",
            }
        )
        st.dataframe(table.head(1000), width="stretch", hide_index=True)
        st.download_button(
            "Download harmonized grid",
            data=table.to_csv(index=False).encode("utf-8"),
            file_name="emberscope_harmonized.csv",
            mime="text/csv",
        )
elif mode == "detections":
    table = filtered.copy()
    table["date"] = table["date"].dt.strftime("%Y-%m-%d")
    table["daynight"] = table["daynight"].map(daynight_label)
    table["frp"] = table["frp"].round(2)
    table["lat"] = table["lat"].round(4)
    table["lon"] = table["lon"].round(4)
    keep = [
        "date",
        "sensor",
        "satellite",
        "lat",
        "lon",
        "frp",
        "confidence",
        "conf_class",
        "daynight",
        "type",
    ]
    table = table[keep].rename(
        columns={
            "date": "Date",
            "sensor": "Sensor",
            "satellite": "Satellite",
            "lat": "Latitude",
            "lon": "Longitude",
            "frp": "FRP (MW)",
            "confidence": "Confidence",
            "conf_class": "Class",
            "daynight": "Day / Night",
            "type": "Type",
        }
    )
    st.dataframe(table.head(1000), width="stretch", hide_index=True)
    st.download_button(
        "Download filtered detections",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name="emberscope_detections.csv",
        mime="text/csv",
    )
else:
    table = filtered.copy()
    table["date"] = pd.to_datetime(table["date"]).dt.strftime("%Y-%m-%d")
    table = table.rename(
        columns={
            "date": "Date",
            "sensor": "Sensor",
            "cell_lat": "Cell latitude",
            "cell_lon": "Cell longitude",
            "n_hotspots": "Hotspots",
            "frp_sum": "FRP sum (MW)",
        }
    )
    st.dataframe(table.head(1000), width="stretch", hide_index=True)
    st.download_button(
        "Download grid",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name="emberscope_grid.csv",
        mime="text/csv",
    )

st.divider()
st.markdown(
    """
<div style="text-align:center;color:#64748b;">
EmberScope · NASA Space Apps 2026 · MODIS and VIIRS active fire, on one scale<br/>
Detections from NASA FIRMS. MODIS is the reference scale.
</div>
""",
    unsafe_allow_html=True,
)
