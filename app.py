
import streamlit as st
import pandas as pd
import pydeck as pdk
import plotly.express as px
from pathlib import Path

# ==========================================
# 1. PAGE CONFIGURATION
# ==========================================

st.set_page_config(
    page_title="NASA FireWatch",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==========================================
# 2. CUSTOM STYLING
# ==========================================

st.markdown("""
<style>
    .stApp {
        background-color: #0b1120;
        color: #f1f5f9;
    }

    [data-testid="stSidebar"] {
        background-color: #111827;
    }

    [data-testid="stMetric"] {
        background-color: #172033;
        padding: 18px;
        border-radius: 10px;
        border: 1px solid #29364c;
    }

    h1, h2, h3 {
        color: #f8fafc;
    }

    .subtitle {
        color: #94a3b8;
        font-size: 15px;
    }
</style>
""", unsafe_allow_html=True)


# ==========================================
# 3. LOAD DATA
# ==========================================

@st.cache_data
def load_data(file_path):
    df = pd.read_csv(file_path)

    df.columns = df.columns.str.lower().str.strip()

    df["acq_date"] = pd.to_datetime(
        df["acq_date"], errors="coerce"
    )

    numeric_columns = [
        "latitude", "longitude", "frp",
        "brightness", "confidence"
    ]

    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col], errors="coerce"
            )

    df = df.dropna(
        subset=["latitude", "longitude", "acq_date"]
    )

    return df


st.sidebar.title("🔥 NASA FireWatch")
st.sidebar.caption("Global Satellite Fire Monitoring")

uploaded_file = st.sidebar.file_uploader(
    "Upload hotspot CSV (optional)",
    type=["csv"]
)

data_path = Path(__file__).parent / "app_data.csv"

try:
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        df.columns = df.columns.str.lower().str.strip()

        df["acq_date"] = pd.to_datetime(
            df["acq_date"], errors="coerce"
        )

        for col in [
            "latitude", "longitude", "frp",
            "brightness", "confidence"
        ]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col], errors="coerce"
                )

        df = df.dropna(
            subset=["latitude", "longitude", "acq_date"]
        )
    else:
        if not data_path.exists():
            st.error(
                "app_data.csv was not found. "
                "Place it in the same folder as app.py."
            )
            st.stop()

        df = load_data(str(data_path))

except Exception as e:
    st.error(f"Error loading dataset: {e}")
    st.stop()


# ==========================================
# 4. DATA VALIDATION
# ==========================================

required_columns = [
    "latitude", "longitude", "acq_date",
    "frp", "satellite", "sensor"
]

missing_columns = [
    col for col in required_columns
    if col not in df.columns
]

if missing_columns:
    st.error(f"Missing columns: {missing_columns}")
    st.stop()

df = df[
    df["latitude"].between(-90, 90)
    & df["longitude"].between(-180, 180)
].copy()

if df.empty:
    st.error("No valid hotspot observations found.")
    st.stop()


# ==========================================
# 5. SIDEBAR FILTERS
# ==========================================

st.sidebar.header("Data Filters")

# Date filter
min_date = df["acq_date"].min().date()
max_date = df["acq_date"].max().date()

date_range = st.sidebar.date_input(
    "Acquisition Date",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date
)

if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = date_range
    end_date = date_range

# Satellite filter
satellites = sorted(
    df["satellite"].dropna().astype(str).unique()
)

selected_satellites = st.sidebar.multiselect(
    "Satellite",
    satellites,
    default=satellites
)

# Sensor filter
sensors = sorted(
    df["sensor"].dropna().astype(str).unique()
)

selected_sensors = st.sidebar.multiselect(
    "Sensor",
    sensors,
    default=sensors
)

# FRP filter
valid_frp = df["frp"].dropna()

if not valid_frp.empty:
    frp_min = float(valid_frp.min())
    frp_max = float(valid_frp.max())

    if frp_min < frp_max:
        frp_range = st.sidebar.slider(
            "Fire Radiative Power (MW)",
            min_value=frp_min,
            max_value=frp_max,
            value=(frp_min, frp_max)
        )
    else:
        frp_range = (frp_min, frp_max)
