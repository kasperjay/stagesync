"""Unit tests for Intelligence Services: AIAudioJudge, ShazamMatcher, and ReverseGeocoder."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.database.connection import Database
from stagesync.intelligence.ai_audio_judge import AIAudioJudge, AIAudioJudgeResult
from stagesync.intelligence.geocoder import ReverseGeocoder, ResolvedLocation
from stagesync.intelligence.shazam_matcher import ShazamMatcher, AcousticMatchResult
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


if __name__ == "__main__":
    unittest.main()
