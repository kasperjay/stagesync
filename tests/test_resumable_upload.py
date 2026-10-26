"""Unit tests for YouTube Resumable Chunked Uploader and daily quota guardrails."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.database.connection import Database
from stagesync.database.models import PrivacyStatus, Video, YouTubeUpload
from stagesync.publisher.metadata_gen import FormattedYouTubeMetadata
from stagesync.publisher.resumable_upload import ResumableUploader, UploadResult
from stagesync.publisher.youtube_auth import YouTubeAuth


class TestResumableUpload(unittest.TestCase):
    """Test suite for ResumableUploader 8MB streaming, backoff, and daily quota checks."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_upload.db"
        self.db = Database(self.db_path)
        self.mock_auth = MagicMock(spec=YouTubeAuth)
        self.mock_auth.get_access_token.return_value = "mock_access_token"
        self.uploader = ResumableUploader(auth=self.mock_auth, db=self.db, max_daily_uploads=2)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_daily_quota_guardrail(self) -> None:
        # Initially 0 uploads today
        self.assertTrue(self.uploader.check_daily_quota())

        # Create parent videos for foreign key integrity
        self.db.save_video(Video(id="v1", source_photo_id="sp_1", filename="v1.mp4"))
        self.db.save_video(Video(id="v2", source_photo_id="sp_2", filename="v2.mp4"))

        # Simulate 2 uploads today (reaching quota of 2)
        up1 = YouTubeUpload(
            video_id="v1",
            title="Concert 1",
            description="desc",
            upload_status="COMPLETED",
            uploaded_at="2026-10-26T10:00:00Z",
        )
        up2 = YouTubeUpload(
            video_id="v2",
            title="Concert 2",
            description="desc",
            upload_status="COMPLETED",
            uploaded_at="2026-10-26T10:30:00Z",
        )
        self.db.save_youtube_upload(up1)
        self.db.save_youtube_upload(up2)

        self.assertFalse(self.uploader.check_daily_quota())

    def test_upload_file_quota_exceeded_aborts_immediately(self) -> None:
        # Create parent videos and fill quota
        for i in range(2):
            vid_id = f"vid_{i}"
            self.db.save_video(Video(id=vid_id, source_photo_id=f"sp_{vid_id}", filename=f"{vid_id}.mp4"))
            self.db.save_youtube_upload(
                YouTubeUpload(
                    video_id=vid_id,
                    title=f"Title {i}",
                    description="desc",
                    upload_status="COMPLETED",
                    uploaded_at="2026-10-26T10:00:00Z",
                )
            )

        meta = FormattedYouTubeMetadata(title="Test Title", description="Desc", tags=["Live"])
        res = self.uploader.upload_file(
            video_id="vid_new",
            file_path=Path("/tmp/nonexistent.mp4"),
            metadata=meta,
        )

        self.assertFalse(res.success)
        self.assertIn("Daily YouTube upload quota reached", res.error_message)

    def test_upload_file_missing_file(self) -> None:
        meta = FormattedYouTubeMetadata(title="Test Title", description="Desc")
        res = self.uploader.upload_file(
            video_id="vid_test",
            file_path=Path(self.temp_dir.name) / "does_not_exist.mp4",
            metadata=meta,
        )
        self.assertFalse(res.success)
        self.assertIn("File not found", res.error_message)

    @patch("requests.post")
    def test_initiate_resumable_session(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.headers = {"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=sess_12345"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        meta = FormattedYouTubeMetadata(
            title="King Gizzard - Dragon (Live)",
            description="Live performance at Red Rocks.",
            tags=["King Gizzard", "Dragon", "Live"],
            recording_date="2024-08-15T21:42:01Z",
            latitude=39.6653,
            longitude=-105.2058,
        )

        upload_uri = self.uploader.initiate_resumable_session(
            metadata=meta,
            file_size=1024 * 1024 * 16,
            privacy=PrivacyStatus.UNLISTED,
            mime_type="video/mp4",
        )

        self.assertEqual(upload_uri, "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=sess_12345")
        self.assertTrue(mock_post.called)
        call_kwargs = mock_post.call_args[1]
        body = call_kwargs["json"]
        self.assertEqual(body["snippet"]["title"], "King Gizzard - Dragon (Live)")
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")
        self.assertEqual(body["recordingDetails"]["recordingDate"], "2024-08-15T21:42:01Z")

    @patch("requests.put")
    @patch("requests.post")
    def test_upload_file_chunked_completion(self, mock_post: MagicMock, mock_put: MagicMock) -> None:
        # Create a small dummy file (1000 bytes)
        video_file = Path(self.temp_dir.name) / "small_test.mp4"
        video_file.write_bytes(b"A" * 1000)

        # Mock initiate session
        mock_init_resp = MagicMock()
        mock_init_resp.headers = {"Location": "https://upload.youtube.com/sess_abc"}
        mock_init_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_init_resp

        # Mock chunk upload finishing with 200 OK
        mock_chunk_resp = MagicMock()
        mock_chunk_resp.status_code = 200
        mock_chunk_resp.json.return_value = {"id": "yt_video_9999"}
        mock_put.return_value = mock_chunk_resp

        meta = FormattedYouTubeMetadata(title="Small Concert Video", description="Desc")
        res = self.uploader.upload_file(
            video_id="v_small",
            file_path=video_file,
            metadata=meta,
            privacy=PrivacyStatus.UNLISTED,
        )

        self.assertTrue(res.success)
        self.assertEqual(res.youtube_video_id, "yt_video_9999")
        self.assertEqual(res.youtube_url, "https://youtu.be/yt_video_9999")
        self.assertEqual(res.bytes_uploaded, 1000)


if __name__ == "__main__":
    unittest.main()
