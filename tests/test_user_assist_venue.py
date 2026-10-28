"""Unit tests for User-Assisted Venue Search and Re-identification."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.database.connection import Database
from stagesync.database.models import Concert, Identification, Venue, Video, VideoStatus
from stagesync.intelligence.geocoder import ReverseGeocoder
from stagesync.intelligence.setlist_fm import MatchedConcert, SetlistFmClient
from stagesync.intelligence.thumbnail_extractor import ThumbnailExtractor
from stagesync.intelligence.venue_search import VenueSearchEngine
from stagesync.pipeline import PipelineCoordinator


class TestThumbnailExtractor(unittest.TestCase):
    """Test suite for ThumbnailExtractor."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp_dir.name)
        self.extractor = ThumbnailExtractor(cache_dir=self.cache_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_fallback_svg(self):
        """Verify fallback SVG is valid and contains expected markers."""
        svg_bytes = self.extractor.get_fallback_svg()
        self.assertIsInstance(svg_bytes, bytes)
        self.assertIn(b"<svg", svg_bytes)
        self.assertIn(b"Video Preview Frame", svg_bytes)

    def test_cached_path(self):
        """Verify get_cached_path constructs proper destination."""
        path = self.extractor.get_cached_path("test-video-id")
        self.assertEqual(path.name, "test-video-id.jpg")
        self.assertEqual(path.parent, self.cache_dir)

    def test_nonexistent_video_raises_error(self):
        """Verify FileNotFoundError on non-existent video."""
        with self.assertRaises(FileNotFoundError):
            self.extractor.extract_thumbnail("/non/existent/video.mp4")


class TestVenueSearchEngine(unittest.TestCase):
    """Test suite for federated VenueSearchEngine."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_search.db"
        self.db = Database(self.db_path)
        self.mock_setlist = MagicMock(spec=SetlistFmClient)
        self.mock_geocoder = MagicMock(spec=ReverseGeocoder)
        self.engine = VenueSearchEngine(
            db=self.db,
            setlist_client=self.mock_setlist,
            geocoder=self.mock_geocoder,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_empty_query_returns_empty_list(self):
        """Verify empty query returns empty list immediately."""
        res = self.engine.search("")
        self.assertEqual(res, [])
        res2 = self.engine.search("   ")
        self.assertEqual(res2, [])

    def test_federated_search_and_deduplication(self):
        """Verify results from DB, Setlist.fm, and OSM are merged and deduplicated."""
        # Prepopulate local DB
        local_v = Venue(
            name="Come and Take It Live",
            city="Austin",
            state_province="TX",
            country_code="US",
            latitude=30.2408,
            longitude=-97.7288,
            osm_id="382484067",
        )
        self.db.save_venue(local_v)

        # Mock Setlist.fm returns same venue with setlist_fm_venue_id
        self.mock_setlist.search_venues.return_value = [
            {
                "id": "63d55e97",
                "name": "Come and Take It Live",
                "city": "Austin",
                "state": "TX",
                "country": "US",
                "latitude": 30.2408,
                "longitude": -97.7288,
            }
        ]

        # Mock OSM returns matching venue
        self.mock_geocoder.search_venues.return_value = [
            {
                "name": "Come and Take It Live",
                "city": "Austin",
                "state": "TX",
                "country": "US",
                "formatted_address": "2015 E Riverside Dr, Austin, TX 78741",
                "latitude": 30.240856,
                "longitude": -97.728875,
                "osm_id": "382484067",
            }
        ]

        results = self.engine.search("Come and Take It", city="Austin")
        self.assertGreaterEqual(len(results), 1)

        merged = results[0]
        self.assertEqual(merged["name"], "Come and Take It Live")
        self.assertEqual(merged["city"], "Austin")
        self.assertEqual(merged["setlist_fm_venue_id"], "63d55e97")
        self.assertEqual(merged["osm_id"], "382484067")
        self.assertIn("local", merged["sources"])
        self.assertIn("setlist_fm", merged["sources"])
        self.assertIn("osm", merged["sources"])


class TestReidentifyVideoWithVenue(unittest.TestCase):
    """Test suite for PipelineCoordinator.reidentify_video_with_venue."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_reident.db"
        self.db = Database(self.db_path)
        self.coord = PipelineCoordinator(db=self.db)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_reidentify_video_updates_venue_and_links_concert(self):
        """Verify reidentify_video_with_venue associates venue, searches setlist, and updates video."""
        # 1. Create candidate video without venue or coords
        video = Video(
            source_photo_id="sp_test_1",
            filename="IMG_6130.MOV",
            duration_seconds=25.0,
            recorded_at_local="2023-10-13T20:45:35",
            recorded_at_utc="2023-10-14T01:45:35Z",
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        video = self.db.save_video(video)

        # 2. Mock Setlist.fm search to return Attila concert
        fake_concert = MatchedConcert(
            setlist_fm_event_id="63a08a67",
            headliner_artist="Attila",
            lineup_artists=["Attila", "Gideon", "Ten56", "Until I Wake"],
            tour_name="About That Life 10 Year Anniversary",
            event_date="2023-10-13",
            venue_name="Come and Take It Live",
            venue_id="63d55e97",
            city="Austin",
            state_province="TX",
            country_code="US",
            raw_setlist_data={"sets": {"set": [{"song": [{"name": "Middle Fingers Up"}]}]}},
        )
        self.coord.setlist_client.search_by_venue_and_date = MagicMock(return_value=fake_concert)

        # 3. Call reidentify_video_with_venue
        up_vid, up_concert, up_ident = self.coord.reidentify_video_with_venue(
            video_id=video.id,
            venue_name="Come and Take It Live",
            city="Austin",
            state_province="TX",
            latitude=30.2408,
            longitude=-97.7288,
            setlist_fm_venue_id="63d55e97",
            osm_id="382484067",
        )

        # 4. Assertions
        self.assertEqual(up_vid.latitude, 30.2408)
        self.assertEqual(up_vid.longitude, -97.7288)
        self.assertIsNotNone(up_concert)
        self.assertEqual(up_concert.headliner_artist, "Attila")
        self.assertEqual(up_concert.event_date, "2023-10-13")

        self.assertIsNotNone(up_ident)
        self.assertEqual(up_ident.detected_artist, "Attila")
        self.assertTrue(up_ident.setlist_verified)

        # Verify DB persisted
        db_vid = self.db.get_video(video.id)
        self.assertEqual(db_vid.latitude, 30.2408)
        db_ident = self.db.get_identification_by_video_id(video.id)
        self.assertEqual(db_ident.detected_artist, "Attila")


if __name__ == "__main__":
    unittest.main()
