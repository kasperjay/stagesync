"""Unit tests for StageSync Web Dashboard & REST Server."""

import unittest
from unittest.mock import MagicMock

from aiohttp.test_utils import AioHTTPTestCase

from stagesync.database.models import PrivacyStatus, Video, VideoStatus
from stagesync.web.server import create_app


class TestWebServer(AioHTTPTestCase):
    """Test suite for Web Dashboard REST endpoints."""

    async def get_application(self):
        self.mock_coord = MagicMock()
        self.mock_db = MagicMock()
        self.mock_settings = MagicMock()
        self.mock_settings.max_daily_uploads = 5
        self.mock_settings.setlist_fm_api_key = "test_key"
        self.mock_settings.gemini_api_key = "test_key"

        return create_app(
            coordinator=self.mock_coord,
            db=self.mock_db,
            settings=self.mock_settings,
        )

    async def test_get_index(self):
        """Verify dashboard HTML is served at root."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("StageSync", text)
        self.assertIn("Review Queue", text)

    async def test_get_status_json(self):
        """Verify /api/status returns valid system statistics."""
        fake_video = Video(
            source_photo_id="sp_1",
            filename="live.mp4",
            duration_seconds=45.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        self.mock_db.list_videos.return_value = [fake_video]
        self.mock_db.get_todays_upload_count.return_value = 1
        self.mock_coord.photos_client.is_authenticated.return_value = True
        self.mock_coord.drive_client.is_authenticated.return_value = True
        self.mock_coord.uploader.auth.is_authenticated.return_value = False

        resp = await self.client.get("/api/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["total_videos"], 1)
        self.assertEqual(data["uploads_today"], 1)
        self.assertTrue(data["services"]["google_photos"]["authenticated"])
        self.assertTrue(data["services"]["google_drive"]["authenticated"])
        self.assertFalse(data["services"]["youtube"]["authenticated"])

    async def test_approve_video_endpoint(self):
        """Verify /api/video/{id}/approve calls coordinator and returns updated video."""
        fake_video = Video(
            id="v_123",
            source_photo_id="sp_1",
            filename="live.mp4",
            duration_seconds=45.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
            processing_status=VideoStatus.APPROVED,
        )
        self.mock_coord.approve_video.return_value = fake_video

        resp = await self.client.post("/api/video/v_123/approve", json={"title": "Custom Live Title"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["video"]["id"], "v_123")
        self.mock_coord.approve_video.assert_called_once_with(
            video_id="v_123",
            title_override="Custom Live Title",
            privacy_override=None,
        )

    async def test_reject_video_endpoint(self):
        """Verify /api/video/{id}/reject marks video rejected."""
        fake_video = Video(
            id="v_123",
            source_photo_id="sp_1",
            filename="live.mp4",
            duration_seconds=45.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
            processing_status=VideoStatus.REJECTED,
        )
        self.mock_coord.reject_video.return_value = fake_video

        resp = await self.client.post("/api/video/v_123/reject")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.mock_coord.reject_video.assert_called_once_with("v_123")

    async def test_scan_local_directory_endpoint(self):
        """Verify /api/scan with directory invokes scan_local_directory."""
        fake_video = Video(source_photo_id="loc_1", filename="clip.mp4")
        self.mock_coord.scan_local_directory.return_value = [fake_video]

        resp = await self.client.post("/api/scan", json={"directory": "/tmp/videos"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["scanned_count"], 1)
        self.assertEqual(data["mode"], "local_directory")
        self.mock_coord.scan_local_directory.assert_called_once_with("/tmp/videos")

    async def test_scan_google_drive_endpoint(self):
        """Verify /api/scan with provider=google_drive calls scan_google_drive."""
        fake_video = Video(source_photo_id="drive_1", filename="drive_clip.mp4")
        self.mock_coord.scan_google_drive.return_value = [fake_video]

        resp = await self.client.post("/api/scan", json={"provider": "google_drive", "days": 30})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["scanned_count"], 1)
        self.assertEqual(data["mode"], "google_drive")
        self.mock_coord.scan_google_drive.assert_called_once()

    async def test_scan_google_photos_scope_error_handling(self):
        """Verify 403 insufficient scopes error returns transparent diagnostics."""
        self.mock_coord.scan_google_photos.side_effect = RuntimeError(
            "Google Photos query failed: 403 Client Error: Forbidden: Request had insufficient authentication scopes."
        )

        resp = await self.client.post("/api/scan", json={"start_date": "2024-01-01"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertFalse(data["success"])
        self.assertTrue(data["is_scope_error"])
        self.assertIn("insufficient authentication scopes", data["error"])

    async def test_picker_create_and_poll_endpoints(self):
        """Verify /api/picker/create and /api/picker/poll flow."""
        self.mock_coord.photos_client.create_picker_session.return_value = {
            "id": "sess_123",
            "pickerUri": "https://photos.google.com/picker/sess_123",
        }
        resp = await self.client.post("/api/picker/create")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["session"]["id"], "sess_123")

        # Incomplete poll
        self.mock_coord.photos_client.poll_picker_session.return_value = False
        resp = await self.client.post("/api/picker/poll", json={"session_id": "sess_123"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["completed"])

        # Completed poll
        self.mock_coord.photos_client.poll_picker_session.return_value = True
        self.mock_coord.photos_client.get_picker_items.return_value = [{"id": "item_1", "baseUrl": "http://img"}]
        fake_v = Video(source_photo_id="item_1", filename="picker.mp4")
        self.mock_coord.ingest_picker_items.return_value = [fake_v]

        resp = await self.client.post("/api/picker/poll", json={"session_id": "sess_123"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["completed"])
        self.assertEqual(data["ingested_count"], 1)
        self.mock_coord.photos_client.delete_picker_session.assert_called_once_with("sess_123")

    async def test_video_thumbnail_fallback(self):
        """Verify /api/video/{id}/thumbnail returns fallback SVG when not found."""
        self.mock_db.get_video.return_value = None
        resp = await self.client.get("/api/video/v_nonexistent/thumbnail")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/svg+xml")
        text = await resp.text()
        self.assertIn("<svg", text)
        self.assertIn("Video Preview Frame", text)

    async def test_venue_search_endpoint(self):
        """Verify /api/venues/search calls venue_search_engine and returns results."""
        self.mock_coord.venue_search_engine.search.return_value = [
            {
                "name": "Come and Take It Live",
                "city": "Austin",
                "state": "TX",
                "country": "US",
                "formatted_address": "2015 E Riverside Dr, Austin, TX",
                "latitude": 30.2408,
                "longitude": -97.7288,
                "setlist_fm_venue_id": "63d55e97",
                "osm_id": "382484067",
                "sources": ["local", "setlist_fm"],
            }
        ]
        resp = await self.client.get("/api/venues/search?q=Come&city=Austin")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["venues"][0]["name"], "Come and Take It Live")
        self.mock_coord.venue_search_engine.search.assert_called_once_with(
            query="Come",
            city="Austin",
            limit=15,
        )

    async def test_set_video_venue_endpoint(self):
        """Verify /api/video/{id}/set-venue links venue and re-identifies concert."""
        fake_video = Video(
            id="v_100",
            source_photo_id="sp_100",
            filename="IMG_6130.MOV",
            duration_seconds=25.0,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        fake_concert = MagicMock()
        fake_concert.model_dump.return_value = {"id": "c_1", "headliner_artist": "Attila"}
        fake_ident = MagicMock()
        fake_ident.model_dump.return_value = {"id": "id_1", "detected_song_title": "About That Life"}

        self.mock_coord.reidentify_video_with_venue.return_value = (fake_video, fake_concert, fake_ident)

        payload = {
            "venue_name": "Come and Take It Live",
            "city": "Austin",
            "state_province": "TX",
            "country_code": "US",
            "latitude": 30.2408,
            "longitude": -97.7288,
            "setlist_fm_venue_id": "63d55e97",
        }
        resp = await self.client.post("/api/video/v_100/set-venue", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["video"]["id"], "v_100")
        self.mock_coord.reidentify_video_with_venue.assert_called_once()

    async def test_set_video_venue_validation_error(self):
        """Verify /api/video/{id}/set-venue requires venue_name."""
        resp = await self.client.post("/api/video/v_100/set-venue", json={})
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertFalse(data["success"])
        self.assertIn("Missing venue_name", data["error"])


if __name__ == "__main__":
    unittest.main()
