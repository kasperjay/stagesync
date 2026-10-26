"""YouTube Data API v3 OAuth2 Authentication Handler.

Manages credentials lookup (~/.config/stagesync/youtube_credentials.json),
token refresh flows, and secure token persistence (~/.config/stagesync/youtube_token.json).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
import requests
from stagesync.config import get_settings
from stagesync.security import enforce_file_permissions, secure_save_json

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeAuth:
    """OAuth 2.0 manager for YouTube Data API v3."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        token_path: Optional[Path] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        settings = get_settings()
        self.credentials_path = credentials_path or settings.youtube_credentials_path
        self.token_path = token_path or settings.youtube_token_path
        self.client_id = client_id or settings.youtube_client_id
        self.client_secret = client_secret or settings.youtube_client_secret

        self._load_credentials_from_file()

    def _load_credentials_from_file(self) -> None:
        """Read client credentials from filesystem if available."""
        alt_paths = [
            self.credentials_path,
            Path("~/.config/stagesync/youtube_credentials.json").expanduser(),
        ]
        for p in alt_paths:
            if p and p.exists():
                enforce_file_permissions(p)
                try:
                    with p.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    installed = data.get("installed") or data.get("web") or data
                    if not self.client_id:
                        self.client_id = installed.get("client_id")
                    if not self.client_secret:
                        self.client_secret = installed.get("client_secret")
                    break
                except Exception as e:
                    logger.debug("Could not read YouTube credentials from %s: %s", p, e)

    def is_authenticated(self) -> bool:
        """Check if active tokens exist."""
        data = self._load_token_data()
        return bool(data and (data.get("access_token") or data.get("refresh_token")))

    def _load_token_data(self) -> Optional[Dict[str, Any]]:
        """Load stored OAuth token from disk with 0600 permission enforcement."""
        if not self.token_path or not self.token_path.exists():
            return None
        enforce_file_permissions(self.token_path)
        try:
            with self.token_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Error loading YouTube token data: %s", e)
            return None

    def _save_token_data(self, data: Dict[str, Any]) -> None:
        """Save OAuth token to disk with atomic restrictive 0600 permissions."""
        secure_save_json(self.token_path, data)

    def get_authorization_url(self, redirect_uri: str, state: str = "stagesync_youtube") -> str:
        """Generate browser URL for YouTube upload consent."""
        if not self.client_id:
            raise ValueError("YouTube Client ID is not configured.")
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_UPLOAD_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        req = requests.Request("GET", GOOGLE_AUTH_URL, params=params).prepare()
        return req.url

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        if not self.client_id or not self.client_secret:
            raise ValueError("YouTube Client ID and Secret are required for token exchange.")
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        res = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=15)
        res.raise_for_status()
        token_data = res.json()
        token_data["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._save_token_data(token_data)
        return token_data

    def get_access_token(self) -> str:
        """Get valid access token, auto-refreshing if expired."""
        data = self._load_token_data()
        if not data:
            raise PermissionError("YouTube is not authenticated. Run 'stagesync auth youtube'.")

        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")

        if refresh_token and self.client_id and self.client_secret:
            created_at_str = data.get("created_at")
            expires_in = data.get("expires_in", 3600)
            is_expired = False
            if created_at_str:
                created_at = datetime.datetime.fromisoformat(created_at_str)
                age = (datetime.datetime.now(datetime.timezone.utc) - created_at).total_seconds()
                if age > (expires_in - 300):
                    is_expired = True
            if is_expired:
                access_token = self._refresh_token(refresh_token)

        if not access_token:
            raise PermissionError("No valid YouTube access token available.")
        return access_token

    def _refresh_token(self, refresh_token: str) -> str:
        """Request fresh access token using refresh token."""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        res = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=15)
        res.raise_for_status()
        fresh = res.json()
        fresh["refresh_token"] = refresh_token
        fresh["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._save_token_data(fresh)
        return fresh["access_token"]
