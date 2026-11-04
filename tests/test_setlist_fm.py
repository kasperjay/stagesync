"""Unit tests for Setlist.fm API client and response parsing."""

import unittest
from unittest.mock import patch
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

    def test_selected_context_rejects_wrong_venue_artist_and_date(self):
        from copy import deepcopy
        original = SAMPLE_SETLIST_FM_PAYLOAD["setlist"][0]
        for field in ("venue", "artist", "date", "missing_date"):
            candidate = deepcopy(original)
            if field == "venue":
                candidate["venue"]["name"] = "Red Rocks"
            elif field == "artist":
                candidate["artist"]["name"] = "Marzomets"
            elif field == "date":
                candidate["eventDate"] = "16-08-2024"
            else:
                candidate.pop("eventDate", None)
            with self.subTest(field=field), patch.object(self.client, "_fetch_setlists_raw", return_value={"setlist": [candidate]}):
                result = self.client.search_by_city_and_date(
                    "2024-08-15", "Morrison", "King Gizzard & The Lizard Wizard",
                    "Red Rocks Amphitheatre", max_pages=1)
                self.assertIsNone(result)

    def test_venue_lookup_excludes_other_dates_and_venues_from_lineup(self):
        from copy import deepcopy
        original = SAMPLE_SETLIST_FM_PAYLOAD["setlist"][0]
        unrelated = deepcopy(original)
        unrelated["artist"]["name"] = "Unrelated Artist"
        unrelated["eventDate"] = "16-08-2024"
        with patch.object(self.client, "_fetch_setlists_raw", return_value={"setlist": [unrelated, original]}):
            result = self.client.search_by_venue_and_date("2024-08-15", "Red Rocks Amphitheatre", "Morrison")
        self.assertEqual(result.headliner_artist, "King Gizzard & The Lizard Wizard")
        self.assertNotIn("Unrelated Artist", result.lineup_artists)

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

    def test_venue_search_preserves_matched_artist_for_multi_event_festival(self) -> None:
        festival = {
            "setlist": [
                {
                    "id": "festival-artist-a",
                    "eventDate": "15-08-2024",
                    "artist": {"name": "Artist A"},
                    "venue": {"name": "Festival Grounds", "city": {"name": "Austin", "country": {"code": "US"}}},
                    "sets": {"set": [{"song": [{"name": "Song A"}]}]},
                },
                {
                    "id": "festival-artist-b",
                    "eventDate": "15-08-2024",
                    "artist": {"name": "Artist B"},
                    "venue": {"name": "Festival Grounds", "city": {"name": "Austin", "country": {"code": "US"}}},
                    "sets": {"set": [{"song": [{"name": "Song B"}]}]},
                },
            ]
        }

        with patch.object(self.client, "_fetch_setlists_raw", return_value=festival):
            matched = self.client.search_by_venue_and_date(
                event_date="2024-08-15",
                venue_name="Festival Grounds",
                city_name="Austin",
            )

        self.assertIsNotNone(matched)
        self.assertEqual(matched.headliner_artist, "Artist A")
        self.assertEqual(matched.lineup_artists, ["Artist A", "Artist B"])
        self.assertEqual(
            matched.raw_setlist_data["_stagesync_lineup_setlists"],
            [
                {"artist": "Artist A", "songs": ["Song A"]},
                {"artist": "Artist B", "songs": ["Song B"]},
            ],
        )

    def test_search_concerts_applies_manual_filters_and_returns_all_records(self) -> None:
        payload = {
            "setlist": [{
                "id": "event-1",
                "eventDate": "15-08-2024",
                "artist": {"name": "Artist A"},
                "venue": {"id": "venue-1", "name": "The Venue", "city": {"name": "Austin", "country": {"code": "US"}}},
                "tour": {"name": "Summer Tour"},
                "sets": {"set": [{"song": [{"name": "Song A"}]}]},
                "url": "https://www.setlist.fm/setlist/event-1",
            }]
        }
        lineup_payload = {
            "setlist": [
                payload["setlist"][0],
                {
                    "id": "event-1-support",
                    "eventDate": "15-08-2024",
                    "artist": {"name": "Artist B"},
                    "venue": {"id": "venue-1", "name": "The Venue", "city": {"name": "Austin", "country": {"code": "US"}}},
                    "tour": {"name": "Summer Tour"},
                    "sets": {"set": [{"song": [{"name": "Song B"}]}]},
                },
            ]
        }
        with patch.object(self.client, "_fetch_setlists_raw", side_effect=[payload, lineup_payload]) as fetch:
            results = self.client.search_concerts(
                event_date="2024-08-15",
                artist_name="Artist A",
                venue_name="The Venue",
                tour_name="Summer Tour",
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].setlist_fm_event_id, "event-1")
        self.assertEqual(results[0].tour_name, "Summer Tour")
        self.assertEqual(results[0].lineup_artists, ["Artist A", "Artist B"])
        self.assertEqual(fetch.call_args_list[0].args[0], {
            "date": "15-08-2024",
            "artistName": "Artist A",
            "venueName": "The Venue",
            "tourName": "Summer Tour",
            "p": "1",
        })
        self.assertEqual(fetch.call_args_list[1].args[0], {
            "date": "15-08-2024",
            "venueName": "The Venue",
            "cityName": "Austin",
            "countryCode": "US",
            "p": "1",
        })

    def test_refresh_show_lineup_uses_unfiltered_show_records(self) -> None:
        selected = {
            "id": "event-1",
            "eventDate": "15-08-2024",
            "artist": {"name": "Artist A"},
            "venue": {"id": "venue-1", "name": "The Venue", "city": {"name": "Austin", "country": {"code": "US"}}},
            "tour": {"name": "Summer Tour"},
            "sets": {"set": [{"song": [{"name": "Song A"}]}]},
        }
        lineup = {
            "setlist": [
                selected,
                {**selected, "id": "event-2", "artist": {"name": "Artist B"}},
                {**selected, "id": "event-3", "artist": {"name": "Artist C"}},
            ]
        }
        matched = self.client.parse_setlist_payload(selected, "2024-08-15")
        with patch.object(self.client, "_fetch_setlists_raw", return_value=lineup) as fetch:
            refreshed = self.client.refresh_show_lineup(matched)

        self.assertEqual(refreshed.lineup_artists, ["Artist A", "Artist B", "Artist C"])
        self.assertEqual(fetch.call_args.args[0]["p"], "1")

    def test_search_concerts_falls_back_when_festival_is_not_a_tour_name(self) -> None:
        payload = {
            "setlist": [{
                "id": "hangout-event",
                "eventDate": "18-05-2014",
                "artist": {"name": "OutKast"},
                "venue": {"id": "hangout-venue", "name": "The Hangout", "city": {"name": "Gulf Shores", "country": {"code": "US"}}},
                "sets": {"set": [{"song": [{"name": "Hey Ya!"}]}]},
            }]
        }
        with patch.object(self.client, "_fetch_setlists_raw", side_effect=[None, payload]) as fetch:
            results = self.client.search_concerts(
                event_date="2014-05-18",
                venue_name="The Hangout",
                city_name="Gulf Shores",
                tour_name="Hangout Music Fest",
                max_pages=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].headliner_artist, "OutKast")
        self.assertEqual(fetch.call_args_list[0].args[0]["tourName"], "Hangout Music Fest")
        self.assertNotIn("tourName", fetch.call_args_list[1].args[0])

    def test_search_concerts_groups_festival_artists_into_one_show(self) -> None:
        payload = {
            "setlist": [
                {
                    "id": "hangout-headliner",
                    "eventDate": "18-05-2014",
                    "artist": {"name": "OutKast"},
                    "venue": {"id": "hangout-venue", "name": "The Hangout", "city": {"name": "Gulf Shores", "country": {"code": "US"}}},
                    "sets": {"set": [{"song": [{"name": "Hey Ya!"}]}]},
                },
                {
                    "id": "hangout-support-a",
                    "eventDate": "18-05-2014",
                    "artist": {"name": "The Killers"},
                    "venue": {"id": "hangout-venue", "name": "The Hangout", "city": {"name": "Gulf Shores", "country": {"code": "US"}}},
                    "sets": {"set": [{"song": [{"name": "Mr. Brightside"}]}]},
                },
                {
                    "id": "hangout-support-b",
                    "eventDate": "18-05-2014",
                    "artist": {"name": "The Black Keys"},
                    "venue": {"id": "hangout-venue", "name": "The Hangout", "city": {"name": "Gulf Shores", "country": {"code": "US"}}},
                    "sets": {"set": [{"song": [{"name": "Gold on the Ceiling"}]}]},
                },
            ]
        }
        with patch.object(self.client, "_fetch_setlists_raw", side_effect=[None, payload]) as fetch:
            results = self.client.search_concerts(
                event_date="2014-05-18",
                venue_name="The Hangout",
                city_name="Gulf Shores",
                tour_name="Hangout Music Fest",
                max_pages=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].setlist_fm_event_id, "hangout-headliner")
        self.assertEqual(results[0].tour_name, "Hangout Music Fest")
        self.assertEqual(results[0].lineup_artists, ["OutKast", "The Killers", "The Black Keys"])
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
