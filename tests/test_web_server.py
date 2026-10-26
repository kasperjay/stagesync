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
        self.mock_coord.uploader.auth.is_authenticated.return_value = False

        resp = await self.client.get("/api/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["total_videos"], 1)
        self.assertEqual(data["uploads_today"], 1)
        self.assertTrue(data["services"]["google_photos"]["authenticated"])
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


if __name__ == "__main__":
    unittest.main()
