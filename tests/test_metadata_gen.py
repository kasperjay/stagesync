"""Unit tests for YouTube title, description, and tag generation engine."""

import unittest
from stagesync.database.models import Concert, Identification, Venue, Video
from stagesync.publisher.metadata_gen import (
    MetadataGenerator,
    sanitize_hashtag,
)


class TestMetadataGen(unittest.TestCase):
    """Test suite for YouTube metadata formatter adhering to API length constraints."""

    def setUp(self) -> None:
        self.gen = MetadataGenerator()

    def test_sanitize_hashtag(self) -> None:
        self.assertEqual(sanitize_hashtag("King Gizzard & The Lizard Wizard"), "KingGizzardTheLizardWizard")
        self.assertEqual(sanitize_hashtag("Red Rocks Amphitheatre!"), "RedRocksAmphitheatre")

    def test_format_title_standard_identified(self) -> None:
        title = self.gen.format_title(
            artist="King Gizzard & The Lizard Wizard",
            song_title="Dragon",
            venue_name="Red Rocks Amphitheatre",
            city="Morrison",
            event_date_str="2024-08-15",
        )
        self.assertLessEqual(len(title), 100)
        self.assertIn("King Gizzard & The Lizard Wizard - Dragon", title)
        self.assertIn("Live at", title)
        self.assertIn("Aug 15, 2024", title)

    def test_format_title_strict_length_invariant(self) -> None:
        # Extremely verbose artist, song, and venue that exceeds 120 characters
        long_artist = "The Extremely Elaborate And Extraordinary Orchestral Ensemble With Many Members"
        long_song = "Symphonic Movement Number Nine In B-Flat Minor With Extended Guitar Solo"
        long_venue = "The Historical Grand Opera House And Outdoor Amphitheatre Complex"

        title = self.gen.format_title(
            artist=long_artist,
            song_title=long_song,
            venue_name=long_venue,
            city="San Francisco",
            event_date_str="2024-08-15",
        )
        self.assertLessEqual(len(title), 100)
        self.assertTrue(len(title) > 0)

    def test_format_title_unidentified_clip(self) -> None:
        title = self.gen.format_title(
            artist="Radiohead",
            song_title=None,
            venue_name="Madison Square Garden",
            city="New York",
            event_date_str="2024-08-10",
            clip_index=3,
        )
        self.assertLessEqual(len(title), 100)
        self.assertIn("Radiohead - Live at Madison Square Garden", title)
        self.assertIn("[Clip #3]", title)

    def test_format_description(self) -> None:
        video = Video(
            source_photo_id="photo_123",
            filename="PXL_20240815.mp4",
            device_make="Google",
            device_model="Pixel 8 Pro",
        )
        venue = Venue(
            name="Red Rocks Amphitheatre",
            city="Morrison",
            state_province="CO",
            country_code="US",
            latitude=39.6653,
            longitude=-105.2058,
        )
        concert = Concert(
            venue_id=venue.id,
            headliner_artist="King Gizzard & The Lizard Wizard",
            tour_name="North America 2024",
            event_date="2024-08-15",
            source_url="https://www.setlist.fm/setlist/test.html",
        )
        ident = Identification(
            video_id=video.id,
            detected_song_title="Dragon",
            detected_artist="King Gizzard & The Lizard Wizard",
            match_confidence=0.95,
            track_number_in_setlist=5,
        )

        desc = self.gen.format_description(video, ident, concert, venue)

        self.assertIn('Live performance of "Dragon" by King Gizzard & The Lizard Wizard.', desc)
        self.assertIn("📍 Venue: Red Rocks Amphitheatre, Morrison, CO", desc)
        self.assertIn("🗓️ Date: Thursday, August 15, 2024", desc)
        self.assertIn("🎸 Tour: North America 2024", desc)
        self.assertIn("🎵 Setlist Context: Track #5", desc)
        self.assertIn("Recorded on Google Pixel 8 Pro.", desc)
        self.assertIn("https://www.setlist.fm/setlist/test.html", desc)
        self.assertIn("#LiveMusic", desc)
        self.assertIn("#KingGizzardTheLizardWizard", desc)

    def test_generate_tags(self) -> None:
        tags = self.gen.generate_tags(
            artist="King Gizzard",
            song_title="Dragon",
            venue_name="Red Rocks",
            city="Morrison",
            event_date_str="2024-08-15",
        )
        self.assertIn("King Gizzard", tags)
        self.assertIn("Dragon", tags)
        self.assertIn("Live", tags)
        self.assertIn("Red Rocks", tags)
        self.assertIn("2024", tags)
        # Total characters formatted
        total_len = sum(len(t) for t in tags) + len(tags)
        self.assertLessEqual(total_len, 500)


if __name__ == "__main__":
    unittest.main()
