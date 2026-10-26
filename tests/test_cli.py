"""Unit tests for StageSync CLI commands, formatting helpers, and review flows."""

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.cli import format_duration, format_file_size, cmd_status, cmd_review
from stagesync.database.connection import Database
from stagesync.database.models import Concert, Identification, Venue, Video, VideoStatus


class TestCLI(unittest.TestCase):
    """Test suite validating CLI formatting functions and non-interactive review execution."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cli.db"
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(None), "00:00")
        self.assertEqual(format_duration(0.0), "00:00")
        self.assertEqual(format_duration(45.0), "00:45")
        self.assertEqual(format_duration(84.25), "01:24")
        self.assertEqual(format_duration(3665.0), "01:01:05")

    def test_format_file_size(self) -> None:
        self.assertEqual(format_file_size(None), "0 B")
        self.assertEqual(format_file_size(0), "0 B")
        self.assertEqual(format_file_size(512), "512.0 B")
        self.assertEqual(format_file_size(1024), "1.0 KB")
        self.assertEqual(format_file_size(1024 * 1024 * 5), "5.0 MB")
        self.assertEqual(format_file_size(1024 * 1024 * 1024 * 2), "2.0 GB")

    @patch("stagesync.cli.get_db")
    def test_cmd_status_runs_cleanly(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = self.db
        args = argparse.Namespace()
        # Should execute without throwing any exception
        cmd_status(args)

    @patch("stagesync.cli.get_db")
    def test_cmd_review_non_interactive(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = self.db

        # Seed candidate in review queue
        v = Video(
            id="v_review_1",
            source_photo_id="sp_100",
            filename="PXL_concert.mp4",
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        self.db.save_video(v)

        ident = Identification(
            video_id=v.id,
            detected_song_title="Dragon",
            detected_artist="King Gizzard",
            match_confidence=0.92,
        )
        self.db.save_identification(ident)

        args = argparse.Namespace(non_interactive=True)
        # Should print table without prompting
        cmd_review(args)


if __name__ == "__main__":
    unittest.main()