else:
    frp_range = None

# Day/night filter
if "daynight" in df.columns:
    daynight_options = sorted(
        df["daynight"].dropna().astype(str).unique()
    )

    selected_daynight = st.sidebar.multiselect(
        "Day / Night",
        daynight_options,
        default=daynight_options
    )
else:
    selected_daynight = None


# ==========================================
# 6. APPLY FILTERS
# ==========================================

filtered = df.copy()

filtered = filtered[
    (filtered["acq_date"].dt.date >= start_date)
    & (filtered["acq_date"].dt.date <= end_date)
]

filtered = filtered[
    filtered["satellite"].astype(str).isin(
        selected_satellites
    )
]

filtered = filtered[
    filtered["sensor"].astype(str).isin(
        selected_sensors
    )
]

if frp_range is not None:
    filtered = filtered[
        filtered["frp"].between(
            frp_range[0], frp_range[1]
        )
    ]

if selected_daynight is not None:
    filtered = filtered[
        filtered["daynight"].astype(str).isin(
            selected_daynight
        )
    ]


# ==========================================
# 7. APPLICATION HEADER
# ==========================================

st.title("🔥 NASA FireWatch")

st.markdown(
    '<p class="subtitle">'
    'Global wildfire hotspot monitoring using NASA MODIS and VIIRS satellite data'
    '</p>',
    unsafe_allow_html=True
)

st.divider()

if filtered.empty:
    st.warning(
        "No observations match your selected filters. "
        "Try adjusting the filters in the sidebar."
    )
    st.stop()


# ==========================================
# 8. KEY STATISTICS
# ==========================================

total_hotspots = len(filtered)

average_frp = filtered["frp"].mean()

maximum_frp = filtered["frp"].max()

unique_satellites = filtered["satellite"].nunique()

col1, col2, col3, col4 = st.columns(4)

col1.metric(
    "Total Hotspots",
    f"{total_hotspots:,}"
)

col2.metric(
    "Average FRP",
    f"{average_frp:.2f} MW"
    if pd.notna(average_frp) else "N/A"
)

col3.metric(
    "Maximum FRP",
    f"{maximum_frp:.2f} MW"
    if pd.notna(maximum_frp) else "N/A"
)

col4.metric(
    "Satellites",
    unique_satellites
)

st.divider()


# ==========================================
# 9. INTERACTIVE HOTSPOT MAP
# ==========================================

st.subheader("🌍 Global Hotspot Distribution")

st.caption(
    "Each point represents a satellite hotspot detection. "
    "Larger and brighter points represent higher FRP."
)

map_data = filtered.dropna(
    subset=["latitude", "longitude"]
).copy()

# Limit the number of rendered points for performance
MAX_MAP_POINTS = 50000

if len(map_data) > MAX_MAP_POINTS:
    map_data = map_data.sample(
        MAX_MAP_POINTS,
        random_state=42
    )

map_data["frp_display"] = (
    map_data["frp"].fillna(0).clip(lower=0)
)

# Radius scaling for visualization
map_data["radius"] = (
    30 + map_data["frp_display"].clip(upper=100) * 2
)

# Color intensity
map_data["intensity"] = (
    120 + map_data["frp_display"].clip(upper=100) * 1.3
).clip(upper=255)

layer = pdk.Layer(
    "ScatterplotLayer",
    data=map_data,
    get_position="[longitude, latitude]",
    get_radius="radius",
    get_fill_color="""
        [255, intensity, 20, 190]
    """,
    pickable=True,
    opacity=0.8,
    stroked=False,
    radius_min_pixels=2,
    radius_max_pixels=12
)

view_state = pdk.ViewState(
    latitude=20,
    longitude=0,
    zoom=1.2,
    pitch=0
)

tooltip = {
    "html": """
    <b>Satellite:</b> {satellite}<br/>
    <b>Sensor:</b> {sensor}<br/>
    <b>Date:</b> {acq_date}<br/>
    <b>FRP:</b> {frp} MW<br/>
    <b>Latitude:</b> {latitude}<br/>
    <b>Longitude:</b> {longitude}
    """,
    "style": {
        "backgroundColor": "#172033",
        "color": "white"
    }
}

