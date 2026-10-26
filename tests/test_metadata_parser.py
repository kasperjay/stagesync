"""Unit tests for EXIF and ISO 6709 Metadata Parser."""

import unittest
from pathlib import Path
from stagesync.ingestion.metadata_parser import (
    MetadataParser,
    parse_iso6709,
)
from tests.conftest import SAMPLE_RED_ROCKS_PROBE_JSON


class TestMetadataParser(unittest.TestCase):
    """Test suite for container metadata extraction and ISO 6709 coordinate decoding."""

    def test_parse_iso6709_standard_with_altitude(self) -> None:
        raw = "+37.7749-122.4194+012.000/"
        res = parse_iso6709(raw)
        self.assertIsNotNone(res)
        lat, lon, alt = res
        self.assertAlmostEqual(lat, 37.7749, places=4)
        self.assertAlmostEqual(lon, -122.4194, places=4)
        self.assertAlmostEqual(alt, 12.0, places=1)

    def test_parse_iso6709_without_altitude(self) -> None:
        raw = "+39.6653-105.2058/"
        res = parse_iso6709(raw)
        self.assertIsNotNone(res)
        lat, lon, alt = res
        self.assertAlmostEqual(lat, 39.6653, places=4)
        self.assertAlmostEqual(lon, -105.2058, places=4)
        self.assertIsNone(alt)

    def test_parse_iso6709_southern_eastern_hemisphere(self) -> None:
        raw = "-33.8688+151.2093/"
        res = parse_iso6709(raw)
        self.assertIsNotNone(res)
        lat, lon, alt = res
        self.assertAlmostEqual(lat, -33.8688, places=4)
        self.assertAlmostEqual(lon, 151.2093, places=4)

    def test_parse_iso6709_without_trailing_slash(self) -> None:
        raw = "+34.0522-118.2437"
        res = parse_iso6709(raw)
        self.assertIsNotNone(res)
        lat, lon, _ = res
        self.assertAlmostEqual(lat, 34.0522, places=4)
        self.assertAlmostEqual(lon, -118.2437, places=4)

    def test_parse_iso6709_invalid(self) -> None:
        self.assertIsNone(parse_iso6709(""))
        self.assertIsNone(parse_iso6709("Not a coordinate"))
        self.assertIsNone(parse_iso6709("+195.0000-120.0000/"))  # Latitude out of bounds

    def test_parse_probe_dict(self) -> None:
        parser = MetadataParser()
        dummy_path = Path("/tmp/PXL_20240815_214201.mp4")
        meta = parser.parse_probe_dict(
            probe_data=SAMPLE_RED_ROCKS_PROBE_JSON,
            path=dummy_path,
            file_size=192937984,
            file_hash="dummy_hash_12345",
        )

        self.assertEqual(meta.filename, "PXL_20240815_214201.mp4")
        self.assertAlmostEqual(meta.duration_seconds, 84.25, places=2)
        self.assertEqual(meta.width, 3840)
        self.assertEqual(meta.height, 2160)
        self.assertTrue(meta.has_audio)
        self.assertEqual(meta.latitude, 39.6653)
        self.assertEqual(meta.longitude, -105.2058)
        self.assertEqual(meta.altitude_meters, 1960.0)
        self.assertEqual(meta.device_make, "Google")
        self.assertEqual(meta.device_model, "Pixel 8 Pro")
        self.assertEqual(meta.recorded_at_local, "2024-08-15T21:42:01")
        self.assertEqual(meta.timezone_offset_minutes, -360)
        self.assertTrue(meta.is_concert_candidate)
        self.assertGreaterEqual(meta.concert_confidence_score, 0.80)

    def test_concert_candidate_heuristics(self) -> None:
        parser = MetadataParser()

        # No audio -> not candidate
        is_cand, score = parser.evaluate_concert_candidate(
            duration_s=60.0,
            recorded_at_local="2024-08-15T21:30:00",
            has_audio=False,
            has_location=True,
        )
        self.assertFalse(is_cand)
        self.assertEqual(score, 0.0)

        # Ideal prime-time concert snippet
        is_cand, score = parser.evaluate_concert_candidate(
            duration_s=90.0,
            recorded_at_local="2024-08-15T21:45:00",
            has_audio=True,
            has_location=True,
        )
        self.assertTrue(is_cand)
        self.assertGreaterEqual(score, 0.90)

        # Daytime pocket recording (noon) without location
        is_cand, score = parser.evaluate_concert_candidate(
            duration_s=4.0,  # Too short
            recorded_at_local="2024-08-15T12:00:00",
            has_audio=True,
            has_location=False,
        )
        self.assertFalse(is_cand)
        self.assertLess(score, 0.50)


if __name__ == "__main__":
    unittest.main()
