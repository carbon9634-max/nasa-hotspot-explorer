"""Checks that harmonize.py still follows the notebook method."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import harmonize
from harmonize import (
    DEFAULT_K,
    FirmsFetchError,
    active_k,
    aggregate,
    estimate_k,
    fetch_firms_nrt,
    normalize,
    prepare_detections,
    read_data_uri,
    select_harmonized,
    standardize_grid,
)


def _detection_frame(**overrides):
    count = max((len(value) for value in overrides.values() if hasattr(value, "__len__")), default=4)
    rows = {
        "latitude": [10.0, 10.004, 10.02, 10.5][:count] if count <= 4 else [10.0] * count,
        "longitude": [20.0, 20.004, 20.0, 20.0][:count] if count <= 4 else [20.0] * count,
        "acq_date": ["2026-09-01"] * count,
        "acq_time": ([930, 931, 932, 1200] + [1200] * count)[:count],
        "satellite": ["N"] * count,
        "confidence": (["n", "h", "l", "n"] + ["n"] * count)[:count],
        "frp": ([10.0, 12.0, 5.0, 7.0] + [1.0] * count)[:count],
        "daynight": ["D"] * count,
        "type": ([0, 0, 0, 2] + [0] * count)[:count],
    }
    rows.update(overrides)
    return pd.DataFrame(rows)


class HarmonizeTests(unittest.TestCase):
    def test_letters_are_not_blanked_by_numeric_conversion(self):
        raw = pd.DataFrame(
            {
                "latitude": [0, 0, 0, 0],
                "longitude": [0, 0, 0, 0],
                "acq_date": ["2026-09-01"] * 4,
                "acq_time": [1200] * 4,
                "satellite": ["N"] * 4,
                "confidence": ["l", "n", "h", "50"],
                "frp": [1, 1, 1, 1],
                "daynight": ["D"] * 4,
            }
        )
        blanked = pd.to_numeric(raw["confidence"], errors="coerce")
        self.assertEqual(int(blanked.isna().sum()), 3)
        out = normalize(raw, "VIIRS")
        self.assertEqual(list(out["conf_class"]), ["low", "nominal", "high", "nominal"])

    def test_numeric_bins_match_the_notebook(self):
        raw = _detection_frame(
            latitude=[0] * 6,
            longitude=[0] * 6,
            confidence=["0", "29", "30", "79", "80", "100"],
            type=[0] * 6,
        )
        out = normalize(raw, "MODIS")
        self.assertEqual(
            list(out["conf_class"]),
            ["low", "low", "nominal", "nominal", "high", "high"],
        )

    def test_aggregate_dedupes_filters_and_bins(self):
        out = aggregate(normalize(_detection_frame(), "VIIRS"))
        self.assertEqual(len(out), 1)
        self.assertEqual(int(out.iloc[0]["n_hotspots"]), 1)
        self.assertAlmostEqual(float(out.iloc[0]["cell_lat"]), 10.125)
        self.assertAlmostEqual(float(out.iloc[0]["cell_lon"]), 20.125)
        self.assertEqual(out.iloc[0]["sensor"], "VIIRS")

    def test_missing_type_is_kept_as_vegetation(self):
        frame = _detection_frame(latitude=[1.0], longitude=[2.0], confidence=["h"], type=[0])
        frame = frame.drop(columns=["type"])
        out = aggregate(normalize(frame, "VIIRS"))
        self.assertEqual(len(out), 1)

    def test_splice_uses_viirs_only_after_2012_02_01(self):
        daily = pd.DataFrame(
            {
                "sensor": ["MODIS", "VIIRS", "MODIS", "VIIRS"],
                "date": pd.to_datetime(
                    ["2012-01-31", "2012-01-31", "2012-02-01", "2012-02-01"]
                ),
                "cell_lat": [0.125, 0.125, 0.125, 0.125],
                "cell_lon": [0.125, 0.125, 0.125, 0.125],
                "n_hotspots": [10, 50, 12, 40],
                "frp_sum": [1, 2, 3, 4],
            }
        )
        out, note = select_harmonized(daily, k=4)
        self.assertIn("VIIRS", note)
        self.assertEqual(len(out), 2)
        january = out[out["date"] == pd.Timestamp("2012-01-31")].iloc[0]
        february = out[out["date"] == pd.Timestamp("2012-02-01")].iloc[0]
        self.assertEqual(january["sensor"], "MODIS")
        self.assertEqual(float(january["harmonized"]), 10)
        self.assertEqual(february["sensor"], "VIIRS")
        self.assertEqual(float(february["harmonized"]), 10)
        self.assertEqual(february["scale_note"], "VIIRS / k")

    def test_modis_only_days_stay_visible_beside_viirs_days(self):
        daily = pd.DataFrame(
            {
                "sensor": ["VIIRS", "MODIS", "MODIS"],
                "date": pd.to_datetime(["2026-09-01", "2026-09-01", "2026-11-08"]),
                "cell_lat": [0.125, 0.125, 1.125],
                "cell_lon": [0.125, 0.125, 1.125],
                "n_hotspots": [10, 4, 2],
                "frp_sum": [1, 1, 1],
            }
        )
        out, _note = select_harmonized(daily, k=5)
        september = out[out["date"] == pd.Timestamp("2026-09-01")]
        november = out[out["date"] == pd.Timestamp("2026-11-08")]
        self.assertEqual(list(september["sensor"]), ["VIIRS"])
        self.assertEqual(float(september.iloc[0]["harmonized"]), 2)
        self.assertEqual(november.iloc[0]["scale_note"], "MODIS reference")
        self.assertEqual(float(november.iloc[0]["harmonized"]), 2)

    def test_modis_only_filter_falls_back_without_double_counting(self):
        daily = pd.DataFrame(
            {
                "sensor": ["MODIS"],
                "date": pd.to_datetime(["2026-09-01"]),
                "cell_lat": [1.125],
                "cell_lon": [2.125],
                "n_hotspots": [8],
                "frp_sum": [3.5],
            }
        )
        out, note = select_harmonized(daily, k=5)
        self.assertEqual(out.iloc[0]["scale_note"], "MODIS reference")
        self.assertEqual(float(out.iloc[0]["harmonized"]), 8)
        self.assertIn("unscaled MODIS", note)

    def test_estimate_k_is_the_notebook_ratio(self):
        daily = pd.DataFrame(
            {"sensor": ["VIIRS", "MODIS"], "n_hotspots": [20, 5]}
        )
        self.assertEqual(estimate_k(daily), 4)

    def test_active_k_honors_env_and_ignores_bad_values(self):
        with mock.patch.dict(os.environ, {"HARMONIZE_K": "2.5"}, clear=False):
            value, source = active_k()
        self.assertEqual(value, 2.5)
        self.assertIn("HARMONIZE_K", source)
        with mock.patch.dict(os.environ, {"HARMONIZE_K": "nope"}, clear=False):
            value, source = active_k()
        self.assertEqual(value, DEFAULT_K)
        self.assertIn("not a number", source)
        with mock.patch.dict(os.environ, {}, clear=True):
            value, _source = active_k()
        self.assertEqual(value, DEFAULT_K)

    def test_prepare_reads_instrument_when_sensor_is_absent(self):
        frame = _detection_frame(
            latitude=[0.2, 1.2],
            longitude=[0.2, 1.2],
            confidence=["n", "80"],
            type=[0, 0],
        )
        frame = frame.drop(columns=["satellite"])
        frame["instrument"] = ["VIIRS", "MODIS"]
        frame["satellite"] = ["N", "Terra"]
        out = prepare_detections(frame)
        self.assertEqual(set(out["sensor"]), {"VIIRS", "MODIS"})
        self.assertEqual(set(out["conf_class"]), {"nominal", "high"})

    def test_fetch_requires_a_key_and_hides_it(self):
        with self.assertRaises(FirmsFetchError) as ctx:
            fetch_firms_nrt("")
        self.assertIn("FIRMS_MAP_KEY", str(ctx.exception))

        def explode(url, timeout, headers):
            raise harmonize.requests.ConnectionError(url)

        with mock.patch("harmonize.requests.get", explode):
            with self.assertRaises(FirmsFetchError) as ctx:
                fetch_firms_nrt("super-secret-key")
        self.assertNotIn("super-secret-key", str(ctx.exception))
        self.assertNotIn("http", str(ctx.exception).lower())

    def test_fetch_parses_both_sources_and_strips_the_key_from_warnings(self):
        def fake_get(url, timeout, headers):
            class Response:
                def __init__(self, status, text):
                    self.status_code = status
                    self.text = text

            if "VIIRS_SNPP_NRT" in url:
                return Response(
                    200,
                    "latitude,longitude,acq_date,acq_time,satellite,confidence,frp,daynight\n"
                    "1.5,2.5,2026-11-15,1200,N,n,4.0,D\n",
                )
            return Response(400, "bad key super-secret-key")

        with mock.patch("harmonize.requests.get", fake_get):
            frame, warning = fetch_firms_nrt("super-secret-key")
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["sensor"], "VIIRS")
        self.assertNotIn("super-secret-key", warning)
        self.assertIn("MODIS_NRT", warning)

    def test_round_trip_grid_parquet(self):
        daily = aggregate(normalize(_detection_frame(), "VIIRS"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cells.parquet"
            daily.to_parquet(path, index=False)
            loaded = standardize_grid(read_data_uri(str(path)))
        self.assertEqual(int(loaded.iloc[0]["n_hotspots"]), 1)

    def test_bundled_sample_matches_the_documented_k(self):
        path = Path(__file__).parent / "app_data.csv"
        raw = pd.read_csv(path)
        prepared = prepare_detections(raw)
        self.assertEqual(int(prepared["conf_class"].isna().sum()), 0)
        daily = aggregate(prepared)
        totals = daily.groupby("sensor")["n_hotspots"].sum()
        self.assertEqual(int(totals["VIIRS"]), 73334)
        self.assertEqual(int(totals["MODIS"]), 14129)
        self.assertAlmostEqual(estimate_k(daily), DEFAULT_K, places=5)


if __name__ == "__main__":
    unittest.main()
