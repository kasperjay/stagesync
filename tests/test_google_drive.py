"""Unit tests for Google Drive video discovery, querying, downloading, and pipeline ingestion."""

import datetime
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.database.connection import Database
from stagesync.database.models import Video, VideoStatus
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
        self.assertIn("mimeType contains 'video/'", query)
        self.assertIn("'folder_abc' in parents", query)
        self.assertIn("trashed = false", query)
        self.assertNotIn("createdTime >=", query)
        self.assertNotIn("createdTime <=", query)

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
        coordinator.metadata_parser = MagicMock()
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

    def test_pipeline_skips_drive_content_duplicate(self) -> None:
        """Verify a second Drive file with identical bytes is not cataloged twice."""
        coordinator = PipelineCoordinator(db=self.db)
        coordinator.drive_client = MagicMock()
        coordinator.metadata_parser = MagicMock()
        coordinator.metadata_parser.probe_file.side_effect = RuntimeError("test probe skipped")
        existing_path = Path(self.temp_dir.name) / "existing.mp4"
        existing_path.write_bytes(b"same-video-content")
        existing = self.db.save_video(
            Video(
                source_photo_id="drive_original",
                filename="original.mp4",
                file_path=str(existing_path),
                file_hash_sha256=hashlib.sha256(b"same-video-content").hexdigest(),
                processing_status=VideoStatus.DOWNLOADED,
            )
        )
        coordinator.drive_client.list_videos.return_value = (
            [{"id": "drive_duplicate", "name": "copy.mp4", "mimeType": "video/mp4", "size": "18", "videoMediaMetadata": {"durationMillis": "90000"}}],
            None,
        )

        def download(_file_id, target):
            target.write_bytes(b"same-video-content")
            return target, existing.file_hash_sha256

        coordinator.drive_client.download_video.side_effect = download
        videos = coordinator.scan_google_drive(dry_run=False)

        self.assertEqual(videos, [])
        self.assertEqual(len(self.db.list_videos()), 1)
        self.assertFalse((coordinator.download_dir / f"{existing.id}_copy.mp4").exists())

    def test_pipeline_continues_after_empty_filtered_drive_page(self) -> None:
        """Verify an API page with no video candidates does not stop discovery."""
        coordinator = PipelineCoordinator(db=self.db)
        coordinator.drive_client = MagicMock()
        coordinator.drive_client.list_videos.side_effect = [
            ([], "next-page"),
            (
                [
                    {
                        "id": "drv_later",
                        "name": "Later_Concert.MOV",
                        "mimeType": "video/quicktime",
                        "size": "5000000",
                        "createdTime": "2024-08-15T22:00:00Z",
                        "videoMediaMetadata": {"durationMillis": "95000"},
                    }
                ],
                None,
            ),
        ]

        videos = coordinator.scan_google_drive(dry_run=True, limit=50)

        self.assertEqual([video.filename for video in videos], ["Later_Concert.MOV"])
        self.assertEqual(coordinator.drive_client.list_videos.call_count, 2)

    def test_folder_scan_paginates_past_default_limit(self) -> None:
        """Verify folder scans continue through every Drive page instead of stopping at 50."""
        coordinator = PipelineCoordinator(db=self.db)
        coordinator.drive_client = MagicMock()
        first_page = [
            {
                "id": f"drv_{index}",
                "name": f"Concert_{index}.MOV",
                "mimeType": "video/quicktime",
                "size": "5000000",
                "videoMediaMetadata": {"durationMillis": "95000"},
            }
            for index in range(50)
        ]
        second_page = [
            {
                "id": "drv_50",
                "name": "Concert_50.MOV",
                "mimeType": "video/quicktime",
                "size": "5000000",
                "videoMediaMetadata": {"durationMillis": "95000"},
            }
        ]
        coordinator.drive_client.list_videos.side_effect = [
            (first_page, "next-page"),
            (second_page, None),
        ]

        videos = coordinator.scan_google_drive(folder_id="folder_abc", dry_run=True)

        self.assertEqual(len(videos), 51)
        self.assertEqual(coordinator.drive_client.list_videos.call_count, 2)

    def test_pipeline_filters_drive_dates_by_embedded_recording_time(self) -> None:
        """Verify Drive date filters use recording metadata instead of upload time."""
        coordinator = PipelineCoordinator(db=self.db)
        coordinator.drive_client = MagicMock()
        coordinator.metadata_parser = MagicMock()
        coordinator.drive_client.list_videos.return_value = (
            [
                {
                    "id": "drv_in_range",
                    "name": "In_Range.MOV",
                    "mimeType": "video/quicktime",
                    "size": "5000000",
                    "createdTime": "2026-10-27T22:00:00Z",
                    "videoMediaMetadata": {"durationMillis": "95000"},
                },
                {
                    "id": "drv_out_of_range",
                    "name": "Out_Of_Range.MOV",
                    "mimeType": "video/quicktime",
                    "size": "5000000",
                    "createdTime": "2026-10-27T22:01:00Z",
                    "videoMediaMetadata": {"durationMillis": "95000"},
                },
            ],
            None,
        )

        def download(file_id: str, target_file: Path):
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(b"video")
            return target_file, "hash"

        coordinator.drive_client.download_video.side_effect = download
        coordinator.metadata_parser.probe_file.side_effect = [
            MagicMock(
                recorded_at_utc="2023-10-13T20:45:35+00:00",
                recorded_at_local=None,
                duration_seconds=95.0,
                width=None,
                height=None,
                latitude=None,
                longitude=None,
            ),
            MagicMock(
                recorded_at_utc="2025-10-13T20:45:35+00:00",
                recorded_at_local=None,
                duration_seconds=95.0,
                width=None,
                height=None,
                latitude=None,
                longitude=None,
            ),
        ]

        videos = coordinator.scan_google_drive(
            start_date="2023-01-01",
            end_date="2024-01-01",
            dry_run=False,
        )

        self.assertEqual([video.filename for video in videos], ["In_Range.MOV"])

    def test_pipeline_accepts_daytime_videos_and_filters_long_videos(self) -> None:
        coordinator = PipelineCoordinator(db=self.db)
        coordinator.drive_client = MagicMock()
        coordinator.drive_client.list_videos.return_value = (
            [
                {
                    "id": "drv_long",
                    "name": "Long_NonConcert.MOV",
                    "mimeType": "video/quicktime",
                    "size": "5000000",
                    "videoMediaMetadata": {
                        "durationMillis": "1200000",
                        "time": "2024-08-15T20:00:00-05:00",
                    },
                },
                {
                    "id": "drv_daytime",
                    "name": "Daytime_NonConcert.MOV",
                    "mimeType": "video/quicktime",
                    "size": "5000000",
                    "videoMediaMetadata": {
                        "durationMillis": "95000",
                        "time": "2024-08-15T11:00:00-05:00",
                    },
                },
                {
                    "id": "drv_concert",
                    "name": "Concert.MOV",
                    "mimeType": "video/quicktime",
                    "size": "5000000",
                    "videoMediaMetadata": {
                        "durationMillis": "95000",
                        "time": "2024-08-15T21:00:00-05:00",
                    },
                },
            ],
            None,
        )

        videos = coordinator.scan_google_drive(folder_id="folder_abc", dry_run=True)

        self.assertEqual([video.filename for video in videos], ["Daytime_NonConcert.MOV", "Concert.MOV"])

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
        self.assertFalse(is_likely_video_file("IMG_1058.MOV", "video/quicktime", 33645))  # Takeout still JPEG

        # Legitimate video candidates
        self.assertTrue(is_likely_video_file("PXL_20240815_redrocks.mp4", "video/mp4", 50_000_000))
        self.assertTrue(is_likely_video_file("IMG_1234.mov", "video/quicktime", 35_000_000))
        self.assertTrue(is_likely_video_file("concert_clip.mkv", "video/x-matroska", 120_000_000))


if __name__ == "__main__":
    unittest.main()
