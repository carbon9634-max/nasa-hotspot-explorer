
    
  
import streamlit as st
import pandas as pd
import pydeck as pdk
import plotly.express as px

# ------------------------------------------------------------
# Page setup
# ------------------------------------------------------------
st.set_page_config(
    page_title="NASA Hotspot Explorer",
    page_icon="🔥",
    layout="wide"
)

st.title("🔥 NASA Hotspot Explorer")
st.write(
    "Explore satellite-detected fire hotspots from NASA FIRMS. "
    "Use the filters to explore observations by sensor, source, and date."
)

# ------------------------------------------------------------
# Load and prepare data
# ------------------------------------------------------------
@st.cache_data
def load_data():
    data = pd.read_csv("app_data.csv")

    required_columns = [
        "latitude", "longitude", "frp", "acq_date",
        "sensor", "data_source"
    ]
    missing_columns = [col for col in required_columns if col not in data.columns]

    if missing_columns:
        raise ValueError(
            "The CSV is missing required columns: "
            + ", ".join(missing_columns)
        )

    data["acq_date"] = pd.to_datetime(data["acq_date"], errors="coerce")
    data["latitude"] = pd.to_numeric(data["latitude"], errors="coerce")
    data["longitude"] = pd.to_numeric(data["longitude"], errors="coerce")
    data["frp"] = pd.to_numeric(data["frp"], errors="coerce")

    data = data.dropna(subset=["acq_date", "latitude", "longitude"])
    data = data[
        data["latitude"].between(-90, 90)
        & data["longitude"].between(-180, 180)
    ].copy()

    return data


try:
    df = load_data()
except Exception as error:
    st.error(f"Could not load app_data.csv: {error}")
    st.stop()

if df.empty:
    st.warning("The dataset does not contain any valid hotspot observations.")
    st.stop()

# ------------------------------------------------------------
# Sidebar filters
# ------------------------------------------------------------
st.sidebar.header("Filters")

available_sensors = sorted(df["sensor"].dropna().unique().tolist())
available_sources = sorted(df["data_source"].dropna().unique().tolist())

selected_sensors = st.sidebar.multiselect(
    "Select sensor",
    options=available_sensors,
    default=available_sensors
)

selected_sources = st.sidebar.multiselect(
    "Select data source",
    options=available_sources,
    default=available_sources
)

minimum_date = df["acq_date"].min().date()
maximum_date = df["acq_date"].max().date()

selected_dates = st.sidebar.date_input(
    "Select date range",
    value=(minimum_date, maximum_date),
    min_value=minimum_date,
    max_value=maximum_date
)

# ------------------------------------------------------------
# Apply filters
# ------------------------------------------------------------

