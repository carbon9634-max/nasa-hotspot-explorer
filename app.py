
    
  
import streamlit as st
import pandas as pd
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
    "Filter observations by sensor, data source, and date."
)

# ------------------------------------------------------------
# Load data
# ------------------------------------------------------------
@st.cache_data
def load_data():
    data = pd.read_csv("app_data.csv")

    required = [
        "latitude", "longitude", "frp", "acq_date",
        "sensor", "data_source"
    ]
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError("Missing CSV columns: " + ", ".join(missing))

    data["latitude"] = pd.to_numeric(data["latitude"], errors="coerce")
    data["longitude"] = pd.to_numeric(data["longitude"], errors="coerce")
    data["frp"] = pd.to_numeric(data["frp"], errors="coerce")
    data["acq_date"] = pd.to_datetime(data["acq_date"], errors="coerce")

    data = data.dropna(subset=["latitude", "longitude", "acq_date"])
    data = data[
        data["latitude"].between(-90, 90)
        & data["longitude"].between(-180, 180)
    ].copy()

    return data


try:
    df = load_data()
except Exception as error:
    st.error(f"Unable to load app_data.csv: {error}")
    st.stop()

if df.empty:
    st.error("No valid hotspot observations were found in app_data.csv.")
    st.stop()

# ------------------------------------------------------------
# Sidebar filters
# ------------------------------------------------------------
st.sidebar.header("Filters")

sensors_available = sorted(df["sensor"].dropna().unique().tolist())
sources_available = sorted(df["data_source"].dropna().unique().tolist())

sensors_selected = st.sidebar.multiselect(
    "Select sensor",
    sensors_available,
    default=sensors_available
)

sources_selected = st.sidebar.multiselect(
    "Select data source",
    sources_available,
    default=sources_available
)

first_date = df["acq_date"].min().date()
last_date = df["acq_date"].max().date()

date_selection = st.sidebar.date_input(
    "Select date range",
    value=(first_date, last_date),
    min_value=first_date,
    max_value=last_date
)

# ------------------------------------------------------------
# Filter observations
# ------------------------------------------------------------
filtered = df[
    df["sensor"].isin(sensors_selected)
    & df["data_source"].isin(sources_selected)
].copy()

