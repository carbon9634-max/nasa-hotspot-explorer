"""Loader tests against a tiny local archive. No GCS calls."""

import os
import unittest
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from archive import (
    archive_location,
    burn_calendar,
    cells_from_points,
    chart_date_window,
    GRID_TOOLTIP_FIELDS,
    GRID_TOOLTIP_HTML,
    grid_tooltip_frame,
    harmonize_archive_grid,
    load_daily_grid,
    load_daily_totals,
    load_points,
    normalize_manifest,
    row_groups_for_range,
    should_load_points,
)


def _write_archive(root: Path) -> None:
    points = pa.table(
        {
            "latitude": pa.array([10.1, 10.2, 40.0], pa.float32()),
            "longitude": pa.array([20.1, 20.4, 50.0], pa.float32()),
            "acq_date": pa.array([date(2016, 11, 20), date(2016, 11, 20), date(2016, 11, 21)]),
            "acq_time": pa.array([930, 1545, 30], pa.int16()),
            "satellite": ["Terra", "N", "Aqua"],
            "instrument": ["MODIS", "VIIRS", "MODIS"],
            "confidence_raw": ["85", "n", "20"],
            "confidence": ["high", "nominal", "low"],
            "frp": pa.array([15.5, 4.0, 1.5], pa.float32()),
            "brightness": pa.array([330.0, 310.0, 300.0], pa.float32()),
            "bright_t31": pa.array([290.0, 280.0, 285.0], pa.float32()),
            "daynight": ["D", "N", "D"],
            "type": pa.array([0, None, 0], pa.int8()),
            "scan": pa.array([1.0, 0.4, 1.1], pa.float32()),
            "track": pa.array([1.0, 0.4, 1.0], pa.float32()),
        }
    )
    split = {
        "modis": points.take([0, 2]),
        "viirs": points.take([1]),
    }
    for source, table in split.items():
        folder = root / "firms" / f"source={source}" / "year=2016" / "month=11"
        folder.mkdir(parents=True)
        pq.write_table(table, folder / "part-0.parquet")

    grid = pa.table(
        {
            "acq_date": pa.array([date(2016, 11, 20), date(2016, 11, 20), date(2017, 1, 2)]),
            "lat_bin": pa.array([10.0, 10.0, 40.0], pa.float32()),
            "lon_bin": pa.array([20.0, 20.0, 50.0], pa.float32()),
            "source": ["modis", "viirs", "modis"],
            "n": pa.array([10, 5, 7], pa.int32()),
            "frp_sum": pa.array([20.0, 8.0, 3.0], pa.float32()),
        }
    )
    agg = root / "agg"
    agg.mkdir()
    pq.write_table(grid, agg / "daily_grid.parquet")


class ArchiveTests(unittest.TestCase):
    def test_points_follow_date_source_and_bbox(self):
        with TemporaryDirectory() as tmp:
            _write_archive(Path(tmp))
            os.environ["DATA_PATH"] = tmp
            os.environ.pop("DATA_BUCKET", None)
            self.assertEqual(archive_location(), ("local", tmp))
            frame, truncated = load_points(
                "local",
                tmp,
                date(2016, 11, 20),
                date(2016, 11, 20),
                sources=["MODIS", "VIIRS"],
                bbox=(9, 12, 19, 22),
            )
        self.assertFalse(truncated)
        self.assertEqual(len(frame), 2)
        self.assertEqual(set(frame["sensor"]), {"MODIS", "VIIRS"})
        self.assertEqual(set(frame["conf_class"]), {"high", "nominal"})
        self.assertIn("0930", set(frame["hhmm"]))
        self.assertTrue((frame["brightness"] > 0).all())

    def test_grid_harmonizes_as_modis_plus_viirs_over_k(self):
        with TemporaryDirectory() as tmp:
            _write_archive(Path(tmp))
            grid, by_day = load_daily_grid(
                "local",
                tmp,
                date(2016, 11, 1),
                date(2016, 11, 30),
                sources=["modis", "viirs"],
                bbox=(9, 12, 19, 22),
            )
        self.assertTrue(by_day)
        self.assertEqual(len(grid), 2)
        harmonized = harmonize_archive_grid(grid, k=5)
        self.assertEqual(len(harmonized), 1)
        self.assertAlmostEqual(float(harmonized.iloc[0]["harmonized"]), 11.0)
        self.assertAlmostEqual(float(harmonized.iloc[0]["cell_lat"]), 10.125)
        self.assertAlmostEqual(float(harmonized.iloc[0]["frp_sum"]), 28.0)

    def test_long_range_sums_cells_and_point_policy(self):
        self.assertTrue(should_load_points(date(2020, 1, 1), date(2020, 1, 31), None))
        self.assertFalse(should_load_points(date(2016, 11, 15), date(2026, 11, 15), None))
        self.assertTrue(
            should_load_points(date(2016, 11, 15), date(2026, 11, 15), (10, 20, 20, 30))
        )
        # A country-sized rectangle, not only a 20° box, should load points.
        self.assertTrue(
            should_load_points(date(2016, 11, 15), date(2026, 11, 15), (-20, 5, -80, -45))
        )
        with TemporaryDirectory() as tmp:
            _write_archive(Path(tmp))
            grid, by_day = load_daily_grid(
                "local",
                tmp,
                date(2016, 11, 1),
                date(2017, 2, 1),
            )
        self.assertFalse(by_day)
        self.assertEqual(len(grid), 3)
        self.assertEqual(int(grid.loc[grid["sensor"].eq("MODIS"), "n_hotspots"].sum()), 17)


