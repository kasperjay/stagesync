"""Unit tests for Google Drive video discovery, querying, downloading, and pipeline ingestion."""

import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.database.connection import Database
from stagesync.database.models import VideoStatus
from stagesync.ingestion.google_drive import GoogleDriveClient, _parse_date, is_likely_video_file
from stagesync.pipeline import PipelineCoordinator


class TestGoogleDrive(unittest.TestCase):
    """Test suite validating Google Drive API queries, date ranges, and downloads."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = Path(self.temp_dir.name) / "drive_token.json"
        self.client = GoogleDriveClient(
            token_path=self.token_path,
            client_id="test_drive_id",
            client_secret="test_drive_secret",
        )
        self.db_path = Path(self.temp_dir.name) / "test_drive.db"
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_date_formats(self) -> None:
        """Verify date strings are parsed into datetime.date."""
        d = _parse_date("2024-07-20")
        self.assertEqual(d, datetime.date(2024, 7, 20))
        d_slash = _parse_date("2024/07/20")
        self.assertEqual(d_slash, datetime.date(2024, 7, 20))

    @patch.object(GoogleDriveClient, "get_access_token", return_value="fake_drive_token")
    @patch("requests.get")
    def test_list_videos_query_parameters(self, mock_get: MagicMock, mock_token: MagicMock) -> None:
        """Verify list_videos builds correct query syntax with mimeType, dates, and folder."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "files": [
                {
                    "id": "file_123",
                    "name": "concert_video.mp4",
                    "mimeType": "video/mp4",
                    "size": "5000000",
                    "createdTime": "2024-08-15T21:00:00Z",
                    "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": "60000"},
                    "imageMediaMetadata": {"location": {"latitude": 39.6653, "longitude": -105.2058}},
                }
            ],
            "nextPageToken": None,
        }
        mock_get.return_value = mock_resp

        with patch.object(self.client, "get_subfolder_ids", return_value=[]):
            files, next_token = self.client.list_videos(
                start_date="2024-08-01",
                end_date="2024-08-31",
                folder_id="folder_abc",
            )

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["id"], "file_123")
        self.assertIsNone(next_token)

        call_args, call_kwargs = mock_get.call_args
        params = call_kwargs["params"]
        query = params["q"]
        self.assertIn("'folder_abc' in parents", query)
        self.assertIn("trashed = false", query)
        self.assertIn("createdTime >= '2024-08-01T00:00:00Z'", query)
        self.assertIn("createdTime <= '2024-08-31T23:59:59Z'", query)

        # Global scan without folder_id targets video MIME types
        self.client.list_videos()
        call_args2, call_kwargs2 = mock_get.call_args
        self.assertIn("mimeType contains 'video/'", call_kwargs2["params"]["q"])

    @patch.object(GoogleDriveClient, "get_access_token", return_value="fake_drive_token")
    @patch("requests.get")
    def test_download_video_stream(self, mock_get: MagicMock, mock_token: MagicMock) -> None:
        """Verify download_video streams binary content and computes sha256."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": "10"}
        mock_resp.iter_content.return_value = [b"video_data_12345"]
        mock_resp.__enter__.return_value = mock_resp
        mock_get.return_value = mock_resp

        out_dest = Path(self.temp_dir.name) / "downloaded.mp4"
        path, sha = self.client.download_video("file_123", out_dest)

        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"video_data_12345")
        self.assertTrue(len(sha) == 64)

    def test_pipeline_coordinator_scan_google_drive(self) -> None:
        """Verify PipelineCoordinator.scan_google_drive catalogs discoveries properly."""
        coordinator = PipelineCoordinator(db=self.db)
        coordinator.drive_client = MagicMock()
        coordinator.drive_client.list_videos.return_value = (
            [
                {
                    "id": "drv_999",
                    "name": "Live_KingGizzard_RedRocks.mp4",
                    "mimeType": "video/mp4",
                    "size": "10485760",
                    "createdTime": "2024-08-15T22:00:00Z",
                    "videoMediaMetadata": {"width": 3840, "height": 2160, "durationMillis": "95000"},
                    "imageMediaMetadata": {"location": {"latitude": 39.6653, "longitude": -105.2058}},
                }
            ],
            None,
        )

        videos = coordinator.scan_google_drive(
            start_date="2024-08-15",
            end_date="2024-08-15",
            dry_run=True,
        )

        self.assertEqual(len(videos), 1)
        v = videos[0]
        self.assertEqual(v.source_photo_id, "drive_drv_999")
        self.assertEqual(v.filename, "Live_KingGizzard_RedRocks.mp4")
        self.assertEqual(v.width, 3840)
        self.assertEqual(v.height, 2160)
        self.assertEqual(v.duration_seconds, 95.0)
        self.assertAlmostEqual(v.latitude, 39.6653)
        self.assertAlmostEqual(v.longitude, -105.2058)
        self.assertEqual(v.processing_status, VideoStatus.DISCOVERED)

        # Verify saved in SQLite DB
        db_v = self.db.get_video_by_source_id("drive_drv_999")
        self.assertIsNotNone(db_v)
        self.assertEqual(db_v.id, v.id)

    def test_is_likely_video_file_filter(self) -> None:
        """Verify is_likely_video_file rejects code, presets, and small false-positives."""
        # False positives from Google Drive misclassification
        self.assertFalse(is_likely_video_file("index.d.mts", "video/mp2t", 13089))
        self.assertFalse(is_likely_video_file("esm.d.mts", "video/mp2t", 168))
        self.assertFalse(is_likely_video_file("acorn.d.mts", "video/mp2t", 21989))
        self.assertFalse(is_likely_video_file("03 longer throat 2.tfx", "video/mp4v-es", 432))
        self.assertFalse(is_likely_video_file("08 naselvox.tfx", "video/mp4v-es", 432))
        self.assertFalse(is_likely_video_file("package.json", "video/mp4", 500))

        # Under min_size_bytes
        self.assertFalse(is_likely_video_file("corrupt.mp4", "video/mp4", 50))

        # Legitimate video candidates
        self.assertTrue(is_likely_video_file("PXL_20240815_redrocks.mp4", "video/mp4", 50_000_000))
        self.assertTrue(is_likely_video_file("IMG_1234.mov", "video/quicktime", 35_000_000))
        self.assertTrue(is_likely_video_file("concert_clip.mkv", "video/x-matroska", 120_000_000))


if __name__ == "__main__":
    unittest.main()
