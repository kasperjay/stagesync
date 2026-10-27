"""Unit tests for Spatial-Temporal Neighbor Correlator.

Verifies EXIF GPS extraction, Takeout sidecar parsing, and temporal proximity matching
for iPhone videos that lack embedded location atoms.
"""

import datetime
import json
from pathlib import Path
import tempfile
import unittest

from stagesync.intelligence.spatial_correlator import (
    CorrelatedLocation,
    PhotoLocationSample,
    SpatialTemporalCorrelator,
)


class TestSpatialTemporalCorrelator(unittest.TestCase):
    """Test suite for spatial-temporal correlation logic."""

    def setUp(self) -> None:
        self.correlator = SpatialTemporalCorrelator(max_window_hours=4.0)

    def test_parse_takeout_sidecar_valid(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            data = {
                "title": "IMG_1142.JPG",
                "photoTakenTime": {"timestamp": "1481863570"},  # 2016-12-16 04:46:10 UTC
                "geoData": {
                    "latitude": 30.240764,
                    "longitude": -97.728814,
                    "altitude": 152.3,
                },
            }
            json.dump(data, f)
            temp_path = Path(f.name)

        try:
            sample = self.correlator.parse_takeout_sidecar(temp_path)
            self.assertIsNotNone(sample)
            assert sample is not None
            self.assertEqual(sample.filename, "IMG_1142.JPG")
            self.assertAlmostEqual(sample.latitude, 30.240764, places=5)
            self.assertAlmostEqual(sample.longitude, -97.728814, places=5)
            self.assertAlmostEqual(sample.altitude_meters, 152.3, places=1)
            self.assertEqual(
                sample.timestamp_utc,
                datetime.datetime.fromtimestamp(1481863570, tz=datetime.timezone.utc),
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def test_parse_takeout_sidecar_zero_coordinates_rejected(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            data = {
                "title": "IMG_0001.JPG",
                "photoTakenTime": {"timestamp": "1481863570"},
                "geoData": {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "altitude": 0.0,
                },
            }
            json.dump(data, f)
            temp_path = Path(f.name)

        try:
            sample = self.correlator.parse_takeout_sidecar(temp_path)
            self.assertIsNone(sample)
        finally:
            temp_path.unlink(missing_ok=True)

    def test_correlate_closest_neighbor_in_window(self) -> None:
        base_time = datetime.datetime(2024, 8, 15, 20, 0, 0, tzinfo=datetime.timezone.utc)

        sample_close = PhotoLocationSample(
            filename="photo_1.jpg",
            latitude=39.6653,
            longitude=-105.2058,
            altitude_meters=1900.0,
            timestamp_utc=base_time - datetime.timedelta(minutes=15),  # 15m before
            device_make="Apple",
            device_model="iPhone 14 Pro",
        )

        sample_far = PhotoLocationSample(
            filename="photo_2.jpg",
            latitude=39.7392,
            longitude=-104.9903,
            timestamp_utc=base_time + datetime.timedelta(hours=2),  # 2h after
            device_make="Apple",
            device_model="iPhone 14 Pro",
        )

        video_time = base_time.isoformat()
        corr = self.correlator.correlate_video_location(
            video_recorded_at=video_time,
            photo_samples=[sample_far, sample_close],
        )

        self.assertIsNotNone(corr)
        assert corr is not None
        self.assertAlmostEqual(corr.latitude, 39.6653, places=4)
        self.assertAlmostEqual(corr.longitude, -105.2058, places=4)
        self.assertEqual(corr.device_model, "iPhone 14 Pro")
        self.assertEqual(corr.time_delta_seconds, 900.0)

    def test_correlate_outside_max_window_falls_back_to_same_day(self) -> None:
        video_dt = datetime.datetime(2024, 8, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)

        # 8 hours earlier on the same day (beyond 4h max window)
        same_day_sample = PhotoLocationSample(
            filename="afternoon_soundcheck.jpg",
            latitude=39.6653,
            longitude=-105.2058,
            timestamp_utc=datetime.datetime(2024, 8, 15, 15, 0, 0, tzinfo=datetime.timezone.utc),
            device_make="Apple",
            device_model="iPhone 14 Pro",
        )

        # Different day
        other_day_sample = PhotoLocationSample(
            filename="yesterday.jpg",
            latitude=34.0522,
            longitude=-118.2437,
            timestamp_utc=datetime.datetime(2024, 8, 14, 23, 0, 0, tzinfo=datetime.timezone.utc),
        )

        corr = self.correlator.correlate_video_location(
            video_recorded_at=video_dt.isoformat(),
            photo_samples=[other_day_sample, same_day_sample],
        )

        self.assertIsNotNone(corr)
        assert corr is not None
        self.assertAlmostEqual(corr.latitude, 39.6653, places=4)
        self.assertAlmostEqual(corr.longitude, -105.2058, places=4)
        self.assertIn("same_day", corr.source_reference)

    def test_correlate_no_matching_samples(self) -> None:
        video_dt = datetime.datetime(2024, 8, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)
        diff_day = PhotoLocationSample(
            filename="last_year.jpg",
            latitude=34.0522,
            longitude=-118.2437,
            timestamp_utc=datetime.datetime(2023, 8, 15, 23, 0, 0, tzinfo=datetime.timezone.utc),
        )

        corr = self.correlator.correlate_video_location(
            video_recorded_at=video_dt.isoformat(),
            photo_samples=[diff_day],
        )
        self.assertIsNone(corr)

    def test_parse_timestamp_formats(self) -> None:
        """Verify robust parsing of EXIF colons and ISO timestamps."""
        # EXIF format with colons
        exif_dt = SpatialTemporalCorrelator.parse_timestamp("2023:10:13 20:34:13")
        self.assertIsNotNone(exif_dt)
        assert exif_dt is not None
        self.assertEqual(exif_dt.year, 2023)
        self.assertEqual(exif_dt.month, 10)
        self.assertEqual(exif_dt.day, 13)
        self.assertEqual(exif_dt.hour, 20)
        self.assertEqual(exif_dt.minute, 34)

        # ISO format
        iso_dt = SpatialTemporalCorrelator.parse_timestamp("2023-10-14T01:45:35Z")
        self.assertIsNotNone(iso_dt)
        assert iso_dt is not None
        self.assertEqual(iso_dt.year, 2023)
        self.assertEqual(iso_dt.month, 10)
        self.assertEqual(iso_dt.day, 14)
        self.assertEqual(iso_dt.hour, 1)
        self.assertEqual(iso_dt.minute, 45)

    def test_correlate_local_time_and_utc_cross_midnight(self) -> None:
        """Verify correlation when photo is recorded in local camera time and video is in UTC."""
        # Photo taken at 8:34 PM local time (20:34:13)
        photo_sample = PhotoLocationSample(
            filename="IMG_6127.JPG",
            latitude=30.2418,
            longitude=-97.7287,
            timestamp_utc=datetime.datetime(2023, 10, 13, 20, 34, 13, tzinfo=datetime.timezone.utc),
            device_make="Apple",
            device_model="iPhone 14 Pro",
        )

        # Video taken at 8:45 PM local time (which is 01:45 UTC next day in CDT)
        corr = self.correlator.correlate_video_location(
            video_recorded_at="2023-10-14T01:45:35Z",
            video_recorded_at_local="2023-10-13T20:45:35",
            photo_samples=[photo_sample],
        )

        self.assertIsNotNone(corr)
        assert corr is not None
        self.assertAlmostEqual(corr.latitude, 30.2418, places=4)
        self.assertAlmostEqual(corr.longitude, -97.7287, places=4)
        # Delta should be ~11.4 minutes (682 seconds), not 5 hours!
        self.assertAlmostEqual(corr.time_delta_seconds, 682.0, delta=5.0)

    def test_scan_local_directory_for_photos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dir_path = Path(tmp_dir)

            # Create sidecar JSON
            sidecar = dir_path / "IMG_2000.JPG.json"
            sidecar.write_text(
                json.dumps({
                    "title": "IMG_2000.JPG",
                    "photoTakenTime": {"timestamp": "1723758000"},
                    "geoData": {"latitude": 39.6653, "longitude": -105.2058, "altitude": 1900.0},
                })
            )

            # Create non-photo file
            (dir_path / "notes.txt").write_text("concert setlist draft")

            samples = self.correlator.scan_local_directory_for_photos(dir_path)
            self.assertEqual(len(samples), 1)
            self.assertAlmostEqual(samples[0].latitude, 39.6653, places=4)
            self.assertEqual(samples[0].filename, "IMG_2000.JPG")


if __name__ == "__main__":
    unittest.main()