def _amazon_grid(dates, date_type):
    return pa.table(
        {
            "acq_date": pa.array(dates, date_type),
            "lat_bin": pa.array([-10.0, -10.0, 40.0], pa.float32()),
            "lon_bin": pa.array([-60.0, -60.0, 10.0], pa.float32()),
            "source": ["modis", "viirs", "modis"],
            "n": pa.array([8, 4, 99], pa.int32()),
            "frp_sum": pa.array([12.0, 6.0, 1.0], pa.float32()),
        }
    )


class AmazonGridTests(unittest.TestCase):
    def test_date32_and_timestamp_amazon_box(self):
        amazon = (-15.0, -5.0, -70.0, -50.0)
        for date_type in (pa.date32(), pa.timestamp("ns")):
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "agg").mkdir()
                values = [date(2024, 8, 15), date(2024, 8, 15), date(2024, 8, 16)]
                if pa.types.is_timestamp(date_type):
                    values = [datetime(2024, 8, 15), datetime(2024, 8, 15), datetime(2024, 8, 16)]
                pq.write_table(_amazon_grid(values, date_type), root / "agg" / "daily_grid.parquet")
                grid, by_day = load_daily_grid(
                    "local",
                    tmp,
                    date(2024, 8, 1),
                    date(2024, 8, 31),
                    sources=["modis", "viirs"],
                    bbox=amazon,
                )
                totals = load_daily_totals(
                    "local",
                    tmp,
                    date(2024, 8, 1),
                    date(2024, 8, 31),
                    bbox=amazon,
                )
            self.assertTrue(by_day, date_type)
            self.assertEqual(len(grid), 2, date_type)
            self.assertEqual(int(grid["n_hotspots"].sum()), 12, date_type)
            self.assertEqual(len(totals), 2, date_type)
            calendar = burn_calendar(totals, k=4)
            self.assertEqual(float(calendar.loc[2024, 8]), 8 + 4 / 4)

    def test_points_fill_dates_the_aggregate_does_not_cover(self):
        points = pd.DataFrame(
            {
                "lat": [ -9.9, -9.8, -9.7],
                "lon": [-59.9, -59.8, -59.7],
                "date": pd.to_datetime(["2026-11-10", "2026-11-10", "2026-11-11"]),
                "sensor": ["MODIS", "VIIRS", "VIIRS"],
                "conf_class": ["high", "nominal", "low"],
                "type": [0, None, 0],
                "frp": [10.0, 5.0, 1.0],
            }
        )
        cells = cells_from_points(points)
        self.assertEqual(len(cells), 2)
        harmonized = harmonize_archive_grid(cells, k=5)
        self.assertEqual(len(harmonized), 1)
        self.assertAlmostEqual(float(harmonized.iloc[0]["harmonized"]), 1.2)
        manifest = normalize_manifest(
            {"agg_start": "2016-11-15", "agg_end": "2025-12-31", "points_end": "2026-11-15"}
        )
        self.assertEqual(manifest["agg_end"], date(2025, 12, 31))
        self.assertEqual(manifest["latest"], date(2026, 11, 15))
        shaped = normalize_manifest(
            {
                "date_min": "2016-11-15",
                "date_max": "2026-11-14",
                "counts": {"viirs": {"2024": {"rows": 10, "date_min": "2024-01-01", "date_max": "2024-12-31"}}},
                "agg": {"path": "agg/daily_grid.parquet", "cells": 39_000_000, "bytes": 154_000_000},
            }
        )
        self.assertEqual(shaped["latest"], date(2026, 11, 14))
        self.assertEqual(shaped["agg_end"], date(2026, 11, 14))
        self.assertEqual(chart_date_window(date(2016, 11, 15), date(2026, 11, 15))[0], date(2024, 1, 1))

    def test_row_groups_outside_the_week_are_not_read(self):
        import pyarrow.parquet as pq

        early = pa.table(
            {
                "acq_date": pa.array([date(2016, 1, 1)], pa.date32()),
                "source": ["modis"],
                "lat_bin": pa.array([0.0], pa.float32()),
                "lon_bin": pa.array([0.0], pa.float32()),
                "n": pa.array([100], pa.int32()),
                "frp_sum": pa.array([1.0], pa.float32()),
            }
        )
        late = pa.table(
            {
                "acq_date": pa.array([date(2026, 11, 12)], pa.date32()),
                "source": ["viirs"],
                "lat_bin": pa.array([-10.0], pa.float32()),
                "lon_bin": pa.array([-60.0], pa.float32()),
                "n": pa.array([3], pa.int32()),
                "frp_sum": pa.array([2.0], pa.float32()),
            }
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agg").mkdir()
            path = root / "agg" / "daily_grid.parquet"
            writer = pq.ParquetWriter(path, early.schema)
            writer.write_table(early)
            writer.write_table(late)
            writer.close()
            chosen = row_groups_for_range(
                pq.ParquetFile(path), date(2026, 11, 10), date(2026, 11, 16), ["modis", "viirs"]
            )
            self.assertEqual(chosen, [1])
            grid, by_day = load_daily_grid(
                "local", tmp, date(2026, 11, 10), date(2026, 11, 16), bbox=(-20, 0, -70, -50)
            )
        self.assertTrue(by_day)
        self.assertEqual(len(grid), 1)
        self.assertEqual(int(grid.iloc[0]["n_hotspots"]), 3)
        self.assertEqual(grid.iloc[0]["sensor"], "VIIRS")

    def test_grid_tooltip_has_the_fields_the_map_reads(self):
        harmonized = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-11-12"]),
                "cell_lat": [-9.875],
                "cell_lon": [-59.875],
                "modis_n": [2],
                "viirs_n": [5],
                "harmonized": [3.0],
                "frp_sum": [10.0],
            }
        )
        tips = grid_tooltip_frame(harmonized, date(2026, 11, 10), date(2026, 11, 16), k=5)
        row = tips.iloc[0]
        for name in GRID_TOOLTIP_FIELDS:
            self.assertIn(name, tips.columns)
            self.assertIn("{" + name + "}", GRID_TOOLTIP_HTML)
        self.assertEqual(float(row["lat"]), -9.875)
        self.assertEqual(float(row["lon"]), -59.875)
        self.assertEqual(float(row["south"]), -10.0)
        self.assertEqual(float(row["north"]), -9.75)
        self.assertEqual(row["date_range"], "2026-11-10 to 2026-11-16")
        self.assertEqual(row["cell_date"], "2026-11-12")
        self.assertEqual(int(row["modis_n"]), 2)
        self.assertEqual(int(row["viirs_n"]), 5)
        self.assertAlmostEqual(float(row["viirs_over_k"]), 1.0)
        self.assertAlmostEqual(float(row["harmonized_count"]), 3.0)
        self.assertAlmostEqual(float(row["frp_mean"]), round(10 / 7, 2))

    def test_pydeck_tooltip_keeps_the_tip_column(self):
        import json

        import pydeck as pdk

        harmonized = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-11-12"]),
                "cell_lat": [-9.875],
                "cell_lon": [-59.875],
                "modis_n": [2],
                "viirs_n": [5],
                "harmonized": [3.0],
                "frp_sum": [10.0],
            }
        )
        tips = grid_tooltip_frame(harmonized, date(2026, 11, 10), date(2026, 11, 16), k=5)
        record = {name: tips.iloc[0][name] for name in GRID_TOOLTIP_FIELDS}
        record.update(radius=12000, fill_r=68, fill_g=1, fill_b=84)
        layer = pdk.Layer(
            "ScatterplotLayer",
            data=[record],
            get_position="[lon, lat]",
            get_radius="radius",
            get_fill_color="[fill_r, fill_g, fill_b, 210]",
            pickable=True,
        )
        deck = pdk.Deck(layers=[layer], tooltip={"html": GRID_TOOLTIP_HTML})
        payload = json.loads(deck.to_json())
        data = payload["layers"][0]["data"][0]
        self.assertEqual(deck._tooltip["html"], GRID_TOOLTIP_HTML)
        self.assertTrue(layer.pickable)
        self.assertTrue(payload["layers"][0]["pickable"])
        for name in GRID_TOOLTIP_FIELDS:
            self.assertIn("{" + name + "}", deck._tooltip["html"])
            self.assertIn(name, data)
        self.assertEqual(int(data["modis_n"]), 2)
        self.assertEqual(float(data["viirs_over_k"]), 1.0)
        self.assertIn("MODIS n:", GRID_TOOLTIP_HTML)
        self.assertIn("VIIRS/k:", GRID_TOOLTIP_HTML)


if __name__ == "__main__":
    unittest.main()
