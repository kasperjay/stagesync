"""Unit tests for YouTubeAuth and GooglePhotosClient authentication and credential security."""

import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.ingestion.google_photos import GooglePhotosClient
from stagesync.publisher.youtube_auth import YouTubeAuth


class TestAuthAndCredentials(unittest.TestCase):
    """Test suite validating OAuth token management, 0600 permission checks, and refresh flows."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # YouTubeAuth Tests
    # -------------------------------------------------------------------------

    def test_youtube_auth_token_save_creates_0600(self) -> None:
        token_file = self.base_path / "yt_token.json"
        auth = YouTubeAuth(token_path=token_file, client_id="test_id", client_secret="test_secret")

        token_data = {
            "access_token": "mock_access_token",
            "refresh_token": "mock_refresh_token",
            "expires_in": 3600,
        }
        auth._save_token_data(token_data)

        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(auth.is_authenticated())

    def test_youtube_auth_tightens_permissive_credentials_on_read(self) -> None:
        cred_file = self.base_path / "yt_creds.json"
        cred_file.write_text(json.dumps({"installed": {"client_id": "cid_123", "client_secret": "csec_456"}}))
        os.chmod(cred_file, 0o644)

        token_file = self.base_path / "yt_tok.json"
        auth = YouTubeAuth(credentials_path=cred_file, token_path=token_file)

        # Loading credentials should have tightened cred_file to 0600
        self.assertEqual(cred_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(auth.client_id, "cid_123")
        self.assertEqual(auth.client_secret, "csec_456")

    def test_youtube_auth_url_generation(self) -> None:
        auth = YouTubeAuth(client_id="my_client_id_123", client_secret="secret")
        url = auth.get_authorization_url(redirect_uri="http://localhost:8090/cb")
        self.assertIn("accounts.google.com", url)
        self.assertIn("client_id=my_client_id_123", url)
        self.assertIn("youtube.upload", url)

    def test_youtube_auth_url_missing_client_id(self) -> None:
        auth = YouTubeAuth(client_id="", client_secret="")
        with self.assertRaises(ValueError) as ctx:
            auth.get_authorization_url("http://localhost:8090/cb")
        self.assertIn("YouTube Client ID is not configured", str(ctx.exception))

    def test_youtube_get_access_token_unauthenticated_raises(self) -> None:
        auth = YouTubeAuth(token_path=self.base_path / "missing_token.json")
        with self.assertRaises(PermissionError):
            auth.get_access_token()

    @patch("requests.post")
    def test_youtube_exchange_code(self, mock_post: MagicMock) -> None:
        token_file = self.base_path / "yt_token_exchange.json"
        auth = YouTubeAuth(
            token_path=token_file,
            client_id="my_id",
            client_secret="my_sec",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "fresh_access",
            "refresh_token": "fresh_refresh",
            "expires_in": 3600,
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        res = auth.exchange_code(code="auth_code_xyz", redirect_uri="http://localhost/cb")
        self.assertEqual(res["access_token"], "fresh_access")
        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    # -------------------------------------------------------------------------
    # GooglePhotosClient Tests
    # -------------------------------------------------------------------------

    def test_google_photos_token_save_creates_0600(self) -> None:
        token_file = self.base_path / "gp_token.json"
        client = GooglePhotosClient(token_path=token_file, client_id="test_id", client_secret="test_secret")

        token_data = {"access_token": "gp_access", "refresh_token": "gp_refresh"}
        client._save_token_data(token_data)

        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(client.is_authenticated())

    def test_google_photos_tightens_permissive_token_on_read(self) -> None:
        token_file = self.base_path / "gp_loose_token.json"
        token_file.write_text(json.dumps({"access_token": "existing_loose"}))
        os.chmod(token_file, 0o666)

        client = GooglePhotosClient(token_path=token_file)
        self.assertTrue(client.is_authenticated())
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_google_photos_auth_url_generation(self) -> None:
        client = GooglePhotosClient(client_id="gp_client_99", client_secret="secret")
        url = client.get_authorization_url(redirect_uri="http://localhost:8090/callback")
        self.assertIn("accounts.google.com", url)
        self.assertIn("client_id=gp_client_99", url)
        self.assertIn("photoslibrary.readonly", url)

    @patch("requests.get")
    def test_google_photos_download_video(self, mock_get: MagicMock) -> None:
        token_file = self.base_path / "gp_auth.json"
        token_file.write_text(json.dumps({"access_token": "tok_123"}))
        os.chmod(token_file, 0o600)

        client = GooglePhotosClient(token_path=token_file)

        mock_resp = MagicMock()
        mock_resp.headers = {"content-length": "12"}
        mock_resp.iter_content.return_value = [b"hello ", b"world!"]
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        target_file = self.base_path / "downloads" / "test_download.mp4"
        out_path, sha_hash = client.download_video(base_url="https://lh3.googleusercontent.com/test", output_path=target_file)

        self.assertEqual(out_path, target_file)
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_bytes(), b"hello world!")
        self.assertEqual(len(sha_hash), 64)  # SHA-256 hex string


if __name__ == "__main__":
    unittest.main()
