"""Unit tests for StageSync background watcher daemon."""

import unittest
from unittest.mock import MagicMock, patch

from stagesync.daemon import StageSyncDaemon
from stagesync.database.models import Video, VideoStatus


class TestStageSyncDaemon(unittest.TestCase):
    """Test suite for StageSyncDaemon."""

    def setUp(self) -> None:
        self.mock_coord = MagicMock()
        self.mock_db = MagicMock()
        self.mock_settings = MagicMock()
        self.daemon = StageSyncDaemon(
            coordinator=self.mock_coord,
            db=self.mock_db,
            settings=self.mock_settings,
            interval_minutes=5,
            auto_process=True,
            auto_upload=True,
        )

    def test_run_cycle_authenticated(self) -> None:
        """Verify normal cycle when services are authenticated."""
        self.mock_coord.photos_client.is_authenticated.return_value = True
        fake_video = Video(
            source_photo_id="test_sp",
            filename="test.mp4",
            duration_seconds=30.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
        )
        self.mock_coord.scan_google_photos.return_value = [fake_video]
        self.mock_coord.process_pending_videos.return_value = [fake_video]

        mock_upload_res = MagicMock()
        mock_upload_res.success = True
        self.mock_coord.upload_approved_videos.return_value = [mock_upload_res]

        stats = self.daemon.run_cycle()
        self.assertEqual(stats["scanned"], 1)
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["uploaded"], 1)
        self.assertEqual(len(stats["errors"]), 0)

    def test_run_cycle_unauthenticated(self) -> None:
        """Verify cycle skips remote scan if photos client is not authenticated."""
        self.mock_coord.photos_client.is_authenticated.return_value = False
        self.mock_coord.process_pending_videos.return_value = []
        self.mock_coord.upload_approved_videos.return_value = []

        stats = self.daemon.run_cycle()
        self.assertEqual(stats["scanned"], 0)
        self.mock_coord.scan_google_photos.assert_not_called()

    def test_daemon_start_run_once(self) -> None:
        """Verify start with run_once flag executes single cycle and terminates."""
        with patch.object(self.daemon, "run_cycle") as mock_cycle:
            self.daemon.start(run_once=True)
            mock_cycle.assert_called_once()
            self.assertTrue(self.daemon._running)


if __name__ == "__main__":
    unittest.main()
