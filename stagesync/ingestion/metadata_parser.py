"""Container and EXIF Metadata Parser for StageSync.

Executes ffprobe subprocess to inspect MP4/MOV container streams and format tags.
Extracts ISO 6709 geographic coordinates, timestamps, dimensions, and computes
concert candidate probability scores.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, Optional, Tuple
from dateutil import parser as dt_parser
from pydantic import BaseModel, Field

from stagesync.security import validate_executable

logger = logging.getLogger(__name__)

# ISO 6709 Regex: Latitude [+-]DD(.DDDD)?, Longitude [+-]DDD(.DDDD)?, optional Altitude [+-]AAA(.AAA)?, trailing slash
ISO6709_PATTERN = re.compile(
    r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:([+-]\d+(?:\.\d+)?))?\/?"
)


def parse_iso6709(raw: str) -> Optional[Tuple[float, float, Optional[float]]]:
    """Parse ISO 6709 coordinate string into (latitude, longitude, altitude).

    Supports formats like:
      - "+37.7749-122.4194+012.000/"
      - "+39.6653-105.2058/"
      - "-33.8688+151.2093/"
      - "+37.7749-122.4194"
    """
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    match = ISO6709_PATTERN.match(cleaned)
    if not match:
        return None

    try:
        lat_str, lon_str, alt_str = match.groups()
        lat = float(lat_str)
        lon = float(lon_str)
        alt = float(alt_str) if alt_str is not None else None

        # Validate range
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon, alt
    except (ValueError, TypeError) as err:
        logger.debug("Failed converting ISO 6709 coordinates '%s': %s", raw, err)
    return None


class ParsedVideoMetadata(BaseModel):
    """Normalized metadata extracted from container."""

    filename: str
    file_path: str
    file_hash_sha256: str
    mime_type: str = "video/mp4"
    file_size_bytes: int
    duration_seconds: float = 0.0
    width: Optional[int] = None
    height: Optional[int] = None
    has_audio: bool = False
    recorded_at_utc: Optional[str] = None
    recorded_at_local: Optional[str] = None
    timezone_offset_minutes: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_meters: Optional[float] = None
    device_make: Optional[str] = None
    device_model: Optional[str] = None
    is_concert_candidate: bool = False
    concert_confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_tags: Dict[str, Any] = Field(default_factory=dict)


def compute_file_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute cryptographic SHA-256 digest of file."""
    sha = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(chunk_size):
            sha.update(chunk)
    return sha.hexdigest()