deck = pdk.Deck(
    layers=[layer],
    initial_view_state=view_state,
    tooltip=tooltip,
    map_style=(
        "https://basemaps.cartocdn.com/"
        "gl/dark-matter-gl-style/style.json"
    )
)

st.pydeck_chart(
    deck,
    use_container_width=True,
    height=550
)

st.caption(
    f"Displaying {len(map_data):,} of "
    f"{len(filtered):,} filtered observations."
)


# ==========================================
# 10. DAILY HOTSPOT ACTIVITY
# ==========================================

st.divider()

st.subheader("📈 Daily Hotspot Activity")

daily_data = (
    filtered.groupby("acq_date")
    .size()
    .reset_index(name="Hotspots")
)

fig_daily = px.line(
    daily_data,
    x="acq_date",
    y="Hotspots",
    markers=True,
    labels={
        "acq_date": "Date",
        "Hotspots": "Number of Hotspots"
    },
    template="plotly_dark"
)

fig_daily.update_traces(
    line=dict(width=3)
)

fig_daily.update_layout(
    plot_bgcolor="#111827",
    paper_bgcolor="#0b1120",
    font_color="white"
)

st.plotly_chart(
    fig_daily,
    use_container_width=True
)


# ==========================================
# 11. MODIS VS VIIRS COMPARISON
# ==========================================

st.divider()

st.subheader("🛰️ MODIS vs VIIRS")

col1, col2 = st.columns(2)

with col1:

    sensor_counts = (
        filtered["sensor"]
        .value_counts()
        .reset_index()
    )

    sensor_counts.columns = [
        "Sensor", "Hotspots"
    ]

    fig_sensor = px.pie(
        sensor_counts,
        names="Sensor",
        values="Hotspots",
        hole=0.45,
        template="plotly_dark",
        title="Hotspot Detections by Sensor"
    )

    st.plotly_chart(
        fig_sensor,
        use_container_width=True
    )

with col2:

    frp_comparison = (
        filtered.groupby("sensor")["frp"]
        .mean()
        .reset_index()
    )

    fig_frp = px.bar(
        frp_comparison,
        x="sensor",
        y="frp",
        labels={
            "sensor": "Sensor",
            "frp": "Average FRP (MW)"
        },
        template="plotly_dark",
        title="Average FRP by Sensor"
    )

    st.plotly_chart(
        fig_frp,
        use_container_width=True
    )


# ==========================================
# 12. DAY VS NIGHT ANALYSIS
# ==========================================

if "daynight" in filtered.columns:

    st.divider()

    st.subheader("🌙 Day vs Night Activity")

    daynight_data = (
        filtered["daynight"]
        .value_counts()
        .reset_index()
    )

    daynight_data.columns = [
        "Period", "Hotspots"
    ]

    fig_daynight = px.bar(
        daynight_data,
        x="Period",
        y="Hotspots",
        color="Period",
        template="plotly_dark",
        title="Hotspot Detections by Time of Day"
    )

    st.plotly_chart(
        fig_daynight,
        use_container_width=True
    )


# ==========================================
# 13. RAW DATA EXPLORER
# ==========================================

st.divider()

st.subheader("📋 Hotspot Data Explorer")

st.caption(
    "Explore the observations matching your current filters."
)

st.dataframe(
    filtered.head(1000),
    use_container_width=True,
    hide_index=True
)

csv_data = filtered.to_csv(index=False).encode("utf-8")

st.download_button(
    label="Download Filtered Dataset",
    data=csv_data,
    file_name="filtered_hotspots.csv",
    mime="text/csv"
)


# ==========================================
# 14. FOOTER
# ==========================================

st.divider()

st.markdown("""
<div style="text-align:center; color:#64748b;">
    NASA FireWatch | MODIS & VIIRS Hotspot Monitoring<br/>
    Data-driven wildfire observation and visualization
</div>
""", unsafe_allow_html=True)
