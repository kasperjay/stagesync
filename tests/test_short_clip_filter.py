import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stagesync.database.connection import Database
from stagesync.database.models import Video, VideoStatus
from stagesync.pipeline import PipelineCoordinator


class TestShortClipFilter(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "stagesync.db")
        self.coordinator = PipelineCoordinator.__new__(PipelineCoordinator)
        self.coordinator.settings = SimpleNamespace(min_duration_seconds=8.0)
        self.coordinator.db = self.db

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_short_clip_is_rejected_before_processing(self) -> None:
        video = self.db.save_video(
            Video(
                source_photo_id="short-clip",
                filename="IMG_0001.MP4",
                duration_seconds=2.9,
                processing_status=VideoStatus.DOWNLOADED,
            )
        )

        result = self.coordinator.process_video(video)

        self.assertEqual(result.processing_status, VideoStatus.REJECTED)
        self.assertEqual(self.db.get_video(video.id).processing_status, VideoStatus.REJECTED)

    def test_threshold_allows_archival_length_video(self) -> None:
        self.assertFalse(self.coordinator._is_short_clip(8.0))
        self.assertTrue(self.coordinator._is_short_clip(7.99))
        self.assertFalse(self.coordinator._is_short_clip(None))


if __name__ == "__main__":
    unittest.main()
