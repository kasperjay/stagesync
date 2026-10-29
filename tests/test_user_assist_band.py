"""Unit tests for User-Assisted Band Selection from Concert Lineup."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stagesync.database.connection import Database
from stagesync.database.models import (
    Concert,
    Identification,
    PrivacyStatus,
    Venue,
    Video,
    VideoStatus,
    YouTubeUpload,
)
from stagesync.intelligence.setlist_fm import SetlistFmClient
from stagesync.intelligence.ai_audio_judge import AIAudioJudgeResult
from stagesync.intelligence.shazam_matcher import AcousticMatchResult
from stagesync.pipeline import PipelineCoordinator


class TestUserAssistBandSelection(unittest.TestCase):
    """Test suite for user-assisted band selection."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_band.db"
        self.db = Database(self.db_path)
        self.coordinator = PipelineCoordinator(db=self.db)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_reidentify_video_with_lineup_band(self):
        """Verify reidentify_video_with_band updates artist, metadata, and status."""
        # 1. Create venue and concert with lineup
        venue = Venue(
            name="Come and Take It Live",
            city="Austin",
            state_province="TX",
            country_code="US",
            latitude=30.2408,
            longitude=-97.7288,
        )
        saved_venue = self.db.save_venue(venue)

        concert = Concert(
            venue_id=saved_venue.id,
            headliner_artist="Attila",
            event_date="2023-10-13",
            tour_name="About That Life 10 Year Anniversary",
            lineup_artists=["Attila", "Gideon", "ten56.", "Until I Wake"],
        )
        saved_concert = self.db.save_concert(concert)

        # 2. Create candidate video with unconfirmed song
        video = Video(
            source_photo_id="sp_band_1",
            filename="IMG_6142.MOV",
            file_path="/tmp/fake_video.mov",
            venue_id=saved_venue.id,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        saved_video = self.db.save_video(video)

        ident = Identification(
            video_id=saved_video.id,
            concert_id=saved_concert.id,
            detected_artist="Attila",
            detected_song_title="Live Performance",
            match_confidence=0.0,
            status="UNVERIFIED",
        )
        self.db.save_identification(ident)

        # Pre-create a YouTube draft job
        yt_meta = self.coordinator.metadata_gen.generate_metadata(
            video=saved_video,
            identification=ident,
            concert=saved_concert,
            venue=saved_venue,
        )
        self.db.save_youtube_upload(
            YouTubeUpload(
                video_id=saved_video.id,
                title=yt_meta.title,
                description=yt_meta.description,
                tags=yt_meta.tags,
                category_id=yt_meta.category_id,
            )
        )

        # 3. Call reidentify_video_with_band to assign "Gideon"
        up_vid, up_concert, up_ident = self.coordinator.reidentify_video_with_band(
            video_id=saved_video.id,
            artist_name="Gideon",
        )

        self.assertEqual(up_ident.detected_artist, "Gideon")
        self.assertEqual(up_ident.status, "USER_ASSISTED_BAND")
        self.assertTrue(up_ident.setlist_verified)
        self.assertEqual(up_vid.processing_status, VideoStatus.PENDING_REVIEW)

        # Verify YouTube draft title was updated with the chosen band
        job = self.db.get_youtube_upload_by_video_id(saved_video.id)
        self.assertIsNotNone(job)
        self.assertIn("Gideon", job.title)
        self.assertIn("Come and Take It Live", job.title)

    def test_reidentify_video_with_unlisted_opener(self):
        """Verify adding an unlisted opener adds artist to concert lineup."""
        venue = Venue(
            name="Stubbs",
            city="Austin",
            country_code="US",
            latitude=30.2672,
            longitude=-97.7365,
        )
        saved_venue = self.db.save_venue(venue)

        concert = Concert(
            venue_id=saved_venue.id,
            headliner_artist="Thrice",
            event_date="2023-05-18",
            lineup_artists=["Thrice"],
        )
        saved_concert = self.db.save_concert(concert)

        video = Video(
            source_photo_id="sp_thrice_1",
            filename="IMG_0001.MOV",
            file_path="/tmp/fake_thrice.mov",
            venue_id=saved_venue.id,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        saved_video = self.db.save_video(video)

        ident = Identification(
            video_id=saved_video.id,
            concert_id=saved_concert.id,
            detected_artist="Thrice",
            detected_song_title="Live Performance",
            match_confidence=0.0,
            status="UNVERIFIED",
        )
        self.db.save_identification(ident)

        # Select custom/unlisted opener "Holy Fawn"
        up_vid, up_concert, up_ident = self.coordinator.reidentify_video_with_band(
            video_id=saved_video.id,
            artist_name="Holy Fawn",
        )

        self.assertEqual(up_ident.detected_artist, "Holy Fawn")
        # Lineup should now include Holy Fawn
        persisted_concert = self.db.get_concert(saved_concert.id)
        self.assertIn("Holy Fawn", persisted_concert.lineup_artists)

    def test_reidentify_video_with_band_continues_to_song_matching(self):
        """Selecting an artist should immediately attempt song identification."""
        venue = self.db.save_venue(
            Venue(
                name="Come and Take It Live",
                city="Austin",
                state_province="TX",
                country_code="US",
                latitude=30.2408,
                longitude=-97.7288,
            )
        )
        concert = self.db.save_concert(
            Concert(
                venue_id=venue.id,
                headliner_artist="Attila",
                event_date="2023-10-13",
                lineup_artists=["Attila", "Gideon"],
                setlist_data={"sets": {"set": [{"song": [{"name": "Middle Fingers Up"}]}]}},
            )
        )
        source_video = Path(self.tmp_dir.name) / "clip.mov"
        source_video.write_bytes(b"video")
        video = self.db.save_video(
            Video(
                source_photo_id="sp_band_song_1",
                filename="clip.mov",
                file_path=str(source_video),
                duration_seconds=45.0,
                venue_id=venue.id,
                processing_status=VideoStatus.PENDING_REVIEW,
            )
        )
        ident = self.db.save_identification(
            Identification(
                video_id=video.id,
                concert_id=concert.id,
                detected_artist="Attila",
                detected_song_title="Live Performance",
                match_confidence=0.0,
                status="UNVERIFIED",
            )
        )

        with patch.object(
            self.coordinator.audio_extractor,
            "extract_clip",
            return_value=source_video,
        ) as extract_clip, patch.object(
            self.coordinator.shazam_matcher,
            "recognize",
            return_value=AcousticMatchResult(matched=False),
        ), patch.object(
            self.coordinator.ai_audio_judge,
            "evaluate_live_clip",
            return_value=AIAudioJudgeResult(
                matched=True,
                song_title="Middle Fingers Up",
                confidence=0.91,
                setlist_index=1,
                lyrics_detected="middle fingers up",
            ),
        ) as evaluate_live_clip:
            _, _, updated_ident = self.coordinator.reidentify_video_with_band(
                video_id=video.id,
                artist_name="Gideon",
            )

        extract_clip.assert_called_once()
        evaluate_live_clip.assert_called_once()
        self.assertEqual(updated_ident.detected_artist, "Gideon")
        self.assertEqual(updated_ident.detected_song_title, "Middle Fingers Up")
        self.assertEqual(updated_ident.match_confidence, 0.91)
        self.assertEqual(updated_ident.track_number_in_setlist, 1)
        self.assertEqual(updated_ident.status, "USER_ASSISTED_BAND")

    def test_lineup_extraction_from_setlist_payload(self):
        """Verify parse_setlist_payload aggregates lineup across all event setlists."""
        client = SetlistFmClient(api_key="fake-key")

        mock_payload = {
            "id": "set1",
            "eventDate": "13-10-2023",
            "artist": {"name": "Attila"},
            "venue": {"name": "Come and Take It Live", "city": {"name": "Austin", "country": {"code": "US"}}},
            "sets": {"set": [{"song": [{"name": "Middle Fingers Up"}]}]},
        }
        all_event_setlists = [
            {"artist": {"name": "Attila"}},
            {"artist": {"name": "Gideon"}},
            {"artist": {"name": "ten56."}},
            {"artist": {"name": "Until I Wake"}},
        ]

        result = client.parse_setlist_payload(
            mock_payload,
            fallback_date="2023-10-13",
            all_setlists=all_event_setlists,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.headliner_artist, "Attila")
        self.assertEqual(result.lineup_artists, ["Attila", "Gideon", "ten56.", "Until I Wake"])


if __name__ == "__main__":
    unittest.main()
