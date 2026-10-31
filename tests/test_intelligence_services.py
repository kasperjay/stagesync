"""Unit tests for Intelligence Services: AIAudioJudge, ShazamMatcher, and ReverseGeocoder."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.config import Settings
from stagesync.database.connection import Database
from stagesync.database.models import Concert, Identification, Video, VideoStatus
from stagesync.intelligence.ai_audio_judge import AIAudioJudge, AIAudioJudgeResult
from stagesync.intelligence.geocoder import ReverseGeocoder, ResolvedLocation
from stagesync.intelligence.musicbrainz import MusicBrainzClient
from stagesync.intelligence.shazam_matcher import ShazamMatcher, AcousticMatchResult
from stagesync.pipeline import PipelineCoordinator
from tests.conftest import SAMPLE_NOMINATIM_PAYLOAD, SAMPLE_SHAZAM_PAYLOAD


class TestIntelligenceServices(unittest.TestCase):
    """Test suite validating AI audio disambiguation, acoustic matching, and geocoding."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        self.db_path = self.base_path / "test_intel.db"
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # AIAudioJudge Tests
    # -------------------------------------------------------------------------

    def test_ai_audio_judge_missing_file_raises(self) -> None:
        judge = AIAudioJudge(api_key="mock_key")
        with self.assertRaises(FileNotFoundError):
            judge.evaluate_live_clip(
                audio_path=self.base_path / "missing.mp3",
                candidate_songs=["Song A"],
                artist_name="Artist",
            )

    def test_ai_audio_judge_empty_candidates_returns_unmatched(self) -> None:
        audio = self.base_path / "clip.mp3"
        audio.write_bytes(b"dummy")
        judge = AIAudioJudge(api_key="mock_key")

        res = judge.evaluate_live_clip(
            audio_path=audio,
            candidate_songs=[],
            artist_name="Artist",
        )
        self.assertFalse(res.matched)
        self.assertEqual(res.confidence, 0.0)

    def test_ai_audio_judge_unconfigured_fallback(self) -> None:
        audio = self.base_path / "clip.mp3"
        audio.write_bytes(b"dummy")
        judge = AIAudioJudge(api_key="")

        res = judge.evaluate_live_clip(
            audio_path=audio,
            candidate_songs=["Dragon", "Gila Monster"],
            artist_name="King Gizzard",
        )
        self.assertFalse(res.matched)
        self.assertIn("not configured", res.reasoning)

    def test_ai_audio_judge_parse_gemini_json_markdown_wrapped(self) -> None:
        judge = AIAudioJudge(api_key="mock_key")
        raw_md = """```json
{
  "song_title": "Dragon",
  "confidence": 0.96,
  "setlist_index": 5,
  "lyrics_detected": "PetroDragonic Apocalypse",
  "reasoning": "Acoustic drum breakdown and distinct vocal rhythm match Dragon."
}
```"""
        candidate_songs = ["Daily Blues", "Muddy Water", "Dragon", "Flamethrower"]
        res = judge._parse_gemini_json(raw_md, candidate_songs, {})

        self.assertTrue(res.matched)
        self.assertEqual(res.song_title, "Dragon")
        self.assertEqual(res.confidence, 0.96)
        self.assertEqual(res.setlist_index, 5)
        self.assertEqual(res.lyrics_detected, "PetroDragonic Apocalypse")

    @patch("stagesync.intelligence.ai_audio_judge.requests.post")
    def test_ai_audio_judge_successful_eval(self, mock_post: MagicMock) -> None:
        audio = self.base_path / "dragon_clip.mp3"
        audio.write_bytes(b"audio binary data")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"song_title": "Dragon", "confidence": 0.92, "setlist_index": 1, "lyrics_detected": "Dragon", "reasoning": "Matched"}'
                            }
                        ]
                    }
                }
            ]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        judge = AIAudioJudge(api_key="valid_gemini_key")
        res = judge.evaluate_live_clip(
            audio_path=audio,
            candidate_songs=["Dragon", "Gila Monster"],
            artist_name="King Gizzard",
            venue_name="Red Rocks",
            event_date="2024-08-15",
        )

        self.assertTrue(res.matched)
        self.assertEqual(res.song_title, "Dragon")
        self.assertEqual(res.confidence, 0.92)

    # -------------------------------------------------------------------------
    # ShazamMatcher Tests
    # -------------------------------------------------------------------------

    def test_shazam_matcher_missing_file_raises(self) -> None:
        matcher = ShazamMatcher()
        with self.assertRaises(FileNotFoundError):
            matcher.recognize(self.base_path / "nonexistent.mp3")

    def test_shazam_matcher_payload_parsing(self) -> None:
        matcher = ShazamMatcher()
        parsed = matcher.parse_shazam_payload(SAMPLE_SHAZAM_PAYLOAD)

        self.assertTrue(parsed.matched)
        self.assertEqual(parsed.song_title, "Dragon")
        self.assertEqual(parsed.artist, "King Gizzard & The Lizard Wizard")
        self.assertEqual(parsed.album, "PetroDragonic Apocalypse")
        self.assertGreaterEqual(parsed.confidence, 0.90)

    def test_shazam_matcher_empty_payload(self) -> None:
        matcher = ShazamMatcher()
        parsed = matcher.parse_shazam_payload({})
        self.assertFalse(parsed.matched)
        self.assertEqual(parsed.confidence, 0.0)

    def test_shazam_matcher_recognize_many_preserves_input_order(self) -> None:
        matcher = ShazamMatcher()
        results = [
            AcousticMatchResult(matched=True, song_title="First", confidence=0.8),
            AcousticMatchResult(matched=True, song_title="Second", confidence=0.9),
        ]
        with patch.object(matcher, "recognize", side_effect=results) as recognize:
            actual = matcher.recognize_many(["first.mp3", "second.mp3"])

        self.assertEqual([result.song_title for result in actual], ["First", "Second"])
        self.assertEqual(recognize.call_count, 2)

    # -------------------------------------------------------------------------
    # ReverseGeocoder Tests
    # -------------------------------------------------------------------------

    def test_reverse_geocoder_cache_hit(self) -> None:
        lat = 39.6653
        lon = -105.2058
        cache_key = f"nom_{round(lat, 3):.3f}_{round(lon, 3):.3f}"

        # Seed local SQLite cache
        self.db.set_cached_geocode(
            cache_key=cache_key,
            data={
                "display_name": "Red Rocks Amphitheatre, Morrison, Colorado",
                "venue_name": "Red Rocks Amphitheatre",
                "city": "Morrison",
                "state_province": "Colorado",
                "country_code": "US",
                "osm_id": "way_12345",
                "raw": {},
            },
        )

        geocoder = ReverseGeocoder(db=self.db)
        # Should return cached result without making any network request
        res = geocoder.reverse_geocode(lat, lon)

        self.assertEqual(res.venue_name, "Red Rocks Amphitheatre")
        self.assertEqual(res.city, "Morrison")
        self.assertEqual(res.country_code, "US")
        self.assertEqual(res.osm_id, "way_12345")

    @patch("stagesync.intelligence.geocoder.requests.get")
    def test_reverse_geocoder_network_fallback(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = Exception("Nominatim timeout")
        geocoder = ReverseGeocoder(db=self.db)

        # Non-cached coordinates
        res = geocoder.reverse_geocode(latitude=12.3456, longitude=78.9101)
        self.assertEqual(res.city, "Unknown City")
        self.assertIn("12.3456", res.display_name)

    # -------------------------------------------------------------------------
    # Silent-Failure Regression & Manual Match Outcome Tests
    # -------------------------------------------------------------------------

    def test_ai_audio_judge_null_song_title_and_placeholder_filtering(self) -> None:
        judge = AIAudioJudge(api_key="valid_gemini_key")

        # Null song_title / confidence must not crash and must preserve reasoning
        null_parsed = judge._parse_gemini_json(
            '{"song_title": null, "confidence": null, "reasoning": "Instrumental intro without distinct vocal hook."}',
            candidate_songs=["Bite Down"],
            raw_payload={},
        )
        self.assertFalse(null_parsed.matched)
        self.assertIsNone(null_parsed.song_title)
        self.assertEqual(null_parsed.confidence, 0.0)
        self.assertIn("Instrumental intro", null_parsed.reasoning or "")

        # Generic placeholder title must be rejected as unmatched rather than saved
        placeholder_parsed = judge._parse_gemini_json(
            '{"song_title": "Live Performance", "confidence": 0.85, "reasoning": "Live band playing."}',
            candidate_songs=[],
            raw_payload={},
        )
        self.assertFalse(placeholder_parsed.matched)
        self.assertIsNone(placeholder_parsed.song_title)

    def test_shazam_matcher_uses_modern_recognize_method_and_captures_errors(self) -> None:
        audio = self.base_path / "modern_shazam.mp3"
        audio.write_bytes(b"ID3dummy")
        matcher = ShazamMatcher()

        class FakeModernShazam:
            async def recognize(self, path_str: str) -> dict:
                return SAMPLE_SHAZAM_PAYLOAD

        with patch("stagesync.intelligence.shazam_matcher.Shazam", FakeModernShazam, create=True), patch.dict(
            "sys.modules", {"shazamio": MagicMock(Shazam=FakeModernShazam)}
        ):
            res = matcher.recognize(audio)
            self.assertTrue(res.matched)
            self.assertEqual(res.song_title, "Dragon")
            self.assertIsNone(res.error)

        class BrokenShazam:
            async def recognize(self, path_str: str) -> dict:
                raise RuntimeError("Shazam upstream 503")

        with patch.dict("sys.modules", {"shazamio": MagicMock(Shazam=BrokenShazam)}):
            err_res = matcher.recognize(audio)
            self.assertFalse(err_res.matched)
            self.assertIn("503", err_res.error or "")

    @patch("stagesync.intelligence.musicbrainz.requests.get")
    def test_musicbrainz_raise_on_error_surfaces_failure(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = RuntimeError("DNS lookup failed")
        mb = MusicBrainzClient()

        # Default behavior stays resilient (empty list)
        self.assertEqual(mb.search_recordings("Gideon", query="Bite"), [])

        # Opted-in manual search surfaces error explicitly
        with self.assertRaises(RuntimeError):
            mb.search_recordings("Gideon", query="Bite", raise_on_error=True)

    def test_pipeline_match_video_song_preserves_opener_and_reports_outcomes(self) -> None:
        settings = Settings(
            database_url=f"sqlite:///{self.db_path}",
            cache_dir=self.base_path / "cache",
            gemini_api_key="test_gemini_key",
            setlist_fm_api_key="",
        )
        coord = PipelineCoordinator(db=self.db, settings=settings)

        clip_path = self.base_path / "opener_clip.mp4"
        clip_path.write_bytes(b"fakevideo")
        fake_window = self.base_path / "window_0.mp3"
        fake_window.write_bytes(b"fakeaudio")

        concert = Concert(
            id="concert_attila",
            event_date="2024-10-13",
            headliner_artist="Attila",
            lineup_artists=["Attila", "Gideon", "Ten56"],
            setlist_data={"songs": []},
        )
        self.db.save_concert(concert)

        video = Video(
            id="vid_opener",
            source_photo_id="sp_opener",
            filename="IMG_9001.MOV",
            file_path=str(clip_path),
            duration_seconds=42.0,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        self.db.save_video(video)
        ident = Identification(
            id="ident_opener",
            video_id=video.id,
            concert_id=concert.id,
            detected_artist="Gideon",
            detected_song_title="Live Performance",
            match_confidence=0.35,
        )
        self.db.save_identification(ident)

        # 1) No-match outcome preserves Gideon (never overwrites with headliner Attila)
        with patch.object(coord.audio_extractor, "extract_clips", return_value=[fake_window]), patch.object(
            coord.shazam_matcher,
            "recognize_many",
            return_value=[AcousticMatchResult(matched=False, confidence=0.0)],
        ), patch.object(
            coord.ai_audio_judge,
            "evaluate_live_clip",
            return_value=AIAudioJudgeResult(
                matched=False,
                song_title=None,
                confidence=0.15,
                reasoning="Heavy crowd distortion masked lyrics.",
            ),
        ):
            no_match_res = coord.match_video_song(video.id)
            self.assertTrue(no_match_res["success"])
            self.assertFalse(no_match_res["matched"])
            self.assertEqual(no_match_res["outcome"], "no_match")
            self.assertIn("No song match found", no_match_res["message"])
            saved_ident = self.db.get_identification_by_video_id(video.id)
            self.assertEqual(saved_ident.detected_artist, "Gideon")

        # 2) Matched outcome updates song title while preserving Gideon
        with patch.object(coord.audio_extractor, "extract_clips", return_value=[fake_window]), patch.object(
            coord.shazam_matcher,
            "recognize_many",
            return_value=[
                AcousticMatchResult(
                    matched=True,
                    song_title="No Love/No One",
                    artist="Gideon",
                    confidence=0.95,
                )
            ],
        ):
            match_res = coord.match_video_song(video.id)
            self.assertTrue(match_res["success"])
            self.assertTrue(match_res["matched"])
            self.assertEqual(match_res["outcome"], "matched")
            self.assertEqual(match_res["song_title"], "No Love/No One")
            saved_ident = self.db.get_identification_by_video_id(video.id)
            self.assertEqual(saved_ident.detected_artist, "Gideon")
            self.assertEqual(saved_ident.detected_song_title, "No Love/No One")

        # 3) Missing local video file returns explicit error outcome
        missing_video = Video(
            id="vid_missing_file",
            source_photo_id="sp_missing",
            filename="IMG_9999.MOV",
            file_path=str(self.base_path / "does_not_exist.mov"),
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        self.db.save_video(missing_video)
        err_res = coord.match_video_song(missing_video.id)
        self.assertFalse(err_res["success"])
        self.assertEqual(err_res["outcome"], "error")
        self.assertIn("missing from local storage", err_res["error"] or "")


if __name__ == "__main__":
    unittest.main()
