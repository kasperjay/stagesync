"""Google Photos Library API Client.

Handles OAuth 2.0 token storage, media item discovery with VIDEO filters,
and chunked downloading with '=dv' parameters preserving QuickTime atoms.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import requests
from stagesync.config import get_settings
from stagesync.security import enforce_file_permissions, secure_save_json

logger = logging.getLogger(__name__)

GOOGLE_PHOTOS_API_BASE = "https://photoslibrary.googleapis.com/v1"
GOOGLE_PHOTOS_PICKER_BASE = "https://photospicker.googleapis.com/v1"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
PHOTOS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly "
    "https://www.googleapis.com/auth/photoslibrary.readonly "
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"
)


def _parse_date(date_val: Optional[Union[datetime.date, datetime.datetime, str]]) -> Optional[datetime.date]:
    """Parse various date formats into standard datetime.date."""
    if not date_val:
        return None
    if isinstance(date_val, datetime.datetime):
        return date_val.date()
    if isinstance(date_val, datetime.date):
        return date_val
    if isinstance(date_val, str):
        clean_str = date_val.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.datetime.strptime(clean_str, fmt).date()
            except ValueError:
                continue
        try:
            from dateutil import parser
            return parser.parse(clean_str).date()
        except Exception:
            pass
    return None


class GooglePhotosClient:
    """Client for Google Photos Library API."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        token_path: Optional[Path] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        settings = get_settings()
        self.credentials_path = credentials_path or settings.google_photos_credentials_path
        self.token_path = token_path or settings.google_photos_token_path
        self.client_id = client_id or settings.google_photos_client_id
        self.client_secret = client_secret or settings.google_photos_client_secret

        # Auto-discover client credentials from file if not provided
        self._load_credentials_from_file()

    def _load_credentials_from_file(self) -> None:
        """Attempt to read client_id and client_secret from json credentials file."""
        alt_paths = [
            self.credentials_path,
            Path("~/.config/stagesync/google_photos_credentials.json").expanduser(),
            Path("~/.config/google-photos/credentials.json").expanduser(),
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
                    logger.debug("Could not read credentials from %s: %s", p, e)

    def is_authenticated(self) -> bool:
        """Check if active valid or refreshable tokens are saved."""
        token_data = self._load_token_data()
        return bool(token_data and (token_data.get("access_token") or token_data.get("refresh_token")))

    def _load_token_data(self) -> Optional[Dict[str, Any]]:
        """Load stored OAuth tokens from disk with 0600 permission check."""
        candidate_paths = [
            self.token_path,
            Path("~/.config/stagesync/google_photos_token.json").expanduser(),
            Path("~/.config/google-photos/credentials.json").expanduser(),
        ]
        for p in candidate_paths:
            if p and p.exists():
                enforce_file_permissions(p)
                try:
                    with p.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("access_token") or data.get("refresh_token"):
                        return data
                except Exception as e:
                    logger.warning("Error loading token data from %s: %s", p, e)
        return None

    def _save_token_data(self, data: Dict[str, Any]) -> None:
        """Persist OAuth tokens to disk with atomic 0600 permissions."""
        secure_save_json(self.token_path, data)

    def get_authorization_url(self, redirect_uri: str, state: str = "stagesync_auth") -> str:
        """Generate browser authorization URL for OAuth 2.0 PKCE/Web flow."""
        if not self.client_id:
            raise ValueError("Google Photos Client ID is not configured.")
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": PHOTOS_READONLY_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        req = requests.Request("GET", GOOGLE_AUTH_URL, params=params).prepare()
        return req.url

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        if not self.client_id or not self.client_secret:
            raise ValueError("Client ID and Client Secret are required for token exchange.")
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

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Obtain a valid access token, refreshing if necessary or forced."""
        token_data = self._load_token_data()
        if not token_data:
            raise PermissionError("Google Photos is not authenticated. Run 'stagesync auth google-photos'.")

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if refresh_token:
            if not self.client_id or not self.client_secret:
                self._load_credentials_from_file()

            if self.client_id and self.client_secret:
                is_expired = force_refresh
                if not access_token:
                    is_expired = True
                elif not force_refresh:
                    # Check expires_at timestamp if present
                    expires_at = token_data.get("expires_at")
                    if expires_at is not None:
                        try:
                            now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
                            if now_ts > (float(expires_at) - 300):
                                is_expired = True
                        except (ValueError, TypeError):
                            is_expired = True
                    else:
                        # Check created_at ISO string if present
                        created_at_str = token_data.get("created_at")
                        expires_in = token_data.get("expires_in", 3600)
                        if created_at_str:
                            try:
                                created_at = datetime.datetime.fromisoformat(created_at_str)
                                age = (datetime.datetime.now(datetime.timezone.utc) - created_at).total_seconds()
                                if age > (expires_in - 300):
                                    is_expired = True
                            except (ValueError, TypeError):
                                is_expired = True
                        else:
                            # Neither expires_at nor created_at is present; assume expired to be safe
                            is_expired = True

                if is_expired:
                    try:
                        access_token = self._refresh_token(refresh_token)
                    except Exception as e:
                        logger.warning("Failed to refresh Google Photos access token: %s", e)
                        if not access_token:
                            raise

        if not access_token:
            raise PermissionError("No valid access token available for Google Photos.")
        return access_token

    def _refresh_token(self, refresh_token: str) -> str:
        """Request new access token using refresh token."""
        if not self.client_id or not self.client_secret:
            self._load_credentials_from_file()
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        res = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=15)
        res.raise_for_status()
        fresh = res.json()
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        fresh["refresh_token"] = refresh_token
        fresh["created_at"] = now_dt.isoformat()
        expires_in = fresh.get("expires_in", 3600)
        fresh["expires_at"] = now_dt.timestamp() + expires_in
        if self.client_id:
            fresh["client_id"] = self.client_id
        if self.client_secret:
            fresh["client_secret"] = self.client_secret
        self._save_token_data(fresh)
        return fresh["access_token"]

    def list_videos(
        self,
        start_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        end_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        days: Optional[int] = None,
        album_id: Optional[str] = None,
        page_size: int = 50,
        page_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Query Google Photos for video items matching search criteria and date range."""
        access_token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        url = f"{GOOGLE_PHOTOS_API_BASE}/mediaItems:search"
        payload: Dict[str, Any] = {
            "pageSize": min(page_size, 100),
        }

        if page_token:
            payload["pageToken"] = page_token

        if album_id:
            payload["albumId"] = album_id
        else:
            filters: Dict[str, Any] = {
                "mediaTypeFilter": {
                    "mediaTypes": ["VIDEO"],
                }
            }

            parsed_start = _parse_date(start_date)
            parsed_end = _parse_date(end_date)

            if not parsed_start and days:
                now_date = datetime.datetime.now(datetime.timezone.utc).date()
                parsed_start = now_date - datetime.timedelta(days=days)
                if not parsed_end:
                    parsed_end = now_date
            elif parsed_start and not parsed_end:
                parsed_end = datetime.datetime.now(datetime.timezone.utc).date()

            if parsed_start and parsed_end:
                if parsed_start > parsed_end:
                    parsed_start, parsed_end = parsed_end, parsed_start
                filters["dateFilter"] = {
                    "ranges": [
                        {
                            "startDate": {
                                "year": parsed_start.year,
                                "month": parsed_start.month,
                                "day": parsed_start.day,
                            },
                            "endDate": {
                                "year": parsed_end.year,
                                "month": parsed_end.month,
                                "day": parsed_end.day,
                            },
                        }
                    ]
                }
            payload["filters"] = filters

        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 401:
            logger.info("Received 401 Unauthorized from Google Photos. Attempting token refresh...")
            try:
                access_token = self.get_access_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {access_token}"
                res = requests.post(url, headers=headers, json=payload, timeout=20)
            except Exception as e:
                logger.warning("Token refresh retry failed: %s", e)

        res.raise_for_status()
        data = res.json()

        items = data.get("mediaItems", [])
        next_page_token = data.get("nextPageToken")
        return items, next_page_token

    def download_video(
        self,
        base_url: str,
        output_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Path, str]:
        """Download original video binary using '=dv' parameter to preserve QuickTime atoms.

        Returns (file_path, sha256_hash).
        """
        download_url = f"{base_url}=dv"
        access_token = self.get_access_token()
        headers = {"Authorization": f"Bearer {access_token}"}

        output_path.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()

        resp = requests.get(download_url, headers=headers, stream=True, timeout=60)
        if resp.status_code == 401:
            logger.info("Received 401 downloading video. Attempting token refresh...")
            try:
                access_token = self.get_access_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {access_token}"
                resp = requests.get(download_url, headers=headers, stream=True, timeout=60)
            except Exception as e:
                logger.warning("Token refresh retry failed during download: %s", e)

        with resp:
            resp.raise_for_status()
            total_bytes = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with output_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        sha.update(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total_bytes)

        return output_path, sha.hexdigest()

    # -------------------------------------------------------------------------
    # Google Photos Picker API (Modern 2026 Interactive Ingestion)
    # -------------------------------------------------------------------------

    def create_picker_session(self) -> Dict[str, str]:
        """Create a new Google Photos Picker session.

        Returns a dictionary with 'id' and 'pickerUri'.
        """
        access_token = self.get_access_token()
        url = f"{GOOGLE_PHOTOS_PICKER_BASE}/sessions"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        res = requests.post(url, headers=headers, timeout=20)
        if res.status_code == 401:
            access_token = self.get_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {access_token}"
            res = requests.post(url, headers=headers, timeout=20)
        res.raise_for_status()
        data = res.json()
        return {"id": data["id"], "pickerUri": data["pickerUri"]}

    def poll_picker_session(self, session_id: str) -> bool:
        """Check if user has finished selecting photos/videos in the Picker session."""
        access_token = self.get_access_token()
        url = f"{GOOGLE_PHOTOS_PICKER_BASE}/sessions/{session_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code == 401:
            access_token = self.get_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {access_token}"
            res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        data = res.json()
        return bool(data.get("mediaItemsSet", False))

    def get_picker_items(self, session_id: str, page_size: int = 100) -> List[Dict[str, Any]]:
        """Retrieve the media items selected during the picker session."""
        access_token = self.get_access_token()
        url = f"{GOOGLE_PHOTOS_PICKER_BASE}/mediaItems"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"sessionId": session_id, "pageSize": min(page_size, 100)}
        res = requests.get(url, headers=headers, params=params, timeout=20)
        if res.status_code == 401:
            access_token = self.get_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {access_token}"
            res = requests.get(url, headers=headers, params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        return data.get("mediaItems", [])

    def delete_picker_session(self, session_id: str) -> None:
        """Clean up and delete a Picker session."""
        try:
            access_token = self.get_access_token()
            url = f"{GOOGLE_PHOTOS_PICKER_BASE}/sessions/{session_id}"
            headers = {"Authorization": f"Bearer {access_token}"}
            requests.delete(url, headers=headers, timeout=10)
        except Exception as e:
            logger.debug("Failed to delete picker session %s: %s", session_id, e)