class MetadataParser:
    """Subprocess ffprobe wrapper for deep media container parsing."""

    def __init__(self, ffprobe_bin: str = "ffprobe") -> None:
        self.ffprobe_bin = validate_executable(ffprobe_bin)

    def probe_file(self, file_path: Union[str, Path]) -> ParsedVideoMetadata:
        """Execute ffprobe and parse all container stream and tag metadata."""
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Video file not found at: {path}")

        file_size = path.stat().st_size
        file_hash = compute_file_sha256(path)

        cmd = [
            self.ffprobe_bin,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]

        try:
            res = subprocess.run(cmd, shell=False, capture_output=True, text=True, check=True)
            probe_data = json.loads(res.stdout)
        except subprocess.CalledProcessError as e:
            logger.error("ffprobe failed on %s: %s", path, e.stderr)
            raise RuntimeError(f"ffprobe extraction failed: {e.stderr}") from e
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON from ffprobe output: %s", e)
            raise RuntimeError(f"ffprobe output parsing failed: {e}") from e

        return self.parse_probe_dict(probe_data, path, file_size, file_hash)

    def parse_probe_dict(
        self,
        probe_data: Dict[str, Any],
        path: Path,
        file_size: int,
        file_hash: str,
    ) -> ParsedVideoMetadata:
        """Parse dictionary returned from ffprobe JSON output."""
        fmt = probe_data.get("format", {})
        streams = probe_data.get("streams", [])
        tags = fmt.get("tags", {})

        # Merge tags from first stream if present
        for st in streams:
            st_tags = st.get("tags", {})
            for k, v in st_tags.items():
                if k not in tags:
                    tags[k] = v

        # Duration
        duration_s = 0.0
        if "duration" in fmt:
            try:
                duration_s = float(fmt["duration"])
            except (ValueError, TypeError):
                duration_s = 0.0

        # Video dimensions and audio stream detection
        width: Optional[int] = None
        height: Optional[int] = None
        has_audio = False

        for st in streams:
            codec_type = st.get("codec_type")
            if codec_type == "video" and width is None:
                width = st.get("width")
                height = st.get("height")
                if duration_s == 0.0 and "duration" in st:
                    with contextlib.suppress(Exception):
                        duration_s = float(st["duration"])
            elif codec_type == "audio":
                has_audio = True

        # Extract coordinates
        latitude: Optional[float] = None
        longitude: Optional[float] = None
        altitude: Optional[float] = None

        iso_keys = [
            "com.apple.quicktime.location.ISO6709",
            "location",
            "location-eng",
            "ISO6709",
            "com.apple.quicktime.location.name",
        ]
        for key in iso_keys:
            raw_loc = tags.get(key)
            if raw_loc:
                coords = parse_iso6709(str(raw_loc))
                if coords:
                    latitude, longitude, altitude = coords
                    break

        # Fallback to separate lat/long tags if present
        if latitude is None:
            raw_lat = tags.get("latitude") or tags.get("GPSLatitude")
            raw_lon = tags.get("longitude") or tags.get("GPSLongitude")
            if raw_lat and raw_lon:
                with contextlib.suppress(Exception):
                    latitude = float(raw_lat)
                    longitude = float(raw_lon)

        # Timestamps
        recorded_at_utc: Optional[str] = None
        recorded_at_local: Optional[str] = None
        tz_offset_minutes: Optional[int] = None

        time_keys = [
            "com.apple.quicktime.creationdate",
            "creation_time",
            "date",
            "date-eng",
        ]
        for tkey in time_keys:
            raw_time = tags.get(tkey)
            if raw_time:
                try:
                    dt = dt_parser.parse(str(raw_time))
                    if dt.tzinfo is not None:
                        # Has explicit timezone offset
                        utc_dt = dt.astimezone(datetime.timezone.utc)
                        recorded_at_utc = utc_dt.isoformat()
                        recorded_at_local = dt.strftime("%Y-%m-%dT%H:%M:%S")
                        tz_offset_minutes = int(dt.utcoffset().total_seconds() / 60)
                    else:
                        # Naive timestamp, assume UTC
                        utc_dt = dt.replace(tzinfo=datetime.timezone.utc)
                        recorded_at_utc = utc_dt.isoformat()
                        recorded_at_local = dt.strftime("%Y-%m-%dT%H:%M:%S")
                        tz_offset_minutes = 0
                    break
                except Exception as e:
                    logger.debug("Failed to parse time tag %s='%s': %s", tkey, raw_time, e)

        # Device make and model
        device_make = (
            tags.get("com.apple.quicktime.make")
            or tags.get("make")
            or tags.get("android.manufacturer")
        )
        device_model = (
            tags.get("com.apple.quicktime.model")
            or tags.get("model")
            or tags.get("android.model")
        )

        # Mime type check
        mime_type = "video/mp4"
        if path.suffix.lower() == ".mov":
            mime_type = "video/quicktime"

        # Heuristic scoring for live concert candidate
        is_candidate, score = self.evaluate_concert_candidate(
            duration_s=duration_s,
            recorded_at_local=recorded_at_local,
            has_audio=has_audio,
            has_location=latitude is not None and longitude is not None,
        )

        return ParsedVideoMetadata(
            filename=path.name,
            file_path=str(path),
            file_hash_sha256=file_hash,
            mime_type=mime_type,
            file_size_bytes=file_size,
            duration_seconds=round(duration_s, 2),
            width=width,
            height=height,
            has_audio=has_audio,
            recorded_at_utc=recorded_at_utc,
            recorded_at_local=recorded_at_local,
            timezone_offset_minutes=tz_offset_minutes,
            latitude=latitude,
            longitude=longitude,
            altitude_meters=altitude,
            device_make=str(device_make) if device_make else None,
            device_model=str(device_model) if device_model else None,
            is_concert_candidate=is_candidate,
            concert_confidence_score=score,
            raw_tags=tags,
        )

    def evaluate_concert_candidate(
        self,
        duration_s: float,
        recorded_at_local: Optional[str],
        has_audio: bool,
        has_location: bool,
    ) -> Tuple[bool, float]:
        """Compute candidate probability score [0.0 - 1.0] based on PRD heuristics.

        Rules:
        - Must have audio (music/vocals)
        - Duration: 10s to 900s (concert snippets or full track clips)
        - Time of day: 17:00 to 03:00 (prime evening live music hours)
        - Coordinates: presence increases confidence
        """
        if not has_audio:
            return False, 0.0

        if duration_s < 8.0 or duration_s > 1800.0:
            return False, 0.05

        score = 0.40  # Base candidate score for having valid video + audio duration

        # Duration sweet spot: 20s to 360s (ideal song or solo clip)
        if 20.0 <= duration_s <= 360.0:
            score += 0.20
        elif 10.0 <= duration_s < 20.0 or 360.0 < duration_s <= 900.0:
            score += 0.10

        # Evening / Night time window check
        if recorded_at_local:
            try:
                hour = dt_parser.parse(recorded_at_local).hour
                if 18 <= hour <= 23 or 0 <= hour <= 3:
                    score += 0.25  # Prime concert hours
                elif 16 <= hour < 18:
                    score += 0.10  # Festival / early openers
            except Exception as err:
                logger.debug("Could not parse recorded_at_local '%s' for hour calculation: %s", recorded_at_local, err)

        # GPS location known
        if has_location:
            score += 0.15

        final_score = min(1.0, round(score, 2))
        is_candidate = final_score >= 0.50
        return is_candidate, final_score
