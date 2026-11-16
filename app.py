import json
import math
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st

from archive import (
    MAX_RENDER_POINTS,
    archive_location,
    bbox_is_world,
    default_archive_dates,
    burn_calendar,
    cells_from_points,
    daily_from_cells,
    GRID_TOOLTIP_FIELDS,
    GRID_TOOLTIP_HTML,
    grid_tooltip_frame,
    harmonize_archive_grid,
    load_daily_grid,
    chart_date_window,
    load_daily_totals,
    load_manifest,
    load_points,
    should_load_points,
)
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

BUNDLED_CSV = Path(__file__).parent / "app_data.csv"
CARTO_VOYAGER = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
MAX_MAP_POINTS = MAX_RENDER_POINTS
LIVE_RETRY_SECONDS = 120
WORLD_BBOX = (-90.0, 90.0, -180.0, 180.0)
MODIS_COLOR = "#0072B2"
VIIRS_COLOR = "#E69F00"
ADJUSTED_COLOR = "#009E73"
SENSOR_RGB = {"MODIS": (0, 114, 178), "VIIRS": (230, 159, 0)}
VIRIDIS_STOPS = (
    (0.00, (68, 1, 84)),
    (0.25, (59, 82, 139)),
    (0.50, (33, 145, 140)),
    (0.75, (94, 201, 98)),
    (1.00, (253, 231, 37)),
)
SENSOR_COLOR_MAP = {
    "MODIS": MODIS_COLOR,
    "VIIRS": VIIRS_COLOR,
    "VIIRS raw": VIIRS_COLOR,
    "VIIRS ÷ k": ADJUSTED_COLOR,
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


def viridis_rgb(tone: float) -> tuple[int, int, int]:
    tone = min(1.0, max(0.0, float(tone)))
    for (left, start), (right, end) in zip(VIRIDIS_STOPS, VIRIDIS_STOPS[1:]):
        if tone <= right or right == 1:
            span = right - left or 1
            mix = (tone - left) / span
            return tuple(int(start[i] + (end[i] - start[i]) * mix) for i in range(3))
    return VIRIDIS_STOPS[-1][1]


def attach_sensor_colors(frame: pd.DataFrame) -> pd.DataFrame:
    colors = frame["sensor"].map(lambda sensor: SENSOR_RGB.get(str(sensor).upper(), (100, 100, 100)))
    rgb = pd.DataFrame(colors.tolist(), columns=["fill_r", "fill_g", "fill_b"], index=frame.index)
    return pd.concat([frame, rgb], axis=1)


def attach_count_colors(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    out = frame.copy()
    values = pd.to_numeric(out[column], errors="coerce").fillna(0).clip(lower=0)
    logged = values.map(lambda value: math.log10(1.0 + float(value)))
    hi = float(logged.quantile(0.95)) if len(logged) else 1.0
    if not pd.notna(hi) or hi <= 0:
        hi = 1.0
    tone = (logged / hi).clip(0, 1)
    rgb = [viridis_rgb(value) for value in tone]
    out["fill_r"] = [item[0] for item in rgb]
    out["fill_g"] = [item[1] for item in rgb]
    out["fill_b"] = [item[2] for item in rgb]
    return out


def style_chart(fig):
    fig.update_layout(
        plot_bgcolor="#111827",
        paper_bgcolor="#0b1120",
        font_color="#f8fafc",
        margin=dict(l=16, r=16, t=48, b=16),
    )
    return fig


def world_view() -> pdk.ViewState:
    return pdk.ViewState(latitude=15, longitude=0, zoom=1.05, min_zoom=0.4, max_zoom=12, pitch=0, bearing=0)


def view_for_bbox(bbox) -> pdk.ViewState:
    if bbox_is_world(bbox):
        return world_view()
    south, north, west, east = bbox
    span = max(north - south, east - west, 0.5)
    zoom = max(0.5, min(10.0, math.log2(360 / span)))
    return pdk.ViewState(
        latitude=(south + north) / 2,
        longitude=(west + east) / 2,
        zoom=zoom,
        min_zoom=0.4,
        max_zoom=12,
        pitch=0,
        bearing=0,
    )


def _scatter(records, radius_min, radius_max, pickable=True):
    return pdk.Layer(
        "ScatterplotLayer",
        data=records,
        get_position="[lon, lat]",
        get_radius="radius",
        get_fill_color="[fill_r, fill_g, fill_b, 210]",
        get_line_color=[30, 30, 30, 180],
        pickable=pickable,
        opacity=0.9,
        stroked=True,
        filled=True,
        line_width_min_pixels=1,
        radius_min_pixels=radius_min,
        radius_max_pixels=radius_max,
        auto_highlight=True,
    )


def _grid_map_records(harmonized: pd.DataFrame, start, end, k: float) -> list[dict]:
    """Scatter rows for the harmonized grid. Every tooltip placeholder is a column."""

    tips = grid_tooltip_frame(harmonized, start, end, k)
    if tips.empty:
        return []
    colored = tips.copy()
    colored["harmonized"] = colored["harmonized_count"]
    colored = attach_count_colors(colored, "harmonized")
    colored["radius"] = 12000
    keep = ["radius", "fill_r", "fill_g", "fill_b", *GRID_TOOLTIP_FIELDS]
    return as_records(colored[keep])


def show_map(records, tooltip, radius_min, radius_max, map_key, bbox=None, view_state=None, under_records=None):
    layers = []
    if bbox is not None and not bbox_is_world(bbox):
        south, north, west, east = bbox
        layers.append(
            pdk.Layer(
                "PolygonLayer",
                data=[{"polygon": [[west, south], [east, south], [east, north], [west, north]]}],
                get_polygon="polygon",
                get_fill_color=[0, 114, 178, 28],
                get_line_color=[0, 114, 178, 230],
                line_width_min_pixels=2,
                stroked=True,
                filled=True,
                pickable=False,
            )
        )
    # The under-layer is context. Leave it unpickable so hover hits the fires on top.
    if under_records:
        layers.append(_scatter(under_records, 3, 22, pickable=False))
    layers.append(_scatter(records, radius_min, radius_max, pickable=True))
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state or view_for_bbox(bbox),
        tooltip=tooltip,
        map_provider="carto",
        map_style=CARTO_VOYAGER,
    )
    return st.pydeck_chart(
        deck,
        width="stretch",
        height=560,
        on_select="rerun",
        selection_mode="single-object",
        key=map_key,
    )


def legend_html(kind: str) -> str:
    swatch = (
        "display:inline-block;width:12px;height:12px;border-radius:99px;"
        "margin-right:6px;vertical-align:-1px;border:1px solid #0f172a;"
    )
    if kind == "sensor":
        return f"""
        <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:14px;color:#e2e8f0;">
          <span><span style="{swatch}background:{MODIS_COLOR};"></span>MODIS</span>
          <span><span style="{swatch}background:{VIIRS_COLOR};"></span>VIIRS</span>
          <span>Okabe-Ito colors. Confidence, FRP, and time are in the tooltip.</span>
        </div>
        """
    return """
    <div style="display:flex;align-items:center;gap:10px;font-size:14px;color:#e2e8f0;">
      <span>Fewer</span>
      <span style="display:inline-block;width:180px;height:12px;border-radius:99px;border:1px solid #0f172a;
        background:linear-gradient(90deg,#440154,#3b528b,#21918c,#5ec962,#fde725);"></span>
      <span>More MODIS-equivalent hotspots (viridis)</span>
    </div>
    """


def clock_label(value) -> str:
    text = str(value).split(".")[0].replace("<NA>", "").replace("nan", "")
    if not text:
        return ""
    text = text.zfill(4)
    if len(text) >= 4 and text[:4].isdigit():
        return f"{text[:2]}:{text[2:4]}"
    return ""


def current_bbox():
    return (
        float(st.session_state.get("bbox_s", -90.0)),
        float(st.session_state.get("bbox_n", 90.0)),
        float(st.session_state.get("bbox_w", -180.0)),
        float(st.session_state.get("bbox_e", 180.0)),
    )


def region_changed(new, old) -> bool:
    return any(abs(float(a) - float(b)) > 0.05 for a, b in zip(new, old))


def clear_region():
    """Runs before widgets, so it may reset the number inputs."""

    for key, value in zip(("bbox_s", "bbox_n", "bbox_w", "bbox_e"), WORLD_BBOX):
        st.session_state[key] = value
    st.session_state["draw_nonce"] = int(st.session_state.get("draw_nonce", 0)) + 1


def region_controls():
    """Numeric box, used when a drag-box is awkward. World means no filter."""

    pending = st.session_state.pop("pending_bbox", None)
    if pending:
        st.session_state.bbox_s, st.session_state.bbox_n, st.session_state.bbox_w, st.session_state.bbox_e = pending
    for key, value in zip(("bbox_s", "bbox_n", "bbox_w", "bbox_e"), WORLD_BBOX):
        st.session_state.setdefault(key, value)
    st.sidebar.subheader("Region")
    st.sidebar.caption("Type a box, or drag a rectangle on the street map.")
    st.sidebar.number_input("South latitude", min_value=-90.0, max_value=90.0, step=0.5, key="bbox_s")
    st.sidebar.number_input("North latitude", min_value=-90.0, max_value=90.0, step=0.5, key="bbox_n")
    st.sidebar.number_input("West longitude", min_value=-180.0, max_value=180.0, step=0.5, key="bbox_w")
    st.sidebar.number_input("East longitude", min_value=-180.0, max_value=180.0, step=0.5, key="bbox_e")
    st.sidebar.button("Clear region", on_click=clear_region)
    south, north, west, east = current_bbox()
    if south > north:
        south, north = north, south
    if west > east:
        west, east = east, west
    return (south, north, west, east)


def draw_region_map(bbox):
    """Folium rectangle draw. Pydeck in Streamlit cannot return a dragged box."""

    import folium
    from folium.plugins import Draw
    from streamlit_folium import st_folium

    south, north, west, east = bbox
    center = [15, 0] if bbox_is_world(bbox) else [(south + north) / 2, (west + east) / 2]
    zoom = 2 if bbox_is_world(bbox) else 4
    fmap = folium.Map(location=center, zoom_start=zoom, tiles=None, scrollWheelZoom=True)
    # CARTO's raster endpoint now returns "API key required" tiles.
    # OpenStreetMap is the free street basemap for the draw box.
    # The fire map below stays on the CARTO Voyager GL style.
    folium.TileLayer(
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="&copy; OpenStreetMap contributors",
        name="Streets",
        max_zoom=19,
    ).add_to(fmap)
    if not bbox_is_world(bbox):
        folium.Rectangle(
            bounds=[[south, west], [north, east]],
            color="#0072B2",
            weight=2,
            fill=False,
        ).add_to(fmap)
    Draw(
        export=False,
        draw_options={
            "polyline": False,
            "polygon": False,
            "circle": False,
            "marker": False,
            "circlemarker": False,
            "rectangle": True,
        },
        edit_options={"edit": False, "remove": False},
    ).add_to(fmap)
    st.caption(
        "Drag a rectangle to filter. Scroll or use +/- to zoom. "
        "Clear region in the sidebar removes the box."
    )
    output = st_folium(
        fmap,
        height=360,
        use_container_width=True,
        returned_objects=["last_active_drawing"],
        key=f"region-draw-{st.session_state.get('draw_nonce', 0)}",
    )
    drawing = (output or {}).get("last_active_drawing") or {}
    geometry = drawing.get("geometry") or {}
    if geometry.get("type") != "Polygon":
        return
    ring = geometry.get("coordinates") or [[]]
    ring = ring[0] if ring else []
    if len(ring) < 4:
        return
    lons = [float(point[0]) for point in ring]
    lats = [float(point[1]) for point in ring]
    new = (min(lats), max(lats), min(lons), max(lons))
    if region_changed(new, current_bbox()):
        st.session_state["pending_bbox"] = new
        st.rerun()


def filter_bbox(frame: pd.DataFrame, bbox, lat_col: str, lon_col: str) -> pd.DataFrame:
    if frame.empty or bbox_is_world(bbox) or lat_col not in frame.columns:
        return frame
    south, north, west, east = bbox
    return frame[frame[lat_col].between(south, north) & frame[lon_col].between(west, east)]


def frp_slider(series: pd.Series, key: str):
    """Range slider whose ceiling stays at the unfiltered max for this key."""

    valid = pd.to_numeric(series, errors="coerce").dropna()
    if valid.empty or float(valid.min()) >= float(valid.max()):
        return None
    low = float(valid.min())
    high = float(valid.max())
    if key not in st.session_state:
        st.session_state[key] = (low, high)
    return st.sidebar.slider(
        "Fire radiative power (MW)",
        min_value=low,
        max_value=high,
        key=key,
        help="Bounds are the min and max for this date range and sensor, before the other filters.",
    )


def point_tooltip_fields(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["date_label"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out["time_label"] = out["hhmm"].map(clock_label) if "hhmm" in out.columns else ""
    out["frp_label"] = pd.to_numeric(out["frp"], errors="coerce").round(1)
    out["bright_label"] = (
        pd.to_numeric(out["brightness"], errors="coerce").round(1) if "brightness" in out.columns else ""
    )
    out["conf_label"] = out["conf_class"].fillna(out.get("confidence", "")).astype(str)
    out["sat_label"] = out["satellite"].fillna("").astype(str) if "satellite" in out.columns else ""
    out["inst_label"] = out["instrument"].fillna("").astype(str) if "instrument" in out.columns else ""
    out["when_label"] = out["daynight"].map(daynight_label) if "daynight" in out.columns else ""
    out["lat_label"] = pd.to_numeric(out["lat"], errors="coerce").round(4)
    out["lon_label"] = pd.to_numeric(out["lon"], errors="coerce").round(4)
    return out


TOOLTIP_STYLE = {"backgroundColor": "#172033", "color": "white", "fontSize": "13px"}

POINT_TOOLTIP_FIELDS = (
    "lat_label",
    "lon_label",
    "date_label",
    "time_label",
    "sat_label",
    "inst_label",
    "conf_label",
    "frp_label",
    "bright_label",
    "when_label",
)

POINT_TOOLTIP = {
    "html": (
        "<b>{lat_label}, {lon_label}</b><br/>"
        "{date_label} {time_label} UTC<br/>"
        "{sat_label} · {inst_label}<br/>"
        "Confidence: {conf_label}<br/>"
        "FRP: {frp_label} MW<br/>"
        "Brightness: {bright_label}<br/>"
        "{when_label}"
    ),
    "style": TOOLTIP_STYLE,
}

GRID_TOOLTIP = {"html": GRID_TOOLTIP_HTML, "style": TOOLTIP_STYLE}


def describe_click(state) -> None:
    selection = getattr(state, "selection", None) or {}
    objects = selection.get("objects") if isinstance(selection, dict) else None
    row = None
    if isinstance(objects, dict):
        for rows in objects.values():
            if rows:
                row = rows[0]
                break
    elif isinstance(objects, list) and objects:
        row = objects[0]
    if not isinstance(row, dict):
        return
    if "harmonized_count" in row or "modis_n" in row:
        st.caption(
            f"{row.get('lat', '')}, {row.get('lon', '')} · "
            f"cell {row.get('south', '')} to {row.get('north', '')}, "
            f"{row.get('west', '')} to {row.get('east', '')} · "
            f"{row.get('date_range', '')} · "
            f"MODIS n {row.get('modis_n', '')} · VIIRS n {row.get('viirs_n', '')} · "
            f"VIIRS/k {row.get('viirs_over_k', '')} · "
            f"harmonized {row.get('harmonized_count', '')} · "
            f"FRP sum {row.get('frp_sum', '')} MW · mean {row.get('frp_mean', '')} MW"
        )
        return
    bits = [
        f"{row.get('lat_label', row.get('lat', ''))}, {row.get('lon_label', row.get('lon', ''))}",
        f"{row.get('date_label', '')} {row.get('time_label', '')} UTC".strip(),
        f"{row.get('sat_label', row.get('satellite', ''))} · {row.get('inst_label', row.get('instrument', ''))}",
        f"Confidence: {row.get('conf_label', '')}",
        f"FRP: {row.get('frp_label', row.get('frp', ''))} MW",
        f"Brightness: {row.get('bright_label', '')}",
        str(row.get("when_label", "")),
    ]
    st.caption(" · ".join(bit for bit in bits if bit and bit != " · "))


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


@st.cache_data(ttl=600, show_spinner=False)
def cached_archive_grid(kind: str, location: str, start_iso: str, end_iso: str, sources: tuple, bbox: tuple):
    return load_daily_grid(
        kind,
        location,
        date.fromisoformat(start_iso),
        date.fromisoformat(end_iso),
        list(sources),
        bbox,
    )


@st.cache_data(ttl=600, show_spinner=False)
def cached_archive_points(kind: str, location: str, start_iso: str, end_iso: str, sources: tuple, bbox: tuple):
    return load_points(
        kind,
        location,
        date.fromisoformat(start_iso),
        date.fromisoformat(end_iso),
        list(sources),
        bbox,
    )


@st.cache_data(ttl=600, show_spinner=False)
def cached_archive_totals(kind: str, location: str, start_iso: str, end_iso: str, sources: tuple, bbox: tuple):
    return load_daily_totals(
        kind,
        location,
        date.fromisoformat(start_iso),
        date.fromisoformat(end_iso),
        list(sources),
        bbox,
    )


@st.cache_data(ttl=600, show_spinner=False)
def cached_manifest(kind: str, location: str):
    return load_manifest(kind, location)


def render_selection_charts(daily: pd.DataFrame, cells: pd.DataFrame, points, k: float) -> None:
    """Charts for the current dates, sensors, and rectangle."""

    st.subheader("This selection")
    if daily is not None and not daily.empty:
        work = daily.copy()
        work["date"] = pd.to_datetime(work["date"])
        modis = work.loc[work["sensor"].eq("MODIS")].groupby("date")["n_hotspots"].sum()
        viirs = work.loc[work["sensor"].eq("VIIRS")].groupby("date")["n_hotspots"].sum()
        daily_chart = pd.DataFrame({"MODIS": modis, "VIIRS": viirs}).fillna(0)
        daily_chart["VIIRS ÷ k"] = daily_chart["VIIRS"] / k if k else 0
        daily_chart["Harmonized"] = daily_chart["MODIS"] + daily_chart["VIIRS ÷ k"]
        melted = daily_chart.reset_index().melt(id_vars="date", var_name="Series", value_name="Hotspots")
        fig_daily = px.line(
            melted,
            x="date",
            y="Hotspots",
            color="Series",
            markers=True,
            title="Hotspots per day",
            template="plotly_dark",
            color_discrete_map={
                "MODIS": MODIS_COLOR,
                "VIIRS": VIIRS_COLOR,
                "VIIRS ÷ k": ADJUSTED_COLOR,
                "Harmonized": "#F0E442",
            },
        )
        st.plotly_chart(style_chart(fig_daily), width="stretch", theme=None)
        monthly = daily_chart.copy()
        monthly.index = pd.to_datetime(monthly.index)
        monthly["month"] = monthly.index.to_period("M").to_timestamp()
        month_totals = monthly.groupby("month")[["MODIS", "VIIRS", "Harmonized"]].sum().reset_index()
        fig_month = px.line(
            month_totals,
            x="month",
            y=["MODIS", "VIIRS", "Harmonized"],
            markers=True,
            title="Hotspots per month",
            template="plotly_dark",
            color_discrete_map={"MODIS": MODIS_COLOR, "VIIRS": VIIRS_COLOR, "Harmonized": "#F0E442"},
        )
        st.plotly_chart(style_chart(fig_month), width="stretch", theme=None)
        st.caption(
            f"Average {daily_chart['Harmonized'].mean():,.1f} harmonized hotspots per day "
            f"in this selection. Total {daily_chart['Harmonized'].sum():,.0f}."
        )
        calendar = burn_calendar(work, k)
        if not calendar.empty and calendar.to_numpy().sum() > 0:
            fig_cal = px.imshow(
                calendar.fillna(0).to_numpy(),
                x=["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"],
                y=[str(year) for year in calendar.index],
                color_continuous_scale="Viridis",
                aspect="auto",
                labels=dict(color="Harmonized hotspots", x="Month", y="Year"),
                title="Burn calendar",
            )
            st.plotly_chart(style_chart(fig_cal), width="stretch", theme=None)
            st.caption("Each cell is that month's MODIS count plus VIIRS count divided by k, inside the rectangle.")

    left, right = st.columns(2)
    with left:
        if daily is not None and not daily.empty:
            share = daily.groupby("sensor")["n_hotspots"].sum().reset_index()
            fig_pie = px.pie(
                share,
                names="sensor",
                values="n_hotspots",
                hole=0.45,
                title="MODIS and VIIRS share",
                template="plotly_dark",
                color="sensor",
                color_discrete_map=SENSOR_COLOR_MAP,
            )
            st.plotly_chart(style_chart(fig_pie), width="stretch", theme=None)
    with right:
        if daily is not None and not daily.empty:
            modis_n = float(daily.loc[daily["sensor"].eq("MODIS"), "n_hotspots"].sum())
            viirs_n = float(daily.loc[daily["sensor"].eq("VIIRS"), "n_hotspots"].sum())
            comparison = pd.DataFrame(
                {
                    "Series": ["MODIS", "VIIRS raw", "VIIRS ÷ k"],
                    "Hotspots": [modis_n, viirs_n, viirs_n / k if k else 0],
                }
            )
            fig_bar = px.bar(
                comparison,
                x="Series",
                y="Hotspots",
                title="Raw counts and VIIRS on the MODIS scale",
                template="plotly_dark",
                color="Series",
                color_discrete_map=SENSOR_COLOR_MAP,
            )
            st.plotly_chart(style_chart(fig_bar), width="stretch", theme=None)

    if cells is not None and not cells.empty and {"cell_lat", "cell_lon", "harmonized"}.issubset(cells.columns):
        ranked = cells.groupby(["cell_lat", "cell_lon"], as_index=False)["harmonized"].sum()
        top = ranked.nlargest(min(10, len(ranked)), "harmonized")
        top["Cell"] = top.apply(lambda row: f"{row['cell_lat']:.2f}, {row['cell_lon']:.2f}", axis=1)
        fig_top = px.bar(
            top,
            x="harmonized",
            y="Cell",
            orientation="h",
            title="Top cells",
            template="plotly_dark",
            color_discrete_sequence=["#009E73"],
        )
        st.plotly_chart(style_chart(fig_top), width="stretch", theme=None)

    if points is None or points.empty:
        return
    st.caption("The next three charts use individual detections in this rectangle and date range.")
    c1, c2 = st.columns(2)
    with c1:
        fig_frp = px.histogram(
            points,
            x="frp",
            color="sensor",
            nbins=40,
            title="FRP distribution",
            template="plotly_dark",
            color_discrete_map=SENSOR_COLOR_MAP,
        )
        st.plotly_chart(style_chart(fig_frp), width="stretch", theme=None)
    with c2:
        conf = points["conf_class"].astype(str).value_counts().reset_index()
        conf.columns = ["Confidence", "Detections"]
        fig_conf = px.bar(
            conf,
            x="Confidence",
            y="Detections",
            title="Confidence",
            template="plotly_dark",
            color="Confidence",
            color_discrete_sequence=["#0072B2", "#E69F00", "#009E73", "#F0E442"],
        )
        st.plotly_chart(style_chart(fig_conf), width="stretch", theme=None)
    if "daynight" in points.columns:
        period = points["daynight"].map(daynight_label).value_counts().reset_index()
        period.columns = ["Period", "Detections"]
        fig_period = px.bar(
            period,
            x="Period",
            y="Detections",
            color="Period",
            title="Day and night",
            template="plotly_dark",
            color_discrete_map={"Day": MODIS_COLOR, "Night": VIIRS_COLOR},
        )
        st.plotly_chart(style_chart(fig_period), width="stretch", theme=None)


def render_archive() -> None:
    """10-year GCS (or local DATA_PATH) archive. Does not load the bundled CSV."""

    kind, location = archive_location()
    manifest = cached_manifest(kind, location) or {}
    archive_min, default_start, default_end = default_archive_dates(date.today(), manifest.get("latest"))
    where = f"gs://{location}" if kind == "gcs" else location
    st.sidebar.caption(f"Archive: {where}")
    if manifest.get("agg_end") or manifest.get("points_end"):
        agg_bit = (
            f"grid {manifest['agg_start']}–{manifest['agg_end']}"
            if manifest.get("agg_end")
            else "grid coverage unknown"
        )
        points_bit = (
            f"points {manifest['points_start']}–{manifest['points_end']}"
            if manifest.get("points_end")
            else "points coverage unknown"
        )
        st.sidebar.caption(f"Coverage: {agg_bit}; {points_bit}.")
    picked = st.sidebar.date_input(
        "Acquisition date range",
        value=(default_start, default_end),
        min_value=archive_min,
        max_value=date.today(),
        key="archive-dates",
        required=True,
    )
    start_date, end_date = parse_date_range(picked, default_start, default_end)
    if start_date < archive_min:
        start_date = archive_min
    if end_date > date.today():
        end_date = date.today()
    sensors = st.sidebar.multiselect(
        "Sensor",
        ["MODIS", "VIIRS"],
        default=["MODIS", "VIIRS"],
        key="archive-sensor",
    )
    bbox = region_controls()
    source_names = tuple(name.lower() for name in sensors) or ("modis", "viirs")
    # Recomputed every run from the dates and rectangle on screen. Not stored.
    load_points_now = should_load_points(start_date, end_date, bbox)
    bbox_key = tuple(round(float(value), 4) for value in bbox)
    region_selected = not bbox_is_world(bbox)
    prefer_points = region_selected and load_points_now
    region_token = (start_date.isoformat(), end_date.isoformat(), bbox_key, source_names)
    if prefer_points and st.session_state.get("points_view_for") != region_token:
        # A new rectangle that can load fires opens on those points.
        st.session_state["map_view"] = "Raw detections"
        st.session_state["points_view_for"] = region_token
    elif "map_view" not in st.session_state:
        st.session_state["map_view"] = "Harmonized grid"
    view = st.radio(
        "Map view",
        ["Raw detections", "Harmonized grid"],
        horizontal=True,
        key="map_view",
        width="stretch",
    )
    try:
        with st.spinner("Loading the map…"):
            grid, by_day = cached_archive_grid(
                kind,
                location,
                start_date.isoformat(),
                end_date.isoformat(),
                source_names,
                bbox_key,
            )
            points, truncated = (None, False)
            if view == "Raw detections" and load_points_now:
                points, truncated = cached_archive_points(
                    kind,
                    location,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    source_names,
                    bbox_key,
                )
    except Exception as exc:
        st.error("The archive could not be read, so the map was not drawn.")
        st.caption(public_error(exc))
        st.caption("Unset DATA_BUCKET to use the bundled sample, or check that the service account can read the bucket.")
        return

    frp_series = None
    frp_key = f"frp-archive-{start_date}-{end_date}-{'-'.join(source_names)}"
    if points is not None and "frp" in points.columns and not points.empty:
        frp_series = points["frp"]
    elif not grid.empty and "frp_sum" in grid.columns:
        frp_series = grid["frp_sum"]
    frp_range = frp_slider(frp_series, frp_key) if frp_series is not None else None
    if frp_range is not None and points is not None:
        points = points[points["frp"].between(frp_range[0], frp_range[1])]
    if points is not None and not points.empty:
        chosen_classes = st.sidebar.multiselect(
            "Confidence class",
            ["low", "nominal", "high"],
            default=["low", "nominal", "high"],
            key="archive-conf",
        )
        points = points[points["conf_class"].astype(str).isin(chosen_classes)]
        chosen_periods = st.sidebar.multiselect(
            "Day / Night",
            ["D", "N"],
            default=["D", "N"],
            format_func=daynight_label,
            key="archive-dn",
        )
        points = points[points["daynight"].astype(str).isin(chosen_periods)]

    built_from_points = False
    if points is not None:
        built = cells_from_points(points)
        agg_end = manifest.get("agg_end")
        grid_missing = grid is None or grid.empty
        agg_stops_early = agg_end is not None and end_date > agg_end
        if grid_missing and built is not None and not built.empty:
            grid = built
            daily = daily_from_cells(built)
            built_from_points = True
        elif agg_stops_early and built is not None and not built.empty:
            extra = built[pd.to_datetime(built["date"]).dt.date > agg_end]
            if not extra.empty:
                grid = pd.concat([grid, extra], ignore_index=True) if not grid_missing else extra
                extra_daily = daily_from_cells(extra)
                daily = extra_daily if daily is None or daily.empty else pd.concat([daily, extra_daily], ignore_index=True)
                built_from_points = True
    harmonized = harmonize_archive_grid(grid, k_value)
    if frp_range is not None and points is None and harmonized is not None and not harmonized.empty:
        harmonized = harmonized[harmonized["frp_sum"].between(frp_range[0], frp_range[1])]

    st.title("EmberScope")
    st.markdown(
        '<p class="subtitle">Ten years of MODIS and VIIRS, harmonized onto the MODIS scale.</p>',
        unsafe_allow_html=True,
    )
    st.caption(f"Archive {start_date.isoformat()} to {end_date.isoformat()}. k = {k_value:.3f}.")
    if manifest.get("latest") and default_end < date.today() and end_date >= default_end:
        st.caption(f"Loaded data runs through {manifest['latest']}. Later days are not in the archive yet.")
    # This condition is the current rectangle, not a message saved from an earlier draw.
    if not load_points_now:
        st.info(
            "This date range is long and no rectangle is selected, so individual detections stay in storage. "
            "Charts use the 0.25° daily grid. Drag a rectangle to load detections for that region."
        )
    elif truncated:
        st.info("This query was capped so the app stays up. Draw a smaller region to see a tighter set of detections.")
    if built_from_points:
        covered = manifest.get("agg_end")
        if covered:
            st.info(
                f"The daily grid currently ends {covered}. "
                "Harmonized cells for the later dates were counted from detections "
                "(nominal and high confidence, type 0 or missing)."
            )
        else:
            st.info(
                "The daily grid has no rows for these dates. "
                "Harmonized cells were counted from detections "
                "(nominal and high confidence, type 0 or missing)."
            )

    if view == "Raw detections" and points is not None:
        st.markdown(legend_html("sensor"), unsafe_allow_html=True)
    else:
        st.markdown(
            "Harmonized count in each cell is MODIS n + VIIRS n / k. "
            "These archive counts are nominal and high confidence, before the 1 km dedupe."
        )
        st.markdown(legend_html("grid"), unsafe_allow_html=True)
    draw_region_map(bbox)

    if view == "Raw detections" and points is not None and not points.empty:
        shown = points
        sample_note = ""
        if len(shown) > MAX_MAP_POINTS:
            shown = shown.sample(MAX_MAP_POINTS, random_state=42)
            sample_note = f" Showing a sample of {MAX_MAP_POINTS:,} of {len(points):,}."
        shown = attach_sensor_colors(shown)
        shown["radius"] = 4000 + shown["frp"].fillna(0).clip(lower=0, upper=200) * 150
        shown = point_tooltip_fields(shown)
        records = as_records(
            shown[
                [
                    "lon",
                    "lat",
                    "radius",
                    "fill_r",
                    "fill_g",
                    "fill_b",
                    *POINT_TOOLTIP_FIELDS,
                ]
            ]
        )
        under = None
        if not harmonized.empty:
            under = _grid_map_records(harmonized, start_date, end_date, k_value)
        state = show_map(
            records,
            POINT_TOOLTIP,
            2,
            14,
            "archive-points",
            bbox=bbox,
            under_records=under,
        )
        describe_click(state)
        st.caption(f"{len(points):,} detections in the query.{sample_note}")
    elif not harmonized.empty:
        records = _grid_map_records(harmonized, start_date, end_date, k_value)
        describe_click(
            show_map(
                records,
                GRID_TOOLTIP,
                3,
                22,
                "archive-grid",
                bbox=bbox,
            )
        )
        st.caption(f"{len(harmonized):,} grid cells. Harmonized = MODIS n + VIIRS n / {k_value:.3f}.")
    else:
        st.warning("No archive rows match these dates, sensors, and region.")

    if not harmonized.empty:
        totals = (
            grid.groupby("sensor")["n_hotspots"].sum()
            if not grid.empty
            else pd.Series(dtype=float)
        )
        modis_n = float(totals.get("MODIS", 0) or 0)
        viirs_n = float(totals.get("VIIRS", 0) or 0)
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
            title="Archive counts in this filter",
            template="plotly_dark",
            color="Series",
            color_discrete_map=SENSOR_COLOR_MAP,
        )
        st.plotly_chart(style_chart(fig_bar), width="stretch", theme=None)
        st.caption("VIIRS ÷ k is the MODIS-equivalent count. The harmonized map adds that to the MODIS count.")
    with st.spinner("Loading charts for this selection…"):
        if by_day and grid is not None and not grid.empty:
            daily = daily_from_cells(grid)
        else:
            chart_start, chart_end = chart_date_window(start_date, end_date)
            daily = cached_archive_totals(
                kind,
                location,
                chart_start.isoformat(),
                chart_end.isoformat(),
                source_names,
                bbox_key,
            )
        if daily is not None and not daily.empty:
            daily = daily.copy()
            daily["date"] = pd.to_datetime(daily["date"])
            newest_years = sorted(daily["date"].dt.year.unique())[-3:]
            daily = daily[daily["date"].dt.year.isin(newest_years)]
    render_selection_charts(daily, harmonized, points if view == "Raw detections" else None, k_value)


def load_saved_copy() -> pd.DataFrame:
    if not BUNDLED_CSV.exists():
        raise FileNotFoundError("app_data.csv was not found next to app.py.")
    return load_bundled_detections(str(BUNDLED_CSV)).copy()


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

saved_label = "Archive" if archive_location() else "Bundled sample"
requested = st.sidebar.radio(
    "Data source",
    [saved_label, "Live (last 24h)", "Upload CSV"],
    help="Live loads the latest FIRMS day for VIIRS SNPP NRT and MODIS NRT, then caches it for 10 minutes.",
)

if requested == "Archive":
    render_archive()
    st.stop()

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
else:
    st.sidebar.caption(
        "This file is already a 0.25° grid, so satellite, confidence, day/night, and FRP filters are not available."
    )

start_ts = pd.Timestamp(start_date).normalize()
end_ts = pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1)
bbox = region_controls()
dated = frame[frame["date"].notna()].copy()
dated = dated[(dated["date"] >= start_ts) & (dated["date"] < end_ts)]
if selected_sensors is not None:
    dated = dated[dated["sensor"].astype(str).isin(selected_sensors)]
if mode == "detections" and "frp" in dated.columns:
    frp_key = f"frp-{token}-{start_date}-{end_date}-{','.join(selected_sensors or [])}"
    frp_range = frp_slider(dated["frp"], frp_key)
filtered = dated
if selected_satellites is not None:
    filtered = filtered[filtered["satellite"].astype(str).isin(selected_satellites)]
if selected_classes is not None:
    filtered = filtered[filtered["conf_class"].fillna("unknown").astype(str).isin(selected_classes)]
if selected_daynight is not None:
    filtered = filtered[filtered["daynight"].astype(str).isin(selected_daynight)]
if frp_range is not None and "frp" in filtered.columns:
    filtered = filtered[filtered["frp"].between(frp_range[0], frp_range[1])]
lat_name = "lat" if "lat" in filtered.columns else "cell_lat"
lon_name = "lon" if "lon" in filtered.columns else "cell_lon"
filtered = filter_bbox(filtered, bbox, lat_name, lon_name)

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
        "Each point is one satellite detection. Blue is MODIS and orange is VIIRS. "
        "Hover or click a point for time, confidence, FRP, and brightness. "
        "Scroll or use +/- to zoom."
    )
    st.markdown(legend_html("sensor"), unsafe_allow_html=True)
