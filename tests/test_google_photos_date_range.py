"""Unit tests for Google Photos date range filtering."""

import datetime
import unittest
from unittest.mock import MagicMock, patch

from stagesync.ingestion.google_photos import GooglePhotosClient, _parse_date


class TestGooglePhotosDateRange(unittest.TestCase):
    """Test date parsing and date range filtering in GooglePhotosClient."""

    def test_parse_date_formats(self) -> None:
        """Verify _parse_date handles diverse string formats and objects."""
        d = datetime.date(2024, 8, 15)
        self.assertEqual(_parse_date(d), d)
        self.assertEqual(_parse_date(datetime.datetime(2024, 8, 15, 20, 0)), d)
        self.assertEqual(_parse_date("2024-08-15"), d)
        self.assertEqual(_parse_date("2024/08/15"), d)
        self.assertEqual(_parse_date("08/15/2024"), d)
        self.assertEqual(_parse_date("15-08-2024"), d)
        self.assertIsNone(_parse_date(None))
        self.assertIsNone(_parse_date(""))

    @patch("requests.post")
    def test_list_videos_with_explicit_date_range(self, mock_post: MagicMock) -> None:
        """Verify payload includes exact date range filters."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"mediaItems": []}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = GooglePhotosClient()
        with patch.object(client, "get_access_token", return_value="fake_token"):
            client.list_videos(
                start_date="2024-06-01",
                end_date="2024-06-30",
            )

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        date_filter = payload.get("filters", {}).get("dateFilter", {})
        ranges = date_filter.get("ranges", [])
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]["startDate"], {"year": 2024, "month": 6, "day": 1})
        self.assertEqual(ranges[0]["endDate"], {"year": 2024, "month": 6, "day": 30})

    @patch("requests.post")
    def test_list_videos_swaps_inverted_date_range(self, mock_post: MagicMock) -> None:
        """Verify client automatically fixes startDate > endDate."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"mediaItems": []}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = GooglePhotosClient()
        with patch.object(client, "get_access_token", return_value="fake_token"):
            client.list_videos(
                start_date="2024-08-20",
                end_date="2024-08-10",
            )

        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        ranges = payload.get("filters", {}).get("dateFilter", {}).get("ranges", [])
        self.assertEqual(ranges[0]["startDate"], {"year": 2024, "month": 8, "day": 10})
        self.assertEqual(ranges[0]["endDate"], {"year": 2024, "month": 8, "day": 20})

    @patch("requests.post")
    def test_list_videos_days_fallback(self, mock_post: MagicMock) -> None:
        """Verify client preserves days=N calculation when explicit dates omitted."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"mediaItems": []}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = GooglePhotosClient()
        with patch.object(client, "get_access_token", return_value="fake_token"):
            client.list_videos(days=14)

        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        ranges = payload.get("filters", {}).get("dateFilter", {}).get("ranges", [])
        self.assertEqual(len(ranges), 1)


if __name__ == "__main__":
    unittest.main()
