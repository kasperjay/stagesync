"""Google Drive API Client for Concert Video Discovery.

Searches, catalogs, and downloads original concert video files from Google Drive
using files.list and files.get with alt=media. Preserves video containers and QuickTime atoms.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import requests

from stagesync.config import get_settings
from stagesync.security import enforce_file_permissions, secure_save_json

logger = logging.getLogger(__name__)

GOOGLE_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

VALID_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".avi",
    ".3gp",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
    ".m2ts",
    ".mts",
}

DISALLOWED_EXTENSIONS = {
    ".d.mts",
    ".tfx",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".cjs",
    ".json",
    ".xml",
    ".txt",
    ".md",
    ".py",
    ".css",
    ".html",
    ".zip",
    ".tar",
    ".gz",
}


def is_likely_video_file(
    filename: str,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    min_size_bytes: int = 1000,
) -> bool:
    """Validate that a candidate is a genuine video file and not code or audio presets.

    Google Drive frequently misclassifies TypeScript ES Module files (.mts / .d.mts)
    as video/mp2t (MPEG-2 Transport Stream) and DAW track preset files (.tfx)
    as video/mp4v-es.
    """
    if not filename:
        return False

    lower_name = filename.lower().strip()

    # Reject known non-video compound extensions (e.g., index.d.mts)
    for bad_ext in (
        ".d.mts",
        ".tfx",
        ".tsx",
        ".jsx",
        ".ts",
        ".js",
        ".mjs",
        ".cjs",
        ".json",
        ".xml",
        ".txt",
        ".md",
        ".py",
        ".css",
        ".html",
        ".zip",
        ".tar",
        ".gz",
        ".pdf",
        ".doc",
        ".docx",
    ):
        if lower_name.endswith(bad_ext):
            return False

    suffix = Path(lower_name).suffix

    # Standard video container extensions (ensure not corrupted tiny file)
    if suffix in VALID_VIDEO_EXTENSIONS:
        if size_bytes is not None and size_bytes < min_size_bytes:
            return False
        return True

    # If MIME type is video/
    if mime_type:
        lower_mime = mime_type.lower()
        if lower_mime.startswith("video/"):
            # Exclude known false-positive MIME types
            if lower_mime in ("video/mp4v-es", "video/mp2t", "video/vnd.dlna.mpeg-tts"):
                return False
            if suffix in DISALLOWED_EXTENSIONS:
                return False
            return True

    return False


def extract_folder_id(raw_id_or_url: Optional[str]) -> Optional[str]:
    """Extract clean Google Drive folder ID from raw ID string or browser URL."""
    if not raw_id_or_url:
        return None
    val = raw_id_or_url.strip()
    if "drive.google.com" in val:
        import re
        m = re.search(r"folders/([a-zA-Z0-9_-]+)", val)
        if m:
            return m.group(1)
        m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", val)
        if m:
            return m.group(1)
    val = val.strip("/'\" ")
    return val if val else None


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


class GoogleDriveClient:
    """Client for Google Drive API v3 video discovery and downloading."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        token_path: Optional[Path] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        settings = get_settings()
        self.credentials_path = credentials_path or settings.google_drive_credentials_path
        self.token_path = token_path or settings.google_drive_token_path
        self.client_id = client_id or settings.google_drive_client_id
        self.client_secret = client_secret or settings.google_drive_client_secret
        self.redirect_uri = settings.google_drive_redirect_uri

        # Auto-discover client credentials if not already populated
        self._load_credentials_from_file()

    def _load_credentials_from_file(self) -> None:
        """Attempt to read client_id and client_secret from credential JSON files."""
        alt_paths = [
            self.credentials_path,
            Path("~/.config/stagesync/google_drive_credentials.json").expanduser(),
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
                    if self.client_id and self.client_secret:
                        break
                except Exception as e:
                    logger.debug("Could not read credentials from %s: %s", p, e)

    def is_authenticated(self) -> bool:
        """Check if active valid or refreshable Drive tokens are saved."""
        token_data = self._load_token_data()
        return bool(token_data and (token_data.get("access_token") or token_data.get("refresh_token")))

    def _load_token_data(self) -> Optional[Dict[str, Any]]:
        """Load stored OAuth tokens from disk with 0600 permission check."""
        candidate_paths = [
            self.token_path,
            Path("~/.config/stagesync/google_drive_token.json").expanduser(),
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
                    logger.warning("Error loading Drive token data from %s: %s", p, e)
        return None

    def _save_token_data(self, data: Dict[str, Any]) -> None:
        """Persist OAuth tokens to disk with atomic 0600 permissions."""
        secure_save_json(self.token_path, data)

    def get_authorization_url(self, redirect_uri: Optional[str] = None, state: str = "stagesync_drive") -> str:
        """Generate browser authorization URL for OAuth 2.0 PKCE/Web flow."""
        if not self.client_id:
            raise ValueError("Google Drive Client ID is not configured.")
        r_uri = redirect_uri or self.redirect_uri
        params = {
            "client_id": self.client_id,
            "redirect_uri": r_uri,
            "response_type": "code",
            "scope": DRIVE_READONLY_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        req = requests.Request("GET", GOOGLE_AUTH_URL, params=params).prepare()
        return req.url

    def exchange_code(self, code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        if not self.client_id or not self.client_secret:
            raise ValueError("Client ID and Client Secret are required for Drive token exchange.")
        r_uri = redirect_uri or self.redirect_uri
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": r_uri,
            "grant_type": "authorization_code",
        }
        res = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=15)
        res.raise_for_status()
        token_data = res.json()
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        token_data["created_at"] = now_dt.isoformat()
        expires_in = token_data.get("expires_in", 3600)
        token_data["expires_at"] = now_dt.timestamp() + expires_in
        if self.client_id:
            token_data["client_id"] = self.client_id
        if self.client_secret:
            token_data["client_secret"] = self.client_secret
        self._save_token_data(token_data)
        return token_data

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Obtain a valid access token, refreshing if necessary or forced."""
        token_data = self._load_token_data()
        if not token_data:
            raise PermissionError("Google Drive is not authenticated. Run 'stagesync auth google-drive'.")

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
                    expires_at = token_data.get("expires_at")
                    if expires_at is not None:
                        try:
                            now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
                            if now_ts > (float(expires_at) - 300):
                                is_expired = True
                        except (ValueError, TypeError):
                            is_expired = True
                    else:
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
                            is_expired = True

                if is_expired:
                    try:
                        access_token = self._refresh_token(refresh_token)
                    except Exception as e:
                        logger.warning("Failed to refresh Google Drive access token: %s", e)
                        if not access_token:
                            raise

        if not access_token:
            raise PermissionError("No valid access token available for Google Drive.")
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

    def get_subfolder_ids(self, parent_folder_id: str, max_depth: int = 2) -> List[str]:
        """Fetch all subfolder IDs under parent folder up to max_depth."""
        clean_parent = extract_folder_id(parent_folder_id)
        if not clean_parent:
            return []

        all_folders: List[str] = []
        queue = [clean_parent]
        current_depth = 0

        while queue and current_depth < max_depth:
            current_depth += 1
            next_queue = []
            for pid in queue:
                try:
                    access_token = self.get_access_token()
                    headers = {"Authorization": f"Bearer {access_token}"}
                    params = {
                        "q": f"mimeType = 'application/vnd.google-apps.folder' and '{pid}' in parents and trashed = false",
                        "pageSize": 50,
                        "fields": "files(id, name)",
                        "supportsAllDrives": "true",
                        "includeItemsFromAllDrives": "true",
                    }
                    res = requests.get(f"{GOOGLE_DRIVE_API_BASE}/files", headers=headers, params=params, timeout=15)
                    if res.status_code == 200:
                        child_ids = [f["id"] for f in res.json().get("files", []) if f.get("id")]
                        all_folders.extend(child_ids)
                        next_queue.extend(child_ids)
                except Exception as e:
                    logger.debug("Failed checking subfolders for %s: %s", pid, e)
            queue = next_queue

        return all_folders

    def list_videos(
        self,
        start_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        end_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        days: Optional[int] = None,
        folder_id: Optional[str] = None,
        page_size: int = 50,
        page_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Search Google Drive for video files matching date range and optional folder."""
        access_token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        query_parts: List[str] = ["trashed = false"]

        # Scope search to video MIME types or common video container filenames
        video_clause = "(mimeType contains 'video/' or name contains '.mp4' or name contains '.mov' or name contains '.m4v' or name contains '.mkv')"
        query_parts.append(video_clause)

        clean_folder = extract_folder_id(folder_id)
        if clean_folder:
            # Query parent folder and subfolders
            subfolders = self.get_subfolder_ids(clean_folder)
            target_folders = [clean_folder] + subfolders
            if len(target_folders) == 1:
                query_parts.append(f"'{clean_folder}' in parents")
            else:
                parent_clauses = " or ".join(f"'{fid}' in parents" for fid in target_folders[:30])
                query_parts.append(f"({parent_clauses})")

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
            start_iso = f"{parsed_start.isoformat()}T00:00:00Z"
            end_iso = f"{parsed_end.isoformat()}T23:59:59Z"
            query_parts.append(f"createdTime >= '{start_iso}'")
            query_parts.append(f"createdTime <= '{end_iso}'")

        full_query = " and ".join(query_parts)
        logger.info("Executing Google Drive search query: %s", full_query)

        params: Dict[str, Any] = {
            "q": full_query,
            "pageSize": min(page_size, 100),
            "fields": "nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, videoMediaMetadata, imageMediaMetadata)",
            "orderBy": "createdTime desc",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        url = f"{GOOGLE_DRIVE_API_BASE}/files"
        res = requests.get(url, headers=headers, params=params, timeout=25)
        if res.status_code == 401:
            logger.info("Drive access token expired, refreshing...")
            try:
                access_token = self.get_access_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {access_token}"
                res = requests.get(url, headers=headers, params=params, timeout=25)
            except Exception as e:
                logger.warning("Drive token refresh failed: %s", e)

        res.raise_for_status()
        data = res.json()
        raw_items = data.get("files", [])
        filtered_items = [
            it
            for it in raw_items
            if is_likely_video_file(
                filename=it.get("name", ""),
                mime_type=it.get("mimeType"),
                size_bytes=int(it.get("size", 0)) if it.get("size") else None,
            )
        ]
        logger.info(
            "Drive query returned %d raw file(s), %d verified video candidate(s)",
            len(raw_items),
            len(filtered_items),
        )
        next_token = data.get("nextPageToken")
        return filtered_items, next_token

    def download_video(
        self,
        file_id: str,
        output_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Path, str]:
        """Download video binary from Google Drive preserving QuickTime atoms.

        Returns (output_path, sha256_hash).
        """
        download_url = f"{GOOGLE_DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        access_token = self.get_access_token()
        headers = {"Authorization": f"Bearer {access_token}"}

        output_path.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()

        resp = requests.get(download_url, headers=headers, stream=True, timeout=60)
        if resp.status_code == 401:
            logger.info("Received 401 downloading Drive video. Attempting token refresh...")
            try:
                access_token = self.get_access_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {access_token}"
                resp = requests.get(download_url, headers=headers, stream=True, timeout=60)
            except Exception as e:
                logger.warning("Drive token refresh retry failed during download: %s", e)

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