else:
    st.markdown(
        "Each circle is a 0.25° cell (about 28 km). Color is a viridis scale of the "
        f"MODIS-equivalent hotspot count (VIIRS divided by k = {k_value:.3f}). "
        "FRP in the tooltip is summed, not divided by k."
    )
    st.markdown(legend_html("grid"), unsafe_allow_html=True)

draw_region_map(bbox)

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
        map_data = attach_sensor_colors(map_data)
        frp_display = map_data["frp"].fillna(0).clip(lower=0, upper=200)
        map_data["radius"] = 4000 + frp_display * 150
        map_data = point_tooltip_fields(map_data)
        records = as_records(
            map_data[
                [
                    "lon",
                    "lat",
                    "radius",
                    "fill_r",
                    "fill_g",
                    "fill_b",
                    *POINT_TOOLTIP_FIELDS,
                ]
            ]
        )
        picked = show_map(
            records,
            POINT_TOOLTIP,
            radius_min=2,
            radius_max=14,
            map_key="map-raw",
            bbox=bbox,
        )
        describe_click(picked)
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
            bbox=bbox,
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
            bbox=bbox,
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
            color="Sensor",
            color_discrete_map=SENSOR_COLOR_MAP,
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
            color="Sensor",
            color_discrete_map=SENSOR_COLOR_MAP,
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
            color_discrete_map=SENSOR_COLOR_MAP,
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
        color_discrete_map={"Day": MODIS_COLOR, "Night": VIIRS_COLOR},
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
