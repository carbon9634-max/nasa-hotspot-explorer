"""Loader tests against a tiny local archive. No GCS calls."""

import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from archive import (
    archive_location,
    harmonize_archive_grid,
    load_daily_grid,
    load_points,
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


if __name__ == "__main__":
    unittest.main()
