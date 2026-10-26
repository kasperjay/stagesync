"""Resumable Chunked YouTube Video Uploader.

Implements the Google YouTube Data API v3 Resumable Upload protocol using 8MB chunks,
network retry exponential backoff, progress tracking, and daily quota preservation guardrails.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, Optional, Union
from pydantic import BaseModel
import requests

from stagesync.config import get_settings
from stagesync.database.connection import Database, get_db
from stagesync.database.models import PrivacyStatus, YouTubeUpload
from stagesync.publisher.metadata_gen import FormattedYouTubeMetadata
from stagesync.publisher.youtube_auth import YouTubeAuth

logger = logging.getLogger(__name__)

YOUTUBE_RESUMABLE_INIT_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status,recordingDetails"
)
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB per YouTube specification


class UploadResult(BaseModel):
    """Result of YouTube upload execution."""

    success: bool
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    bytes_uploaded: int = 0
    total_bytes: int = 0
    error_message: Optional[str] = None


class ResumableUploader:
    """Manages 8MB chunked streaming uploads to YouTube."""

    def __init__(
        self,
        auth: Optional[YouTubeAuth] = None,
        db: Optional[Database] = None,
        max_daily_uploads: Optional[int] = None,
    ) -> None:
        self.auth = auth or YouTubeAuth()
        self.db = db or get_db()
        settings = get_settings()
        self.max_daily_uploads = (
            max_daily_uploads if max_daily_uploads is not None else settings.max_daily_uploads
        )

    def check_daily_quota(self) -> bool:
        """Check if daily quota is available (<= max_daily_uploads)."""
        uploaded_today = self.db.get_todays_upload_count()
        if uploaded_today >= self.max_daily_uploads:
            logger.warning(
                "Daily YouTube upload limit reached (%d/%d). Halting to preserve quota.",
                uploaded_today,
                self.max_daily_uploads,
            )
            return False
        return True

    def initiate_resumable_session(
        self,
        metadata: FormattedYouTubeMetadata,
        file_size: int,
        privacy: PrivacyStatus = PrivacyStatus.UNLISTED,
        mime_type: str = "video/mp4",
    ) -> str:
        """Initiate resumable upload session and acquire upload URI."""
        access_token = self.auth.get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(file_size),
        }

        body: Dict[str, Any] = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
                "categoryId": metadata.category_id,
            },
            "status": {
                "privacyStatus": privacy.value if isinstance(privacy, PrivacyStatus) else str(privacy),
                "selfDeclaredMadeForKids": False,
            },
        }

        recording_details: Dict[str, Any] = {}
        if metadata.recording_date:
            recording_details["recordingDate"] = metadata.recording_date
        if metadata.latitude is not None and metadata.longitude is not None:
            recording_details["location"] = {
                "latitude": metadata.latitude,
                "longitude": metadata.longitude,
            }
        if recording_details:
            body["recordingDetails"] = recording_details

        res = requests.post(
            YOUTUBE_RESUMABLE_INIT_URL,
            headers=headers,
            json=body,
            timeout=20,
        )
        res.raise_for_status()

        upload_url = res.headers.get("Location")
        if not upload_url:
            raise RuntimeError("YouTube API did not return a resumable Location header.")
        return upload_url

    def upload_file(
        self,
        video_id: str,
        file_path: Union[str, Path],
        metadata: FormattedYouTubeMetadata,
        privacy: PrivacyStatus = PrivacyStatus.UNLISTED,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        existing_resumable_uri: Optional[str] = None,
    ) -> UploadResult:
        """Upload video file in 8MB chunks with exponential backoff on interruption."""
        if not self.check_daily_quota():
            return UploadResult(
                success=False,
                error_message=f"Daily YouTube upload quota reached ({self.max_daily_uploads} videos/day).",
            )

        path = Path(file_path).resolve()
        if not path.exists():
            return UploadResult(success=False, error_message=f"File not found: {path}")

        file_size = path.stat().st_size
        mime_type = "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"

        # Acquire or reuse upload URI
        upload_uri = existing_resumable_uri
        if not upload_uri:
            try:
                upload_uri = self.initiate_resumable_session(
                    metadata=metadata,
                    file_size=file_size,
                    privacy=privacy,
                    mime_type=mime_type,
                )
            except Exception as e:
                logger.error("Failed to initiate YouTube resumable session: %s", e)
                return UploadResult(success=False, error_message=str(e), total_bytes=file_size)

        # Update database with resumable URI
        upload_record = self.db.get_youtube_upload_by_video_id(video_id)
        if upload_record:
            upload_record.upload_resumable_uri = upload_uri
            upload_record.upload_status = "UPLOADING"
            upload_record.total_bytes = file_size
            self.db.save_youtube_upload(upload_record)

        bytes_uploaded = 0

        # Upload loop
        with path.open("rb") as f:
            while bytes_uploaded < file_size:
                f.seek(bytes_uploaded)
                chunk = f.read(CHUNK_SIZE)
                chunk_len = len(chunk)
                start_byte = bytes_uploaded
                end_byte = bytes_uploaded + chunk_len - 1

                headers = {
                    "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                    "Content-Length": str(chunk_len),
                    "Content-Type": mime_type,
                }

                # Retry loop for chunk
                max_retries = 5
                retry_delay = 1.0
                chunk_success = False

                for attempt in range(max_retries):
                    try:
                        res = requests.put(
                            upload_uri,
                            headers=headers,
                            data=chunk,
                            timeout=60,
                        )
                        if res.status_code in (200, 201):
                            # Completed!
                            payload = res.json()
                            yt_vid_id = payload.get("id")
                            yt_url = f"https://youtu.be/{yt_vid_id}" if yt_vid_id else None

                            bytes_uploaded = file_size
                            if progress_callback:
                                progress_callback(file_size, file_size)

                            if upload_record:
                                upload_record.youtube_video_id = yt_vid_id
                                upload_record.youtube_url = yt_url
                                upload_record.upload_status = "COMPLETED"
                                upload_record.bytes_uploaded = file_size
                                upload_record.uploaded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                                self.db.save_youtube_upload(upload_record)

                            return UploadResult(
                                success=True,
                                youtube_video_id=yt_vid_id,
                                youtube_url=yt_url,
                                bytes_uploaded=file_size,
                                total_bytes=file_size,
                            )

                        elif res.status_code == 308:
                            # Resume Incomplete
                            range_header = res.headers.get("Range")
                            if range_header:
                                # e.g. "bytes=0-8388607"
                                last_byte = int(range_header.split("-")[1])
                                bytes_uploaded = last_byte + 1
                            else:
                                bytes_uploaded += chunk_len

                            if progress_callback:
                                progress_callback(bytes_uploaded, file_size)

                            if upload_record:
                                upload_record.bytes_uploaded = bytes_uploaded
                                self.db.save_youtube_upload(upload_record)

                            chunk_success = True
                            break

                        elif res.status_code in (500, 502, 503, 504):
                            logger.warning("YouTube 5xx error on chunk: %s. Retrying in %.1fs...", res.status_code, retry_delay)
                            time.sleep(retry_delay)
                            retry_delay *= 2
                        else:
                            res.raise_for_status()

                    except (requests.RequestException, ConnectionError) as e:
                        logger.warning("Network interruption on chunk upload: %s. Retrying in %.1fs...", e, retry_delay)
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        # Query status
                        recovered = self._query_upload_status(upload_uri, file_size)
                        if recovered is not None:
                            bytes_uploaded = recovered
                            chunk_success = True
                            break

                if not chunk_success and bytes_uploaded < file_size:
                    err = f"Failed to upload chunk starting at byte {start_byte} after {max_retries} retries."
                    if upload_record:
                        upload_record.upload_status = "FAILED"
                        upload_record.error_message = err
                        self.db.save_youtube_upload(upload_record)
                    return UploadResult(
                        success=False,
                        bytes_uploaded=bytes_uploaded,
                        total_bytes=file_size,
                        error_message=err,
                    )

        return UploadResult(
            success=False,
            bytes_uploaded=bytes_uploaded,
            total_bytes=file_size,
            error_message="Upload loop terminated unexpectedly.",
        )

    def _query_upload_status(self, upload_uri: str, total_bytes: int) -> Optional[int]:
        """Query resumable session for current uploaded offset."""
        headers = {"Content-Range": f"bytes */{total_bytes}"}
        try:
            res = requests.put(upload_uri, headers=headers, timeout=15)
            if res.status_code == 308:
                range_header = res.headers.get("Range")
                if range_header:
                    return int(range_header.split("-")[1]) + 1
            elif res.status_code in (200, 201):
                return total_bytes
        except Exception as e:
            logger.warning("Status query failed: %s", e)
        return None
