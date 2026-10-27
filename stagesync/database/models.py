"""Domain Models for StageSync.

Defines typed Pydantic models and Enums corresponding to the ERD.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field


def _current_timestamp_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class VideoStatus(str, Enum):
    """Lifecycle statuses for videos."""

    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    ANALYZING_METADATA = "ANALYZING_METADATA"
    CONCERT_MATCHED = "CONCERT_MATCHED"
    SONG_IDENTIFIED = "SONG_IDENTIFIED"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UPLOADING = "UPLOADING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class PrivacyStatus(str, Enum):
    """YouTube publication privacy settings."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class FingerprintService(str, Enum):
    """Audio identification service used."""

    SHAZAMIO = "shazamio"
    GEMINI_MULTIMODAL = "gemini_multimodal"
    AUDD = "audd"
    ACRCLOUD = "acrcloud"
    MANUAL = "manual"


class PipelineStep(str, Enum):
    """Discrete execution stages in the processing pipeline."""

    DISCOVERY = "DISCOVERY"
    DOWNLOAD = "DOWNLOAD"
    EXIF_EXTRACTION = "EXIF_EXTRACTION"
    SPATIAL_CORRELATION = "SPATIAL_CORRELATION"
    REVERSE_GEOCODE = "REVERSE_GEOCODE"
    CONCERT_SEARCH = "CONCERT_SEARCH"
    AUDIO_FINGERPRINT = "AUDIO_FINGERPRINT"
    LLM_DISAMBIGUATION = "LLM_DISAMBIGUATION"
    YOUTUBE_UPLOAD = "YOUTUBE_UPLOAD"


class StepStatus(str, Enum):
    """Audit status of a pipeline step execution."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Venue(BaseModel):
    """Geographic venue location model."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    formatted_address: Optional[str] = None
    city: str
    state_province: Optional[str] = None
    country_code: str
    latitude: float
    longitude: float
    osm_id: Optional[str] = None
    setlist_fm_venue_id: Optional[str] = None
    created_at: str = Field(default_factory=_current_timestamp_iso)
    updated_at: str = Field(default_factory=_current_timestamp_iso)


class Concert(BaseModel):
    """Concert event model correlating artists, setlists, and venues."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    venue_id: Optional[str] = None
    headliner_artist: str
    lineup_artists: List[str] = Field(default_factory=list)
    tour_name: Optional[str] = None
    event_date: str  # YYYY-MM-DD
    setlist_fm_event_id: Optional[str] = None
    setlist_data: Dict[str, Any] = Field(default_factory=dict)
    source_url: Optional[str] = None
    created_at: str = Field(default_factory=_current_timestamp_iso)
    updated_at: str = Field(default_factory=_current_timestamp_iso)


class Video(BaseModel):
    """Video asset model with spatial-temporal container metadata."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_photo_id: str
    filename: str
    file_path: Optional[str] = None
    file_hash_sha256: Optional[str] = None
    mime_type: str = "video/mp4"
    file_size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    recorded_at_utc: Optional[str] = None
    recorded_at_local: Optional[str] = None
    timezone_offset_minutes: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_meters: Optional[float] = None
    device_make: Optional[str] = None
    device_model: Optional[str] = None
    is_concert_candidate: bool = False
    concert_confidence_score: float = 0.0
    processing_status: VideoStatus = VideoStatus.DISCOVERED
    created_at: str = Field(default_factory=_current_timestamp_iso)
    updated_at: str = Field(default_factory=_current_timestamp_iso)


class Identification(BaseModel):
    """Acoustic and setlist-verified song identification record."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    video_id: str
    concert_id: Optional[str] = None
    detected_song_title: str
    detected_artist: str
    album_name: Optional[str] = None
    match_confidence: float = Field(ge=0.0, le=1.0)
    fingerprint_service: str = FingerprintService.SHAZAMIO.value
    setlist_verified: bool = False
    track_number_in_setlist: Optional[int] = None
    identified_lyrics_snippet: Optional[str] = None
    raw_service_payload: Dict[str, Any] = Field(default_factory=dict)
    status: str = "CONFIRMED"
    created_at: str = Field(default_factory=_current_timestamp_iso)
    updated_at: str = Field(default_factory=_current_timestamp_iso)


class YouTubeUpload(BaseModel):
    """YouTube publication record and chunked upload tracking."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    video_id: str
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    title: str
    description: str
    tags: List[str] = Field(default_factory=list)
    category_id: str = "10"  # Music
    privacy_status: PrivacyStatus = PrivacyStatus.UNLISTED
    upload_status: str = "DRAFT"
    bytes_uploaded: int = 0
    total_bytes: int = 0
    upload_resumable_uri: Optional[str] = None
    uploaded_at: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = Field(default_factory=_current_timestamp_iso)
    updated_at: str = Field(default_factory=_current_timestamp_iso)


class PipelineRun(BaseModel):
    """Audit log entry capturing individual pipeline step executions."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    video_id: str
    step_name: PipelineStep
    status: StepStatus = StepStatus.PENDING
    attempt_count: int = 1
    duration_ms: Optional[int] = None
    input_payload: Dict[str, Any] = Field(default_factory=dict)
    output_payload: Dict[str, Any] = Field(default_factory=dict)
    error_log: Optional[str] = None
    started_at: str = Field(default_factory=_current_timestamp_iso)
    completed_at: Optional[str] = None
