"""Spatial-Temporal Neighbor Correlator for StageSync.

Solves the missing GPS problem on iOS/iPhone video recordings:
Videos recorded on iPhone/iOS frequently do not embed GPS coordinates in QuickTime
atoms or EXIF. However, still photos (JPEG, HEIC, DNG) or Takeout JSON sidecars
taken with the same device around the same time retain precision GPS coordinates.

This module indexes neighbor photos in the same local directory or Google Drive
folder, calculates temporal proximity, and inherits location metadata to videos.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
import logging
from pathlib import Path
import struct
from typing import Any, Dict, List, Optional, Tuple, Union
from dateutil import parser as dt_parser

logger = logging.getLogger(__name__)

# Valid still photo extensions to check for EXIF GPS
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".png", ".dng", ".tiff", ".tif"}


@dataclass
class PhotoLocationSample:
    """Represents a spatial-temporal coordinate sample extracted from a neighbor photo."""

    filename: str
    latitude: float
    longitude: float
    altitude_meters: Optional[float] = None
    timestamp_utc: Optional[datetime.datetime] = None
    device_make: Optional[str] = None
    device_model: Optional[str] = None
    source_reference: str = ""


@dataclass
class CorrelatedLocation:
    """Result of interpolating video location from the closest neighbor photo."""

    latitude: float
    longitude: float
    altitude_meters: Optional[float] = None
    source_reference: str = ""
    time_delta_seconds: float = 0.0
    photo_timestamp: Optional[str] = None
    device_make: Optional[str] = None
    device_model: Optional[str] = None


class SpatialTemporalCorrelator:
    """Indexes neighbor photos and correlates missing video GPS coordinates."""

    def __init__(self, max_window_hours: float = 6.0) -> None:
        self.max_window_seconds = max_window_hours * 3600.0

    # -------------------------------------------------------------------------
    # Pure-Python EXIF Parsing (Zero External C Dependencies)
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_rational(data: bytes, offset: int, endian_prefix: str) -> float:
        """Read 8-byte unsigned rational (numerator, denominator)."""
        if offset + 8 > len(data):
            return 0.0
        num, den = struct.unpack(f"{endian_prefix}II", data[offset : offset + 8])
        return float(num) / float(den) if den != 0 else 0.0

    @staticmethod
    def _parse_dms_coordinate(
        data: bytes, val_offset: bytes, endian_prefix: str, fmt_long: str
    ) -> float:
        """Parse 3-part degrees/minutes/seconds rational array into decimal degrees."""
        offset = struct.unpack(fmt_long, val_offset)[0]
        deg = SpatialTemporalCorrelator._parse_rational(data, offset, endian_prefix)
        minutes = SpatialTemporalCorrelator._parse_rational(data, offset + 8, endian_prefix)
        seconds = SpatialTemporalCorrelator._parse_rational(data, offset + 16, endian_prefix)
        return deg + (minutes / 60.0) + (seconds / 3600.0)

    @staticmethod
    def parse_exif_from_bytes(raw_bytes: bytes) -> Optional[PhotoLocationSample]:
        """Extract GPS coordinates, timestamp, and device model from raw JPEG/TIFF bytes."""
        idx = raw_bytes.find(b"Exif\x00\x00")
        if idx == -1:
            return None

        tiff = raw_bytes[idx + 6 :]
        if len(tiff) < 8:
            return None

        endian_tag = tiff[:2]
        if endian_tag == b"MM":
            endian_prefix = ">"
        elif endian_tag == b"II":
            endian_prefix = "<"
        else:
            return None

        fmt_short = f"{endian_prefix}H"
        fmt_long = f"{endian_prefix}I"

        first_ifd_offset = struct.unpack(fmt_long, tiff[4:8])[0]

        def read_ifd(offset: int) -> Dict[int, Tuple[int, int, bytes]]:
            if offset + 2 > len(tiff):
                return {}
            num_entries = struct.unpack(fmt_short, tiff[offset : offset + 2])[0]
            entries: Dict[int, Tuple[int, int, bytes]] = {}
            p = offset + 2
            for _ in range(num_entries):
                if p + 12 > len(tiff):
                    break
                tag, tag_type, count, val = struct.unpack(
                    f"{endian_prefix}HHI4s", tiff[p : p + 12]
                )
                entries[tag] = (tag_type, count, val)
                p += 12
            return entries

        ifd0 = read_ifd(first_ifd_offset)

        # Make & Model
        device_make: Optional[str] = None
        device_model: Optional[str] = None

        if 0x010F in ifd0:  # Make
            _, count, val = ifd0[0x010F]
            if count <= 4:
                device_make = val[:count].decode("ascii", errors="ignore").strip("\x00").strip()
            else:
                off = struct.unpack(fmt_long, val)[0]
                device_make = (
                    tiff[off : off + count].decode("ascii", errors="ignore").strip("\x00").strip()
                )

        if 0x0110 in ifd0:  # Model
            _, count, val = ifd0[0x0110]
            if count <= 4:
                device_model = val[:count].decode("ascii", errors="ignore").strip("\x00").strip()
            else:
                off = struct.unpack(fmt_long, val)[0]
                device_model = (
                    tiff[off : off + count].decode("ascii", errors="ignore").strip("\x00").strip()
                )

        # DateTime tag (0x0132) or Exif SubIFD (0x8769) -> DateTimeOriginal (0x9003)
        sample_time: Optional[datetime.datetime] = None
        time_str: Optional[str] = None

        if 0x8769 in ifd0:
            exif_sub_offset = struct.unpack(fmt_long, ifd0[0x8769][2])[0]
            exif_ifd = read_ifd(exif_sub_offset)
            if 0x9003 in exif_ifd:  # DateTimeOriginal
                _, count, val = exif_ifd[0x9003]
                off = struct.unpack(fmt_long, val)[0] if count > 4 else 0
                time_bytes = tiff[off : off + count] if count > 4 else val[:count]
                time_str = time_bytes.decode("ascii", errors="ignore").strip("\x00").strip()

        if not time_str and 0x0132 in ifd0:  # DateTime fallback
            _, count, val = ifd0[0x0132]
            off = struct.unpack(fmt_long, val)[0] if count > 4 else 0
            time_bytes = tiff[off : off + count] if count > 4 else val[:count]
            time_str = time_bytes.decode("ascii", errors="ignore").strip("\x00").strip()

        if time_str:
            try:
                import re
                match = re.search(
                    r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
                    time_str,
                )
                if match:
                    y, mo, d, H, M, S = match.groups()
                    sample_time = datetime.datetime(
                        int(y), int(mo), int(d), int(H), int(M), int(S), tzinfo=datetime.timezone.utc
                    )
                else:
                    parsed_dt = dt_parser.parse(time_str)
                    sample_time = (
                        parsed_dt.astimezone(datetime.timezone.utc)
                        if parsed_dt.tzinfo
                        else parsed_dt.replace(tzinfo=datetime.timezone.utc)
                    )
            except Exception:
                sample_time = None

        # GPS SubIFD (Tag 0x8825)
        if 0x8825 not in ifd0:
            return None

        gps_offset = struct.unpack(fmt_long, ifd0[0x8825][2])[0]
        gps_ifd = read_ifd(gps_offset)

        if 2 not in gps_ifd or 4 not in gps_ifd:
            return None

        try:
            lat = SpatialTemporalCorrelator._parse_dms_coordinate(
                tiff, gps_ifd[2][2], endian_prefix, fmt_long
            )
            lat_ref = gps_ifd.get(1, (0, 0, b"N"))[2][:1].decode("ascii", errors="ignore")
            if lat_ref.upper() == "S":
                lat = -lat

            lon = SpatialTemporalCorrelator._parse_dms_coordinate(
                tiff, gps_ifd[4][2], endian_prefix, fmt_long
            )
            lon_ref = gps_ifd.get(3, (0, 0, b"E"))[2][:1].decode("ascii", errors="ignore")
            if lon_ref.upper() == "W":
                lon = -lon

            alt: Optional[float] = None
            if 6 in gps_ifd:
                alt_off = struct.unpack(fmt_long, gps_ifd[6][2])[0]
                alt = SpatialTemporalCorrelator._parse_rational(tiff, alt_off, endian_prefix)
                alt_ref = gps_ifd.get(5, (0, 0, b"\x00"))[2][0]
                if alt_ref == 1:  # Below sea level
                    alt = -alt

            # Check valid coordinate range and avoid 0,0
            if abs(lat) < 0.0001 and abs(lon) < 0.0001:
                return None
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                return None

            return PhotoLocationSample(
                filename="",
                latitude=round(lat, 6),
                longitude=round(lon, 6),
                altitude_meters=round(alt, 1) if alt is not None else None,
                timestamp_utc=sample_time,
                device_make=device_make,
                device_model=device_model,
                source_reference="exif",
            )
        except Exception as e:
            logger.debug("Failed parsing GPS tags in EXIF: %s", e)
            return None

    # -------------------------------------------------------------------------
    # Sidecar JSON Parser (Google Takeout / Google Photos format)
    # -------------------------------------------------------------------------

    @staticmethod
    def parse_takeout_sidecar(json_path: Path) -> Optional[PhotoLocationSample]:
        """Extract GPS and timestamps from Google Takeout JSON sidecars."""
        if not json_path.exists():
            return None
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            geo = data.get("geoDataExif") or data.get("geoData") or {}
            lat = float(geo.get("latitude", 0.0))
            lon = float(geo.get("longitude", 0.0))
            alt = float(geo.get("altitude", 0.0)) if geo.get("altitude") is not None else None

            # Skip (0.0, 0.0) invalid coordinates
            if abs(lat) < 0.0001 and abs(lon) < 0.0001:
                return None

            # Timestamp
            photo_time = data.get("photoTakenTime") or data.get("creationTime") or {}
            ts_str = photo_time.get("timestamp")
            dt: Optional[datetime.datetime] = None
            if ts_str:
                dt = datetime.datetime.fromtimestamp(int(ts_str), tz=datetime.timezone.utc)

            title = data.get("title", json_path.stem)
            return PhotoLocationSample(
                filename=title,
                latitude=round(lat, 6),
                longitude=round(lon, 6),
                altitude_meters=round(alt, 1) if alt is not None else None,
                timestamp_utc=dt,
                source_reference=f"sidecar_json:{json_path.name}",
            )
        except Exception as e:
            logger.debug("Could not parse sidecar JSON %s: %s", json_path, e)
            return None

    # -------------------------------------------------------------------------
    # Local Directory Photo Scanning
    # -------------------------------------------------------------------------

    def scan_local_directory_for_photos(
        self, directory_path: Union[str, Path]
    ) -> List[PhotoLocationSample]:
        """Scan a local directory for still photos and Takeout sidecars with valid GPS."""
        dir_p = Path(directory_path).resolve()
        if not dir_p.exists() or not dir_p.is_dir():
            return []

        samples: List[PhotoLocationSample] = []

        # 1. Parse JSON sidecars first (very fast)
        for json_file in dir_p.glob("*.json"):
            sample = self.parse_takeout_sidecar(json_file)
            if sample:
                samples.append(sample)

        # 2. Parse actual photo files with EXIF
        for p in dir_p.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in PHOTO_EXTENSIONS:
                continue

            try:
                # Read first 128KB which covers all EXIF/APP1 blocks
                with open(p, "rb") as f:
                    raw = f.read(131072)
                sample = self.parse_exif_from_bytes(raw)
                if sample:
                    sample.filename = p.name
                    sample.source_reference = f"exif:{p.name}"

                    # If EXIF lacked timestamp, check file modification or creation
                    if sample.timestamp_utc is None:
                        mtime = p.stat().st_mtime
                        sample.timestamp_utc = datetime.datetime.fromtimestamp(
                            mtime, tz=datetime.timezone.utc
                        )

                    samples.append(sample)
            except Exception as e:
                logger.debug("Error extracting EXIF from %s: %s", p.name, e)

        logger.info(
            "Indexed %d GPS photo location sample(s) from directory %s",
            len(samples),
            dir_p.name,
        )
        return samples

    # -------------------------------------------------------------------------
    # Google Drive Folder Photo Indexing
    # -------------------------------------------------------------------------

    @staticmethod
    def index_drive_folder_photos(
        drive_client: Any,
        folder_id: str,
    ) -> List[PhotoLocationSample]:
        """Query Google Drive folder for still photos containing imageMediaMetadata GPS."""
        samples: List[PhotoLocationSample] = []
        try:
            token = drive_client.get_access_token()
            import requests

            url = "https://www.googleapis.com/drive/v3/files"
            params: Dict[str, Any] = {
                "q": f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false",
                "pageSize": 100,
                "fields": "nextPageToken, files(id, name, mimeType, imageMediaMetadata, createdTime)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            headers = {"Authorization": f"Bearer {token}"}

            page_token = None
            while True:
                if page_token:
                    params["pageToken"] = page_token
                resp = requests.get(url, headers=headers, params=params, timeout=25)
                if resp.status_code == 401:
                    token = drive_client.get_access_token(force_refresh=True)
                    headers["Authorization"] = f"Bearer {token}"
                    resp = requests.get(url, headers=headers, params=params, timeout=25)

                resp.raise_for_status()
                data = resp.json()
                for item in data.get("files", []):
                    img_meta = item.get("imageMediaMetadata") or {}
                    loc = img_meta.get("location") or {}
                    lat = loc.get("latitude")
                    lon = loc.get("longitude")
                    if lat is None or lon is None:
                        continue

                    lat_f = float(lat)
                    lon_f = float(lon)
                    if abs(lat_f) < 0.0001 and abs(lon_f) < 0.0001:
                        continue

                    # Timestamp parsing
                    photo_time_str = img_meta.get("time") or item.get("createdTime")
                    dt: Optional[datetime.datetime] = None
                    if photo_time_str:
                        try:
                            if ":" in photo_time_str[:4]:
                                clean_t = photo_time_str[:10].replace(":", "-") + photo_time_str[10:]
                            else:
                                clean_t = photo_time_str
                            parsed_dt = dt_parser.parse(clean_t)
                            dt = (
                                parsed_dt.astimezone(datetime.timezone.utc)
                                if parsed_dt.tzinfo
                                else parsed_dt.replace(tzinfo=datetime.timezone.utc)
                            )
                        except Exception:
                            dt = None

                    samples.append(
                        PhotoLocationSample(
                            filename=item.get("name", ""),
                            latitude=round(lat_f, 6),
                            longitude=round(lon_f, 6),
                            altitude_meters=float(loc.get("altitude")) if loc.get("altitude") else None,
                            timestamp_utc=dt,
                            device_make=img_meta.get("cameraMake"),
                            device_model=img_meta.get("cameraModel"),
                            source_reference=f"drive_image:{item.get('name')}",
                        )
                    )

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            logger.info("Found %d GPS-tagged photos in Drive folder %s", len(samples), folder_id)
        except Exception as e:
            logger.warning("Could not query Drive folder %s for photo locations: %s", folder_id, e)

        return samples

    # -------------------------------------------------------------------------
    # Spatial-Temporal Proximity Matcher
    # -------------------------------------------------------------------------

    def correlate_video_location(
        self,
        video_recorded_at: Optional[Union[str, datetime.datetime]],
        photo_samples: List[PhotoLocationSample],
        fallback_date_str: Optional[str] = None,
    ) -> Optional[CorrelatedLocation]:
        """Find the closest neighbor photo in time and inherit its GPS coordinates."""
        if not photo_samples:
            return None

        # Parse target video timestamp
        video_dt: Optional[datetime.datetime] = None
        if isinstance(video_recorded_at, datetime.datetime):
            video_dt = (
                video_recorded_at.astimezone(datetime.timezone.utc)
                if video_recorded_at.tzinfo
                else video_recorded_at.replace(tzinfo=datetime.timezone.utc)
            )
        elif isinstance(video_recorded_at, str) and video_recorded_at:
            try:
                parsed = dt_parser.parse(video_recorded_at)
                video_dt = (
                    parsed.astimezone(datetime.timezone.utc)
                    if parsed.tzinfo
                    else parsed.replace(tzinfo=datetime.timezone.utc)
                )
            except Exception:
                video_dt = None

        # If timestamp is known, compute exact delta seconds
        if video_dt:
            best_sample: Optional[PhotoLocationSample] = None
            min_delta_s = float("inf")

            for s in photo_samples:
                if s.timestamp_utc is None:
                    continue
                delta = abs((s.timestamp_utc - video_dt).total_seconds())
                if delta < min_delta_s:
                    min_delta_s = delta
                    best_sample = s

            # Accept match within window (default 6 hours = 21,600s)
            if best_sample and min_delta_s <= self.max_window_seconds:
                logger.info(
                    "Correlated location for video from neighbor photo %s (Time delta: %.1f minutes, Coords: %.4f, %.4f)",
                    best_sample.filename,
                    min_delta_s / 60.0,
                    best_sample.latitude,
                    best_sample.longitude,
                )
                return CorrelatedLocation(
                    latitude=best_sample.latitude,
                    longitude=best_sample.longitude,
                    altitude_meters=best_sample.altitude_meters,
                    source_reference=best_sample.source_reference or f"photo:{best_sample.filename}",
                    time_delta_seconds=round(min_delta_s, 1),
                    photo_timestamp=best_sample.timestamp_utc.isoformat()
                    if best_sample.timestamp_utc
                    else None,
                    device_make=best_sample.device_make,
                    device_model=best_sample.device_model,
                )

        # Fallback: Match by calendar date (e.g. "2016-12-15")
        target_date_str = None
        if video_dt:
            target_date_str = video_dt.strftime("%Y-%m-%d")
        elif fallback_date_str:
            target_date_str = fallback_date_str[:10]

        if target_date_str:
            same_day_samples = [
                s
                for s in photo_samples
                if s.timestamp_utc and s.timestamp_utc.strftime("%Y-%m-%d") == target_date_str
            ]
            if same_day_samples:
                # Pick the first or highest-quality sample on that day
                chosen = same_day_samples[0]
                logger.info(
                    "Correlated location by same-day match with photo %s on %s (Coords: %.4f, %.4f)",
                    chosen.filename,
                    target_date_str,
                    chosen.latitude,
                    chosen.longitude,
                )
                return CorrelatedLocation(
                    latitude=chosen.latitude,
                    longitude=chosen.longitude,
                    altitude_meters=chosen.altitude_meters,
                    source_reference=chosen.source_reference or f"same_day:{chosen.filename}",
                    time_delta_seconds=0.0,
                    photo_timestamp=chosen.timestamp_utc.isoformat()
                    if chosen.timestamp_utc
                    else None,
                    device_make=chosen.device_make,
                    device_model=chosen.device_model,
                )

        return None
