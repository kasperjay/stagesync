"""Unit tests for Setlist.fm API client and response parsing."""

import unittest
from stagesync.intelligence.setlist_fm import SetlistFmClient
from tests.conftest import SAMPLE_SETLIST_FM_PAYLOAD


class TestSetlistFm(unittest.TestCase):
    """Test suite for Setlist.fm payload parsing, rate limiting, and date transformations."""

    def setUp(self) -> None:
        self.client = SetlistFmClient(api_key="test_api_key")

    def test_parse_setlist_payload(self) -> None:
        raw_item = SAMPLE_SETLIST_FM_PAYLOAD["setlist"][0]
        matched = self.client.parse_setlist_payload(raw_item, fallback_date="2024-08-15")

        self.assertEqual(matched.setlist_fm_event_id, "3bca982c")
        self.assertEqual(matched.headliner_artist, "King Gizzard & The Lizard Wizard")
        self.assertEqual(matched.venue_name, "Red Rocks Amphitheatre")
        self.assertEqual(matched.city, "Morrison")
        self.assertEqual(matched.state_province, "Colorado")
        self.assertEqual(matched.country_code, "US")
        self.assertEqual(matched.event_date, "2024-08-15")
        self.assertEqual(matched.tour_name, "North America Tour 2024")

        # Setlist songs verification
        titles = matched.song_titles
        self.assertEqual(len(titles), 8)
        self.assertIn("Gila Monster", titles)
        self.assertIn("Dragon", titles)
        self.assertIn("Iron Lung", titles)

        # Verify encore songs
        encore_songs = [s for s in matched.setlist_songs if s.encore > 0]
        self.assertEqual(len(encore_songs), 2)
        self.assertEqual(encore_songs[0].name, "Iron Lung")

    def test_date_formatting(self) -> None:
        api_date = self.client._format_date_for_api("2024-08-15")
        self.assertEqual(api_date, "15-08-2024")

        std_date = self.client._format_date_standard("15-08-2024")
        self.assertEqual(std_date, "2024-08-15")

    def test_missing_api_key_graceful_fallback(self) -> None:
        unconfigured_client = SetlistFmClient(api_key=None)
        res = unconfigured_client.search_by_geo_and_date(
            event_date="2024-08-15",
            latitude=39.6653,
            longitude=-105.2058,
        )
        self.assertIsNone(res)

        res2 = unconfigured_client.search_by_city_and_date(
            event_date="2024-08-15",
            city_name="Morrison",
        )
        self.assertIsNone(res2)


if __name__ == "__main__":
    unittest.main()
