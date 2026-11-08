
import streamlit as st
import pandas as pd
import pydeck as pdk
import plotly.express as px

# -------------------------
# 1. Page configuration
# -------------------------

st.set_page_config(
    page_title="NASA Hotspot Explorer",
    page_icon="🔥",
    layout="wide"
)

st.title("🔥 NASA Hotspot Explorer")
st.write("Explore global satellite-detected fire hotspots.")

# -------------------------
# 2. Load the dataset
# -------------------------

@st.cache_data
def load_data():
    df = pd.read_csv("app_data.csv")
    df["acq_date"] = pd.to_datetime(df["acq_date"])
    return df

df = load_data()

# -------------------------
# 3. Sidebar filters
# -------------------------

st.sidebar.header("Filters")

sensors = st.sidebar.multiselect(
    "Select sensor",
    options=sorted(df["sensor"].unique()),
    default=sorted(df["sensor"].unique())
)

sources = st.sidebar.multiselect(
    "Select data source",
    options=sorted(df["data_source"].unique()),
    default=sorted(df["data_source"].unique())
)

min_date = df["acq_date"].min().date()
max_date = df["acq_date"].max().date()

date_range = st.sidebar.date_input(
    "Select date range",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date
)

# -------------------------
# 4. Filter observations
# -------------------------

filtered = df[
    (df["sensor"].isin(sensors)) &
    (df["data_source"].isin(sources))
].copy()

if len(date_range) == 2:
    start_date, end_date = date_range

    filtered = filtered[
        (filtered["acq_date"].dt.date >= start_date) &
        (filtered["acq_date"].dt.date <= end_date)
    ]

# -------------------------
# 5. Dashboard statistics
# -------------------------

st.subheader("Overview")

col1, col2, col3 = st.columns(3)

col1.metric(
    "Total observations",
    f"{len(filtered):,}"
)

col2.metric(
    "Average FRP",
    f"{filtered['frp'].mean():.2f} MW"
    if not filtered.empty else "N/A"
)

col3.metric(
    "Sensors selected",
    len(sensors)
)

# -------------------------
# 6. Interactive hotspot map
# -------------------------




# -------------------------
# 7. FRP distribution
# -------------------------

    
# -------------------------
# 7. Global hotspot map
# -------------------------

st.subheader("Global Hotspot Map")

if not filtered.empty:

    # Prepare map data
    map_data = filtered.dropna(
        subset=["latitude", "longitude"]
    ).copy()

    # Create hotspot layer with small, equal-sized dots
    hotspot_layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_data,
        get_position="[longitude, latitude]",
        get_radius=1.5,
        radius_units="pixels",
        get_fill_color=[255, 100, 0, 180],
        pickable=True,
        auto_highlight=True,
    )

    # Global map view
    view_state = pdk.ViewState(
        latitude=0,
        longitude=0,
        zoom=1,
        pitch=0
    )

    # Create map
   deck = pdk.Deck(
    layers=[hotspot_layer],
    initial_view_state=view_state,
    map_provider="carto",
    map_style="road",
    tooltip={
        "text": "Latitude: {latitude}\nLongitude: {longitude}\nFRP: {frp}"
    }
)

st.pydeck_chart(deck, use_container_width=True)

    st.pydeck_chart(deck, use_container_width=True)

    st.write(f"Hotspots displayed: {len(map_data):,}")

else:
    st.warning("No observations match your selected filters.")

    

# -------------------------
# 8. Daily observations
# -------------------------


if not filtered.empty:
    daily = (
        filtered.groupby("acq_date")
        .size()
        .reset_index(name="observations")
    )

    fig = px.line(
        daily,
        x="acq_date",
        y="observations",
        markers=True,
        title="Daily hotspot observations"
    )

    st.plotly_chart(fig, use_container_width=True)

# -------------------------
# 9. Observation data table
# -------------------------

st.subheader("Observation Data")

st.dataframe(
    filtered.head(1000),
    use_container_width=True
)

st.caption(
    "Data source: NASA FIRMS. "
    "Map currently displays the development sample."
)

