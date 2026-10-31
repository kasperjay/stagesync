"""StageSync Pipeline Orchestration Coordinator.

Connects Ingestion -> Metadata Parsing -> Geospatial Intelligence ->
Concert Matching -> Audio Fingerprinting -> Review Queue -> YouTube Distribution.
Tracks execution telemetry in the pipeline_runs audit table.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import logging
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from stagesync.config import Settings, get_settings
from stagesync.database.connection import Database, get_db
from stagesync.database.models import (
    Concert,
    Identification,
    PipelineRun,
    PipelineStep,
    PrivacyStatus,
    StepStatus,
    Venue,
    Video,
    VideoStatus,
    YouTubeUpload,
)
from stagesync.ingestion.google_drive import (
    GoogleDriveClient,
    extract_folder_id,
    is_likely_video_file,
)
from stagesync.ingestion.google_photos import GooglePhotosClient
from stagesync.ingestion.metadata_parser import MetadataParser, ParsedVideoMetadata
from stagesync.intelligence.ai_audio_judge import AIAudioJudge, AIAudioJudgeResult
from stagesync.intelligence.audio_extractor import AudioExtractor
from stagesync.intelligence.geocoder import ReverseGeocoder
from stagesync.intelligence.musicbrainz import MusicBrainzClient
from stagesync.intelligence.setlist_fm import MatchedConcert, SetlistFmClient
from stagesync.intelligence.shazam_matcher import AcousticMatchResult, ShazamMatcher
from stagesync.intelligence.spatial_correlator import (
    CorrelatedLocation,
    PhotoLocationSample,
    SpatialTemporalCorrelator,
)
from stagesync.intelligence.thumbnail_extractor import ThumbnailExtractor
from stagesync.intelligence.venue_search import VenueSearchEngine
from stagesync.publisher.metadata_gen import MetadataGenerator
from stagesync.publisher.resumable_upload import ResumableUploader, UploadResult
from stagesync.security import sanitize_filename, validate_safe_path

logger = logging.getLogger(__name__)


class PipelineCoordinator:
    """Master pipeline orchestrator for StageSync."""

    def _is_short_clip(self, duration_seconds: Optional[float]) -> bool:
        """Return whether a clip is below the configured archival threshold."""
        return (
            duration_seconds is not None
            and duration_seconds < self.settings.min_duration_seconds
        )

    @staticmethod
    def _normalized_song_title(title: Optional[str]) -> str:
        return "".join(char.lower() for char in (title or "") if char.isalnum())

    def _setlist_song_titles(self, concert: Optional[Concert]) -> List[str]:
        if not concert or not concert.setlist_data:
            return []
        titles: List[str] = []
        for set_entry in concert.setlist_data.get("sets", {}).get("set", []):
            for song_entry in set_entry.get("song", []):
                title = song_entry.get("name")
                if title:
                    titles.append(title)
        return titles

    def _is_setlist_song(self, title: Optional[str], candidate_titles: List[str]) -> bool:
        normalized_title = self._normalized_song_title(title)
        if not normalized_title:
            return False
        return any(
            normalized_title == self._normalized_song_title(candidate)
            or normalized_title in self._normalized_song_title(candidate)
            or self._normalized_song_title(candidate) in normalized_title
            for candidate in candidate_titles
        )

    _UNRESOLVED_SONG_TITLES = {
        "",
        "live performance",
        "unidentified clip",
        "unidentified song",
        "unknown",
        "unidentified",
        "instrumental",
        "clip",
        "none",
        "null",
        "n/a",
    }

    @staticmethod
    def _usable_artist(artist: Optional[str]) -> str:
        value = (artist or "").strip()
        if value.casefold() in {"live artist", "unknown artist", "unknown"}:
            return ""
        return value

    @classmethod
    def _usable_song(cls, song_title: Optional[str]) -> str:
        value = (song_title or "").strip()
        if value.casefold() in cls._UNRESOLVED_SONG_TITLES:
            return ""
        return value

    def is_video_song_unmatched(self, video_id: str) -> bool:
        """Return True if a video still lacks a confidently identified song title."""
        ident = self.db.get_identification_by_video_id(video_id)
        if not ident:
            return True
        if not self._usable_song(ident.detected_song_title):
            return True
        return (ident.match_confidence or 0.0) < 0.50

    def __init__(
        self,
        db: Optional[Database] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or get_db()

        # Ingestion components
        self.photos_client = GooglePhotosClient()
        self.drive_client = GoogleDriveClient()
        self.metadata_parser = MetadataParser()

        # Intelligence components
        self.audio_extractor = AudioExtractor(cache_dir=self.settings.cache_dir / "playback_audio")
        self.geocoder = ReverseGeocoder(db=self.db)
        self.setlist_client = SetlistFmClient()
        self.musicbrainz_client = MusicBrainzClient()
        self.shazam_matcher = ShazamMatcher()
        self.ai_audio_judge = AIAudioJudge()
        self.spatial_correlator = SpatialTemporalCorrelator()
        self.thumbnail_extractor = ThumbnailExtractor()
        self.venue_search_engine = VenueSearchEngine(
            db=self.db,
            setlist_client=self.setlist_client,
            geocoder=self.geocoder,
        )

        # Publisher components
        self.metadata_gen = MetadataGenerator()
        self.uploader = ResumableUploader(db=self.db)

        # Storage
        self.download_dir = (self.settings.cache_dir / "downloads").resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def get_song_options(self, video_id: str, query: str = "") -> Dict[str, Any]:
        """Return setlist songs first, then bounded discography search results with explicit status/errors."""
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found.")

        ident = self.db.get_identification_by_video_id(video.id)
        concert = self.db.get_concert(ident.concert_id) if ident and ident.concert_id else None
        ident_artist = self._usable_artist(ident.detected_artist if ident else "")
        headliner_artist = self._usable_artist(concert.headliner_artist if concert else "")
        artist = ident_artist or headliner_artist

        is_opener = bool(
            ident_artist
            and headliner_artist
            and ident_artist.casefold() != headliner_artist.casefold()
        )
        setlist_songs = [] if is_opener else self._setlist_song_titles(concert)
        normalized_query = query.strip().casefold()

        if setlist_songs:
            songs = [
                {
                    "title": title,
                    "artist": artist,
                    "source": "setlist",
                }
                for title in setlist_songs
                if not normalized_query or normalized_query in title.casefold()
            ]
            if songs or len(normalized_query) < 2:
                return {
                    "source": "setlist",
                    "artist": artist,
                    "songs": songs[:50],
                    "total_setlist_songs": len(setlist_songs),
                }

        if not artist:
            return {
                "source": "none",
                "songs": [],
                "message": "Select the performing band first before searching songs.",
            }

        if len(normalized_query) < 2:
            return {
                "source": "musicbrainz",
                "artist": artist,
                "songs": [],
                "requires_query": True,
                "message": f"Type at least 2 characters and click Search to query {artist}'s discography.",
            }

        try:
            mb_songs = self.musicbrainz_client.search_recordings(
                artist,
                query,
                raise_on_error=True,
            )
            return {
                "source": "musicbrainz",
                "artist": artist,
                "songs": mb_songs,
                "fallback_from_setlist": bool(setlist_songs),
            }
        except Exception as exc:
            return {
                "source": "musicbrainz",
                "artist": artist,
                "songs": [],
                "error": str(exc),
            }

    def _audit_step(
        self,
        video_id: str,
        step: PipelineStep,
        func: Callable[[], Tuple[StepStatus, Dict[str, Any], Optional[str]]],
    ) -> Tuple[StepStatus, Dict[str, Any]]:
        """Wrap discrete pipeline step execution with audit logging into database."""
        start_time = time.time()
        start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        run = PipelineRun(
            video_id=video_id,
            step_name=step,
            status=StepStatus.IN_PROGRESS,
            started_at=start_iso,
        )
        self.db.log_pipeline_run(run)

        try:
            status, output, error_log = func()
        except Exception as e:
            logger.exception("Step %s failed for video %s: %s", step.value, video_id, e)
            status = StepStatus.FAILED
            output = {}
            error_log = str(e)

        duration_ms = int((time.time() - start_time) * 1000)
        end_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        run.status = status
        run.duration_ms = duration_ms
        run.output_payload = output
        run.error_log = error_log
        run.completed_at = end_iso
        self.db.log_pipeline_run(run)

        return status, output

    # -------------------------------------------------------------------------
    # Ingestion & Discovery
    # -------------------------------------------------------------------------

    def scan_google_photos(
        self,
        start_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        end_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        days: Optional[int] = None,
        album_id: Optional[str] = None,
        dry_run: bool = False,
        limit: int = 50,
    ) -> List[Video]:
        """Scan Google Photos for video items matching date range/days and catalog new discoveries."""
        logger.info(
            "Scanning Google Photos (start_date=%s, end_date=%s, days=%s, album=%s, dry_run=%s)",
            start_date,
            end_date,
            days,
            album_id,
            dry_run,
        )
        discovered_videos: List[Video] = []

        try:
            items, _ = self.photos_client.list_videos(
                start_date=start_date,
                end_date=end_date,
                days=days,
                album_id=album_id,
                page_size=limit,
            )
        except Exception as e:
            logger.error("Failed to query Google Photos: %s", e)
            raise RuntimeError(f"Google Photos query failed: {e}") from e

        for item in items:
            media_id = item.get("id")
            if not media_id:
                continue

            # Check if video is already recorded
            existing = self.db.get_video_by_source_id(media_id)
            if existing:
                continue

            raw_filename = item.get("filename", f"video_{media_id}.mp4")
            clean_filename = sanitize_filename(raw_filename)
            base_url = item.get("baseUrl")
            metadata_raw = item.get("mediaMetadata", {})

            # Pre-filter: Skip Live Photos and short clips using the API-provided duration.
            # Google Photos mediaMetadata.video.duration is in seconds (string or numeric).
            video_meta_raw = metadata_raw.get("video", {})
            raw_duration = video_meta_raw.get("duration")
            api_duration_sec: Optional[float] = None
            if raw_duration is not None:
                try:
                    api_duration_sec = float(raw_duration)
                except (ValueError, TypeError):
                    pass
            if api_duration_sec is not None and api_duration_sec < self.settings.min_duration_seconds:
                logger.debug(
                    "Skipping short video/Live Photo %s (%.1fs < %.1fs)",
                    clean_filename,
                    api_duration_sec,
                    self.settings.min_duration_seconds,
                )
                continue

            video = Video(
                source_photo_id=media_id,
                filename=clean_filename,
                mime_type=item.get("mimeType", "video/mp4"),
                width=int(metadata_raw.get("width", 0)) or None,
                height=int(metadata_raw.get("height", 0)) or None,
                duration_seconds=api_duration_sec,
                processing_status=VideoStatus.DISCOVERED,
            )

            if dry_run:
                video.processing_status = VideoStatus.DISCOVERED
                saved = self.db.save_video(video)
                discovered_videos.append(saved)
                continue

            # Live download
            if base_url:
                local_dir = (self.settings.cache_dir / "downloads").resolve()
                local_dir.mkdir(parents=True, exist_ok=True)
                target_file = validate_safe_path(local_dir, local_dir / f"{media_id}_{clean_filename}")
                try:
                    self.db.update_video_status(video.id, VideoStatus.DOWNLOADING)
                    file_path, sha_hash = self.photos_client.download_video(base_url, target_file)
                    video.file_path = str(file_path)
                    video.file_hash_sha256 = sha_hash
                    video.file_size_bytes = file_path.stat().st_size
                    video.processing_status = VideoStatus.DOWNLOADED
                except Exception as e:
                    logger.error("Download failed for %s: %s", media_id, e)
                    video.processing_status = VideoStatus.FAILED

            saved = self.db.save_video(video)
            discovered_videos.append(saved)

        return discovered_videos

    def ingest_picker_items(
        self,
        items: List[Dict[str, Any]],
        dry_run: bool = False,
    ) -> List[Video]:
        """Ingest selected media items returned from the Google Photos Picker API."""
        discovered_videos: List[Video] = []
        for item in items:
            media_id = item.get("id")
            if not media_id:
                continue

            existing = self.db.get_video_by_source_id(media_id)
            if existing:
                discovered_videos.append(existing)
                continue

            raw_filename = item.get("filename", f"picker_video_{media_id}.mp4")
            clean_filename = sanitize_filename(raw_filename)
            base_url = item.get("baseUrl")
            metadata_raw = item.get("mediaMetadata", {})

            # Pre-filter: Skip Live Photos and short clips using the API-provided duration.
            # Google Photos mediaMetadata.video.duration is in seconds (string or numeric).
            video_meta_raw = metadata_raw.get("video", {})
            raw_duration = video_meta_raw.get("duration")
            api_duration_sec: Optional[float] = None
            if raw_duration is not None:
                try:
                    api_duration_sec = float(raw_duration)
                except (ValueError, TypeError):
                    pass
            if api_duration_sec is not None and api_duration_sec < self.settings.min_duration_seconds:
                logger.debug(
                    "Skipping short picker video/Live Photo %s (%.1fs < %.1fs)",
                    clean_filename,
                    api_duration_sec,
                    self.settings.min_duration_seconds,
                )
                continue

            video = Video(
                source_photo_id=media_id,
                filename=clean_filename,
                mime_type=item.get("mimeType", "video/mp4"),
                width=int(metadata_raw.get("width", 0)) or None,
                height=int(metadata_raw.get("height", 0)) or None,
                duration_seconds=api_duration_sec,
                processing_status=VideoStatus.DISCOVERED,
            )

            if dry_run or not base_url:
                video.processing_status = VideoStatus.DISCOVERED
            else:
                target_file = self.download_dir / f"{video.id}_{clean_filename}"
                try:
                    self.db.update_video_status(video.id, VideoStatus.DOWNLOADING)
                    file_path, sha_hash = self.photos_client.download_video(base_url, target_file)
                    video.file_path = str(file_path)
                    video.file_hash_sha256 = sha_hash
                    video.file_size_bytes = file_path.stat().st_size
                    video.processing_status = VideoStatus.DOWNLOADED
                except Exception as e:
                    logger.error("Download failed for picker item %s: %s", media_id, e)
                    video.processing_status = VideoStatus.FAILED

            saved = self.db.save_video(video)
            discovered_videos.append(saved)

        return discovered_videos

    def scan_google_drive(
        self,
        start_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        end_date: Optional[Union[datetime.date, datetime.datetime, str]] = None,
        days: Optional[int] = None,
        folder_id: Optional[str] = None,
        dry_run: bool = False,
        limit: int = 50,
    ) -> List[Video]:
        """Scan Google Drive for video files matching date range and catalog discoveries."""
        clean_folder = extract_folder_id(folder_id)
        logger.info(
            "Scanning Google Drive (start_date=%s, end_date=%s, days=%s, folder_id=%s, dry_run=%s)",
            start_date,
            end_date,
            days,
            clean_folder,
            dry_run,
        )
        discovered_videos: List[Video] = []
        target_limit = limit or 50
        page_token: Optional[str] = None
        all_items: List[Dict[str, Any]] = []

        try:
            while len(all_items) < target_limit:
                page_size = min(100, target_limit - len(all_items))
                items_page, next_token = self.drive_client.list_videos(
                    start_date=start_date,
                    end_date=end_date,
                    days=days,
                    folder_id=clean_folder,
                    page_size=page_size,
                    page_token=page_token,
                )
                all_items.extend(items_page)
                if not next_token or not items_page:
                    break
                page_token = next_token
        except Exception as e:
            logger.error("Failed to query Google Drive: %s", e)
            if not all_items:
                raise RuntimeError(f"Google Drive query failed: {e}") from e

        # Fetch GPS photo samples from target Drive folder for neighbor correlation
        drive_photo_samples: List[PhotoLocationSample] = []
        if clean_folder:
            try:
                drive_photo_samples = self.spatial_correlator.index_drive_folder_photos(
                    self.drive_client, clean_folder
                )
            except Exception as e:
                logger.debug("Drive folder photo pre-indexing skipped: %s", e)

        for item in all_items:
            file_id = item.get("id")
            if not file_id:
                continue

            source_id = f"drive_{file_id}"
            existing = self.db.get_video_by_source_id(source_id)
            if existing:
                # If already downloaded and available locally, skip
                if existing.file_path and Path(existing.file_path).exists():
                    continue
                # If running a dry-run and already cataloged, skip
                if dry_run:
                    continue
                # Discovered in a prior dry-run and now executing live scan: download!
                video = existing
                clean_filename = video.filename
            else:
                raw_filename = item.get("name", f"drive_video_{file_id}.mp4")
                clean_filename = sanitize_filename(raw_filename)
                file_size = int(item.get("size", 0)) if item.get("size") else None
                mime_type = item.get("mimeType", "video/mp4")

                if not is_likely_video_file(clean_filename, mime_type, file_size):
                    logger.debug("Skipping non-video Drive item: %s (%s, %s bytes)", clean_filename, mime_type, file_size)
                    continue

                video_meta = item.get("videoMediaMetadata") or {}
                image_meta = item.get("imageMediaMetadata") or {}
                loc = image_meta.get("location") or {}

                width = int(video_meta.get("width", 0)) or None
                height = int(video_meta.get("height", 0)) or None
                duration_ms = float(video_meta.get("durationMillis", 0)) or 0.0
                duration_sec = duration_ms / 1000.0 if duration_ms > 0 else None

                # Pre-filter: Skip Live Photos and short clips (< min_duration_seconds)
                if duration_sec is not None and duration_sec < self.settings.min_duration_seconds:
                    logger.debug(
                        "Skipping short video/Live Photo %s (%.1fs < %.1fs)",
                        clean_filename,
                        duration_sec,
                        self.settings.min_duration_seconds,
                    )
                    continue

                lat = float(loc.get("latitude")) if loc.get("latitude") is not None else None
                lng = float(loc.get("longitude")) if loc.get("longitude") is not None else None

                # Correlate GPS from neighbor photos if video lacks coordinates
                device_make = None
                device_model = None
                if (lat is None or lng is None) and drive_photo_samples:
                    corr = self.spatial_correlator.correlate_video_location(
                        video_recorded_at=item.get("createdTime"),
                        photo_samples=drive_photo_samples,
                        video_recorded_at_local=image_meta.get("time") or video_meta.get("time"),
                    )
                    if corr:
                        lat = corr.latitude
                        lng = corr.longitude
                        device_make = corr.device_make
                        device_model = corr.device_model

                video = Video(
                    source_photo_id=source_id,
                    filename=clean_filename,
                    mime_type=mime_type,
                    width=width,
                    height=height,
                    duration_seconds=duration_sec,
                    latitude=lat,
                    longitude=lng,
                    device_make=device_make,
                    device_model=device_model,
                    file_size_bytes=file_size,
                    recorded_at_utc=item.get("createdTime"),
                    processing_status=VideoStatus.DISCOVERED,
                )

            if dry_run:
                video.processing_status = VideoStatus.DISCOVERED
            else:
                target_file = self.download_dir / f"{video.id}_{clean_filename}"
                try:
                    self.db.update_video_status(video.id, VideoStatus.DOWNLOADING)
                    file_path, sha_hash = self.drive_client.download_video(file_id, target_file)
                    video.file_path = str(file_path)
                    video.file_hash_sha256 = sha_hash
                    video.file_size_bytes = file_path.stat().st_size
                    video.processing_status = VideoStatus.DOWNLOADED

                    with contextlib.suppress(Exception):
                        parsed_meta = self.metadata_parser.probe_file(file_path)
                        video.duration_seconds = parsed_meta.duration_seconds
                        video.width = parsed_meta.width
                        video.height = parsed_meta.height
                        video.latitude = parsed_meta.latitude or video.latitude
                        video.longitude = parsed_meta.longitude or video.longitude
                        if parsed_meta.recorded_at_utc:
                            video.recorded_at_utc = parsed_meta.recorded_at_utc
                except Exception as e:
                    logger.error("Download failed for Drive file %s: %s", file_id, e)
                    video.processing_status = VideoStatus.FAILED

            saved = self.db.save_video(video)
            discovered_videos.append(saved)

        return discovered_videos

    def scan_local_directory(self, directory_path: Union[str, Path]) -> List[Video]:
        """Scan a local directory for video files and ingest into pipeline."""
        target_dir = Path(directory_path).resolve()
        if not target_dir.exists() or not target_dir.is_dir():
            raise NotADirectoryError(f"Directory not found: {target_dir}")

        # Index still photos and sidecars in the folder for neighbor GPS correlation
        local_photo_samples = self.spatial_correlator.scan_local_directory_for_photos(target_dir)

        video_extensions = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
        found_videos: List[Video] = []

        for p in target_dir.glob("**/*"):
            if p.is_file() and p.suffix.lower() in video_extensions:
                source_id = f"local_{hashlib.md5(str(p).encode()).hexdigest()[:16]}"
                existing = self.db.get_video_by_source_id(source_id)
                if existing:
                    found_videos.append(existing)
                    continue

                v = Video(
                    source_photo_id=source_id,
                    filename=p.name,
                    file_path=str(p),
                    file_size_bytes=p.stat().st_size,
                    processing_status=VideoStatus.DOWNLOADED,
                )
                with contextlib.suppress(Exception):
                    parsed_meta = self.metadata_parser.probe_file(p)
                    v.duration_seconds = parsed_meta.duration_seconds
                    v.width = parsed_meta.width
                    v.height = parsed_meta.height
                    v.latitude = parsed_meta.latitude
                    v.longitude = parsed_meta.longitude
                    if parsed_meta.recorded_at_utc:
                        v.recorded_at_utc = parsed_meta.recorded_at_utc

                if self._is_short_clip(v.duration_seconds):
                    logger.debug(
                        "Skipping short local video/Live Photo %s (%.1fs < %.1fs)",
                        v.filename,
                        v.duration_seconds,
                        self.settings.min_duration_seconds,
                    )
                    continue

                # Correlate GPS from neighbor photos if video lacks coordinates
                if (v.latitude is None or v.longitude is None) and local_photo_samples:
                    corr = self.spatial_correlator.correlate_video_location(
                        video_recorded_at=v.recorded_at_utc,
                        photo_samples=local_photo_samples,
                        fallback_date_str=v.recorded_at_local[:10] if v.recorded_at_local else None,
                        video_recorded_at_local=v.recorded_at_local,
                    )
                    if corr:
                        v.latitude = corr.latitude
                        v.longitude = corr.longitude
                        v.altitude_meters = corr.altitude_meters
                        v.device_make = v.device_make or corr.device_make
                        v.device_model = v.device_model or corr.device_model

                saved = self.db.save_video(v)
                found_videos.append(saved)

        return found_videos

    # -------------------------------------------------------------------------
    # Core Intelligence Pipeline
    # -------------------------------------------------------------------------

    def process_video(self, video: Video) -> Video:
        """Execute end-to-end intelligence pipeline on a single video entity."""
        logger.info("Starting processing pipeline for video %s (%s)", video.id, video.filename)

        if self._is_short_clip(video.duration_seconds):
            logger.info(
                "Rejecting short video/Live Photo %s (%.1fs < %.1fs)",
                video.filename,
                video.duration_seconds,
                self.settings.min_duration_seconds,
            )
            video.processing_status = VideoStatus.REJECTED
            self.db.save_video(video)
            return video

        if not video.file_path or not Path(video.file_path).exists():
            # If candidate was cataloged from Google Drive during dry-run, download binary on-demand
            if video.source_photo_id and video.source_photo_id.startswith("drive_"):
                drive_file_id = video.source_photo_id.replace("drive_", "")
                target_file = self.download_dir / f"{video.id}_{video.filename}"
                try:
                    logger.info("On-demand downloading Drive video %s (%s)...", video.id, video.filename)
                    self.db.update_video_status(video.id, VideoStatus.DOWNLOADING)
                    file_path_res, sha_hash = self.drive_client.download_video(drive_file_id, target_file)
                    video.file_path = str(file_path_res)
                    video.file_hash_sha256 = sha_hash
                    video.file_size_bytes = file_path_res.stat().st_size
                    video.processing_status = VideoStatus.DOWNLOADED

                    with contextlib.suppress(Exception):
                        parsed_meta = self.metadata_parser.probe_file(file_path_res)
                        video.duration_seconds = parsed_meta.duration_seconds
                        video.width = parsed_meta.width
                        video.height = parsed_meta.height
                        video.latitude = parsed_meta.latitude or video.latitude
                        video.longitude = parsed_meta.longitude or video.longitude
                        if parsed_meta.recorded_at_utc:
                            video.recorded_at_utc = parsed_meta.recorded_at_utc

                    self.db.save_video(video)
                except Exception as e:
                    logger.error("On-demand download failed for Drive video %s: %s", video.id, e)
                    self.db.update_video_status(video.id, VideoStatus.FAILED)
                    return video
            else:
                logger.warning("Video file not available locally for %s", video.id)
                self.db.update_video_status(video.id, VideoStatus.FAILED)
                return video

        file_path = Path(video.file_path)

        # ---------------------------------------------------------------------
        # Step 1: EXIF & Container Inspection (ffprobe)
        # ---------------------------------------------------------------------
        parsed_meta: Optional[ParsedVideoMetadata] = None

        def step_exif():
            nonlocal parsed_meta
            parsed_meta = self.metadata_parser.probe_file(file_path)
            return StepStatus.SUCCESS, parsed_meta.model_dump(), None

        self._audit_step(video.id, PipelineStep.EXIF_EXTRACTION, step_exif)

        if parsed_meta:
            video.duration_seconds = parsed_meta.duration_seconds
            video.width = parsed_meta.width
            video.height = parsed_meta.height
            video.file_hash_sha256 = parsed_meta.file_hash_sha256
            video.recorded_at_utc = parsed_meta.recorded_at_utc
            video.recorded_at_local = parsed_meta.recorded_at_local
            video.timezone_offset_minutes = parsed_meta.timezone_offset_minutes
            video.latitude = parsed_meta.latitude or video.latitude
            video.longitude = parsed_meta.longitude or video.longitude
            video.altitude_meters = parsed_meta.altitude_meters or video.altitude_meters
            video.device_make = parsed_meta.device_make or video.device_make
            video.device_model = parsed_meta.device_model or video.device_model
            video.is_concert_candidate = parsed_meta.is_concert_candidate
            video.concert_confidence_score = parsed_meta.concert_confidence_score
            video.processing_status = VideoStatus.ANALYZING_METADATA
            self.db.save_video(video)

        # ---------------------------------------------------------------------
        # Step 1.5: Spatial-Temporal Neighbor Correlation (for iPhone videos)
        # ---------------------------------------------------------------------
        if video.latitude is None or video.longitude is None:
            def step_spatial_correlate():
                photo_samples: List[PhotoLocationSample] = []
                # 1. Check local directory where video resides
                if file_path.parent.exists():
                    photo_samples = self.spatial_correlator.scan_local_directory_for_photos(file_path.parent)

                # 2. If video was imported from Google Drive, query the Drive folder
                if not photo_samples and video.source_photo_id and video.source_photo_id.startswith("drive_"):
                    drive_file_id = video.source_photo_id.replace("drive_", "")
                    try:
                        meta = self.drive_client.get_file_metadata(drive_file_id)
                        parent_id = meta.get("parents", [None])[0] if meta else None
                        if parent_id:
                            photo_samples = self.spatial_correlator.index_drive_folder_photos(
                                self.drive_client, parent_id
                            )
                    except Exception as e:
                        logger.debug("Drive folder photo query failed for video %s: %s", video.id, e)

                # 3. Correlate with closest neighbor photo
                corr = self.spatial_correlator.correlate_video_location(
                    video_recorded_at=video.recorded_at_utc,
                    photo_samples=photo_samples,
                    fallback_date_str=video.recorded_at_local[:10] if video.recorded_at_local else None,
                    video_recorded_at_local=video.recorded_at_local,
                )
                if corr:
                    video.latitude = corr.latitude
                    video.longitude = corr.longitude
                    video.altitude_meters = corr.altitude_meters
                    if not video.device_make and corr.device_make:
                        video.device_make = corr.device_make
                    if not video.device_model and corr.device_model:
                        video.device_model = corr.device_model

                    # Re-score concert candidate probability with known location
                    is_cand, score = self.metadata_parser.evaluate_concert_candidate(
                        duration_s=video.duration_seconds or 0.0,
                        recorded_at_local=video.recorded_at_local,
                        has_audio=True,
                        has_location=True,
                    )
                    video.is_concert_candidate = is_cand
                    video.concert_confidence_score = score
                    self.db.save_video(video)

                    return StepStatus.SUCCESS, {
                        "correlated_from": corr.source_reference,
                        "time_delta_seconds": corr.time_delta_seconds,
                        "latitude": corr.latitude,
                        "longitude": corr.longitude,
                        "device_model": corr.device_model,
                    }, None

                return StepStatus.SKIPPED, {"message": "No neighbor photo found with GPS"}, None

            self._audit_step(video.id, PipelineStep.SPATIAL_CORRELATION, step_spatial_correlate)

        # ---------------------------------------------------------------------
        # Step 2: Reverse Geocoding & Venue Resolution
        # ---------------------------------------------------------------------
        resolved_venue: Optional[Venue] = None

        if video.latitude is not None and video.longitude is not None:
            def step_geo():
                nonlocal resolved_venue
                # 1. Proximity check: Look for known venue within ~350m in local database
                nearby_venue = self.db.find_venue_by_coords(video.latitude, video.longitude, tolerance=0.005)
                if nearby_venue and nearby_venue.name.lower() != nearby_venue.city.lower():
                    logger.info("Found existing nearby venue '%s' for coordinates (%.4f, %.4f)", nearby_venue.name, video.latitude, video.longitude)
                    resolved_venue = nearby_venue
                    return StepStatus.SUCCESS, nearby_venue.model_dump(), None

                # 2. Reverse geocode via Nominatim
                loc = self.geocoder.reverse_geocode(video.latitude, video.longitude)

                venue_name = loc.venue_name
                # NEVER fallback to city name! A city is NOT a venue name.
                if not venue_name:
                    street = loc.display_name.split(",")[0].strip() if loc.display_name else "Live Venue"
                    venue_name = f"Venue near {street}"

                venue = Venue(
                    name=venue_name,
                    formatted_address=loc.display_name,
                    city=loc.city,
                    state_province=loc.state_province,
                    country_code=loc.country_code,
                    latitude=loc.latitude,
                    longitude=loc.longitude,
                    osm_id=loc.osm_id,
                )
                resolved_venue = self.db.save_venue(venue)
                return StepStatus.SUCCESS, loc.model_dump(), None

            self._audit_step(video.id, PipelineStep.REVERSE_GEOCODE, step_geo)

        # ---------------------------------------------------------------------
        # Step 3: Concert Search (Setlist.fm)
        # ---------------------------------------------------------------------
        matched_concert: Optional[Concert] = None
        event_date_str = (
            video.recorded_at_local[:10]
            if video.recorded_at_local
            else (video.recorded_at_utc[:10] if video.recorded_at_utc else None)
        )

        if event_date_str:
            def step_concert():
                nonlocal matched_concert, resolved_venue
                res: Optional[MatchedConcert] = None

                # 1. Search by exact venue name on event date if venue is known
                if resolved_venue and resolved_venue.name and not resolved_venue.name.startswith("Venue near"):
                    res = self.setlist_client.search_by_venue_and_date(
                        event_date=event_date_str,
                        venue_name=resolved_venue.name,
                        city_name=resolved_venue.city,
                    )
                    # If recorded after local midnight, also check adjacent UTC date
                    if not res and video.recorded_at_utc and video.recorded_at_utc[:10] != event_date_str:
                        res = self.setlist_client.search_by_venue_and_date(
                            event_date=video.recorded_at_utc[:10],
                            venue_name=resolved_venue.name,
                            city_name=resolved_venue.city,
                        )

                # 2. Search by city with venue name hint
                if not res and resolved_venue and resolved_venue.city:
                    res = self.setlist_client.search_by_city_and_date(
                        event_date=event_date_str,
                        city_name=resolved_venue.city,
                        venue_name_hint=resolved_venue.name if resolved_venue else None,
                    )

                if res:
                    # If Setlist.fm returned a verified venue name, ensure resolved_venue reflects it
                    if resolved_venue and res.venue_name and res.venue_name != "Unknown Venue":
                        if resolved_venue.name.startswith("Venue near") or resolved_venue.name.lower() == resolved_venue.city.lower():
                            resolved_venue.name = res.venue_name
                            resolved_venue.setlist_fm_venue_id = res.venue_id
                            resolved_venue = self.db.save_venue(resolved_venue)

                    venue_id = resolved_venue.id if resolved_venue else None
                    concert = Concert(
                        venue_id=venue_id,
                        headliner_artist=res.headliner_artist,
                        lineup_artists=res.lineup_artists,
                        tour_name=res.tour_name,
                        event_date=res.event_date,
                        setlist_fm_event_id=res.setlist_fm_event_id,
                        setlist_data=res.raw_setlist_data,
                        source_url=res.source_url,
                    )
                    matched_concert = self.db.save_concert(concert)
                    return StepStatus.SUCCESS, res.model_dump(), None
                return StepStatus.SKIPPED, {"message": "No setlist found"}, None

            self._audit_step(video.id, PipelineStep.CONCERT_SEARCH, step_concert)

        # ---------------------------------------------------------------------
        # Step 4: Audio Extraction & Acoustic Fingerprinting
        # ---------------------------------------------------------------------
        audio_clip_paths: List[Path] = []
        try:
            audio_clip_paths = self.audio_extractor.extract_clips(
                video_path=file_path,
                video_duration=video.duration_seconds,
            )
        except Exception as e:
            logger.warning("Audio extraction failed for %s: %s", video.id, e)

        detected_song: Optional[str] = None
        detected_artist: Optional[str] = None
        detected_album: Optional[str] = None
        confidence: float = 0.0
        service_used = "shazamio"
        setlist_verified = False
        track_number: Optional[int] = None
        lyrics_snippet: Optional[str] = None
        raw_payload: Dict[str, Any] = {}

        if audio_clip_paths and all(path.exists() for path in audio_clip_paths):
            candidate_song_list = self._setlist_song_titles(matched_concert)
            shazam_results = self.shazam_matcher.recognize_many(audio_clip_paths)
            ranked_results = sorted(
                enumerate(shazam_results),
                key=lambda item: (
                    self._is_setlist_song(item[1].song_title, candidate_song_list),
                    item[1].confidence,
                ),
                reverse=True,
            )
            best_result = ranked_results[0][1] if ranked_results else AcousticMatchResult()
            best_index = ranked_results[0][0] if ranked_results else 0
            audio_clip_path = audio_clip_paths[best_index]

            def step_shazam():
                nonlocal detected_song, detected_artist, detected_album, confidence, raw_payload
                shazam_res = best_result
                if shazam_res.matched:
                    detected_song = shazam_res.song_title
                    detected_artist = shazam_res.artist
                    detected_album = shazam_res.album
                    confidence = shazam_res.confidence
                    raw_payload = {
                        "selected_window": best_index + 1,
                        "windows": [
                            {"window": index + 1, "result": result.model_dump()}
                            for index, result in enumerate(shazam_results)
                        ],
                    }
                    return StepStatus.SUCCESS, {
                        "selected_window": best_index + 1,
                        "windows": [
                            {"window": index + 1, "result": result.model_dump()}
                            for index, result in enumerate(shazam_results)
                        ],
                    }, None
                return StepStatus.SKIPPED, {"message": "No Shazam match"}, None

            self._audit_step(video.id, PipelineStep.AUDIO_FINGERPRINT, step_shazam)

            # Check if Shazam match aligns with Setlist
            if matched_concert and detected_song:
                setlist_verified = self._is_setlist_song(detected_song, candidate_song_list)
                if setlist_verified:
                    confidence = max(confidence, 0.95)

            # Tier 2: Gemini Setlist-Constrained Audio AI (if unverified or low confidence)
            artist_for_ai = matched_concert.headliner_artist if matched_concert else detected_artist

            if (not setlist_verified or confidence < 0.85) and (candidate_song_list or artist_for_ai):
                def step_llm():
                    nonlocal detected_song, detected_artist, confidence, service_used
                    nonlocal setlist_verified, track_number, lyrics_snippet, raw_payload
                    ai_res = self.ai_audio_judge.evaluate_live_clip(
                        audio_path=audio_clip_path,
                        candidate_songs=candidate_song_list,
                        artist_name=artist_for_ai or "Live Artist",
                        venue_name=resolved_venue.name if resolved_venue else "Concert Venue",
                        event_date=matched_concert.event_date if matched_concert else "",
                    )
                    if ai_res.matched:
                        detected_song = ai_res.song_title
                        detected_artist = artist_for_ai or detected_artist
                        confidence = ai_res.confidence
                        service_used = "gemini_multimodal"
                        setlist_verified = bool(candidate_song_list and ai_res.setlist_index)
                        track_number = ai_res.setlist_index
                        lyrics_snippet = ai_res.lyrics_detected
                        raw_payload = ai_res.raw_payload
                        return StepStatus.SUCCESS, ai_res.model_dump(), None
                    return StepStatus.SKIPPED, {"message": ai_res.reasoning or "AI could not disambiguate"}, None

                self._audit_step(video.id, PipelineStep.LLM_DISAMBIGUATION, step_llm)

        # ---------------------------------------------------------------------
        # Step 5: Persist Identification Record
        # ---------------------------------------------------------------------
        if detected_song or matched_concert:
            ident = Identification(
                video_id=video.id,
                concert_id=matched_concert.id if matched_concert else None,
                detected_song_title=detected_song or "Live Performance",
                detected_artist=detected_artist or (matched_concert.headliner_artist if matched_concert else "Live Artist"),
                album_name=detected_album,
                match_confidence=round(confidence, 2),
                fingerprint_service=service_used,
                setlist_verified=setlist_verified,
                track_number_in_setlist=track_number,
                identified_lyrics_snippet=lyrics_snippet,
                raw_service_payload=raw_payload,
            )
            self.db.save_identification(ident)

        # ---------------------------------------------------------------------
        # Step 6: Advance Processing Status
        # ---------------------------------------------------------------------
        dur = video.duration_seconds or 0.0
        if dur < self.settings.min_duration_seconds:
            video.processing_status = VideoStatus.REJECTED
        elif confidence >= self.settings.auto_approve_threshold and video.is_concert_candidate:
            video.processing_status = VideoStatus.APPROVED
        elif (confidence >= 0.50 or matched_concert) and video.is_concert_candidate:
            video.processing_status = VideoStatus.PENDING_REVIEW
        elif video.is_concert_candidate:
            video.processing_status = VideoStatus.PENDING_REVIEW
        else:
            video.processing_status = VideoStatus.REJECTED

        self.db.save_video(video)

        # ---------------------------------------------------------------------
        # Step 7: Pre-generate YouTube Draft Metadata
        # ---------------------------------------------------------------------
        ident_record = self.db.get_identification_by_video_id(video.id)
        yt_meta = self.metadata_gen.generate_metadata(
            video=video,
            identification=ident_record,
            concert=matched_concert,
            venue=resolved_venue,
        )

        upload_job = YouTubeUpload(
            video_id=video.id,
            title=yt_meta.title,
            description=yt_meta.description,
            tags=yt_meta.tags,
            category_id=yt_meta.category_id,
            privacy_status=PrivacyStatus(self.settings.default_privacy),
            upload_status="DRAFT",
            total_bytes=video.file_size_bytes or 0,
        )
        self.db.save_youtube_upload(upload_job)

        logger.info(
            "Video %s processed successfully -> Status: %s (Confidence: %.2f)",
            video.id,
            video.processing_status.value,
            confidence,
        )
        return video

    def process_pending_videos(
        self,
        limit: int = 50,
        status: Optional[Union[VideoStatus, str]] = None,
        reprocess: bool = False,
    ) -> List[Video]:
        """Process discovered, downloaded, or pending videos through the pipeline."""
        target_status: Optional[VideoStatus] = None
        if status:
            if isinstance(status, VideoStatus):
                target_status = status
            elif isinstance(status, str):
                try:
                    target_status = VideoStatus(status)
                except ValueError:
                    target_status = None

        candidates: List[Video] = []
        if target_status:
            candidates = self.db.list_videos(status=target_status, limit=limit)
        elif reprocess:
            candidates = []
            for st in (VideoStatus.DOWNLOADED, VideoStatus.PENDING_REVIEW, VideoStatus.DISCOVERED):
                for v in self.db.list_videos(status=st):
                    if v.latitude is None or not self.db.get_identification_by_video_id(v.id):
                        candidates.append(v)
                    if len(candidates) >= limit:
                        break
                if len(candidates) >= limit:
                    break
        else:
            candidates = self.db.list_videos(status=VideoStatus.DOWNLOADED, limit=limit)
            if not candidates:
                candidates = self.db.list_videos(status=VideoStatus.DISCOVERED, limit=limit)
            if not candidates:
                review_cands = self.db.list_videos(status=VideoStatus.PENDING_REVIEW, limit=limit)
                candidates = [v for v in review_cands if v.latitude is None][:limit]

        processed = []
        for v in candidates:
            if self._is_short_clip(v.duration_seconds):
                v.processing_status = VideoStatus.REJECTED
                self.db.save_video(v)
                continue
            res = self.process_video(v)
            processed.append(res)
        return processed

    # -------------------------------------------------------------------------
    # Review Queue Operations
    # -------------------------------------------------------------------------

    def approve_video(
        self,
        video_id: str,
        song_title_override: Optional[str] = None,
        artist_override: Optional[str] = None,
        privacy_override: Optional[PrivacyStatus] = None,
    ) -> Video:
        """Approve candidate video for YouTube upload, applying optional manual edits."""
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found.")

        ident = self.db.get_identification_by_video_id(video_id)
        if ident and (song_title_override or artist_override):
            if song_title_override:
                ident.detected_song_title = song_title_override
            if artist_override:
                ident.detected_artist = artist_override
            ident.status = "MANUAL_OVERRIDE"
            self.db.save_identification(ident)

        concert = self.db.get_concert(ident.concert_id) if ident and ident.concert_id else None
        venue = None
        if concert and concert.venue_id:
            venue = self.db.get_venue(concert.venue_id)
        elif video.latitude and video.longitude:
            venue = self.db.find_venue_by_coords(video.latitude, video.longitude)

        # Regenerate YouTube metadata with updated values
        meta = self.metadata_gen.generate_metadata(video, ident, concert, venue)
        upload_job = self.db.get_youtube_upload_by_video_id(video_id)
        if upload_job:
            upload_job.title = meta.title
            upload_job.description = meta.description
            upload_job.tags = meta.tags
            if privacy_override:
                upload_job.privacy_status = privacy_override
            upload_job.upload_status = "QUEUED"
            self.db.save_youtube_upload(upload_job)

        video.processing_status = VideoStatus.APPROVED
        self.db.save_video(video)
        return video

    def reject_video(self, video_id: str) -> Video:
        """Reject video candidate."""
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found.")
        video.processing_status = VideoStatus.REJECTED
        self.db.save_video(video)
        return video

    def purge_rejected_videos(
        self,
        max_duration: Optional[float] = None,
        only_rejected: bool = False,
        delete_disk_files: bool = True,
    ) -> List[Dict[str, Any]]:
        """Purge rejected and sub-duration Live Photos from catalog and disk cache."""
        threshold = max_duration if max_duration is not None else self.settings.min_duration_seconds
        purged = self.db.purge_rejected_videos(max_duration=threshold, only_rejected=only_rejected)
        if delete_disk_files:
            for item in purged:
                file_path = item.get("file_path")
                if file_path:
                    try:
                        p = Path(file_path)
                        if p.exists():
                            p.unlink()
                    except OSError as err:
                        logger.warning("Could not delete video file %s: %s", file_path, err)
                vid_id = item.get("id")
                # Also remove any audio clip in download cache
                for audio_cand in self.download_dir.glob(f"{vid_id}*"):
                    try:
                        if audio_cand.is_file():
                            audio_cand.unlink()
                    except OSError:
                        pass
        return purged

    def reidentify_video_with_venue(
        self,
        video_id: str,
        venue_name: str,
        city: Optional[str] = None,
        state_province: Optional[str] = None,
        country_code: str = "US",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        setlist_fm_venue_id: Optional[str] = None,
        osm_id: Optional[str] = None,
        formatted_address: Optional[str] = None,
    ) -> Tuple[Video, Optional[Concert], Optional[Identification]]:
        """Link a venue to a video and re-run concert matching and setlist identification."""
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found.")

        # 1. Resolve or create venue
        venue: Optional[Venue] = None
        if setlist_fm_venue_id:
            venue = self.db.get_venue_by_setlist_id(setlist_fm_venue_id)
        if not venue and latitude is not None and longitude is not None:
            venue = self.db.find_venue_by_coords(latitude, longitude, tolerance=0.005)
        if not venue:
            v_matches = self.db.search_venues(query=venue_name, city=city, limit=1)
            if v_matches:
                venue = v_matches[0]

        if not venue:
            venue = Venue(
                name=venue_name.strip(),
                city=city.strip() if city else "Unknown City",
                state_province=state_province,
                country_code=country_code,
                latitude=latitude,
                longitude=longitude,
                setlist_fm_venue_id=setlist_fm_venue_id,
                osm_id=osm_id,
                formatted_address=formatted_address or f"{venue_name}, {city or ''}",
            )
        else:
            if setlist_fm_venue_id and not venue.setlist_fm_venue_id:
                venue.setlist_fm_venue_id = setlist_fm_venue_id
            if osm_id and not venue.osm_id:
                venue.osm_id = osm_id
            if latitude is not None and venue.latitude is None:
                venue.latitude = latitude
                venue.longitude = longitude

        venue = self.db.save_venue(venue)

        # 2. Update video coordinates if video lacked them
        if video.latitude is None and venue.latitude is not None:
            video.latitude = venue.latitude
            video.longitude = venue.longitude

        # 3. Search Setlist.fm for concert on this video's date at this venue
        event_date_str = (
            video.recorded_at_local[:10]
            if video.recorded_at_local
            else (video.recorded_at_utc[:10] if video.recorded_at_utc else None)
        )

        matched_concert: Optional[Concert] = None
        res: Optional[MatchedConcert] = None
        if event_date_str:
            res = self.setlist_client.search_by_venue_and_date(
                event_date=event_date_str,
                venue_name=venue.name,
                city_name=venue.city,
            )
            # Try adjacent UTC date if recorded near midnight
            if not res and video.recorded_at_utc and video.recorded_at_utc[:10] != event_date_str:
                res = self.setlist_client.search_by_venue_and_date(
                    event_date=video.recorded_at_utc[:10],
                    venue_name=venue.name,
                    city_name=venue.city,
                )
            # Fallback search by city with venue name hint
            if not res and venue.city:
                res = self.setlist_client.search_by_city_and_date(
                    event_date=event_date_str,
                    city_name=venue.city,
                    venue_name_hint=venue.name,
                )

        if res:
            if res.venue_name and res.venue_name != "Unknown Venue":
                venue.name = res.venue_name
                if res.venue_id:
                    venue.setlist_fm_venue_id = res.venue_id
                venue = self.db.save_venue(venue)

            concert = Concert(
                venue_id=venue.id,
                headliner_artist=res.headliner_artist,
                lineup_artists=res.lineup_artists,
                tour_name=res.tour_name,
                event_date=res.event_date,
                setlist_fm_event_id=res.setlist_fm_event_id,
                setlist_data=res.raw_setlist_data,
                source_url=res.source_url,
            )
            matched_concert = self.db.save_concert(concert)

        # 4. Update or create identification
        ident = self.db.get_identification_by_video_id(video.id)
        if not ident:
            ident = Identification(
                video_id=video.id,
                concert_id=matched_concert.id if matched_concert else None,
                detected_song_title="Live Performance",
                detected_artist=matched_concert.headliner_artist if matched_concert else "Live Artist",
                match_confidence=0.85 if matched_concert else 0.40,
                fingerprint_service="venue_user_assisted",
                setlist_verified=bool(matched_concert),
            )
        else:
            if matched_concert:
                ident.concert_id = matched_concert.id
                if not ident.detected_artist or ident.detected_artist in ("Live Artist", "Unknown Artist", "Unknown"):
                    ident.detected_artist = matched_concert.headliner_artist
                ident.setlist_verified = True
                ident.match_confidence = max(ident.match_confidence, 0.85)
            ident.status = "USER_ASSISTED"
        self.db.save_identification(ident)

        # 5. Advance status & YouTube draft metadata
        video.is_concert_candidate = True
        video.processing_status = VideoStatus.PENDING_REVIEW
        self.db.save_video(video)

        yt_meta = self.metadata_gen.generate_metadata(
            video=video,
            identification=ident,
            concert=matched_concert,
            venue=venue,
        )
        upload_job = self.db.get_youtube_upload_by_video_id(video.id)
        if not upload_job:
            upload_job = YouTubeUpload(
                video_id=video.id,
                title=yt_meta.title,
                description=yt_meta.description,
                tags=yt_meta.tags,
                category_id=yt_meta.category_id,
                privacy_status=PrivacyStatus(self.settings.default_privacy),
                upload_status="DRAFT",
                total_bytes=video.file_size_bytes or 0,
            )
        else:
            upload_job.title = yt_meta.title
            upload_job.description = yt_meta.description
            upload_job.tags = yt_meta.tags
        self.db.save_youtube_upload(upload_job)

        return video, matched_concert, ident

    def reidentify_video_with_band(
        self,
        video_id: str,
        artist_name: str,
        song_title: Optional[str] = None,
    ) -> Tuple[Video, Optional[Concert], Optional[Identification]]:
        """Assign specific performing band to video from show lineup."""
        artist_clean = artist_name.strip()
        if not artist_clean:
            raise ValueError("artist_name cannot be empty.")

        video = self.db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found.")

        ident = self.db.get_identification_by_video_id(video.id)
        concert: Optional[Concert] = None
        if ident and ident.concert_id:
            concert = self.db.get_concert(ident.concert_id)

        # If artist not in concert lineup, register it
        if concert:
            if artist_clean not in concert.lineup_artists:
                concert.lineup_artists.append(artist_clean)
                concert = self.db.save_concert(concert)

        artist_setlist: Optional[MatchedConcert] = None
        if not song_title and concert:
            venue = self.db.get_venue(concert.venue_id) if concert.venue_id else None
            if (
                artist_clean.lower() != concert.headliner_artist.lower()
                and venue
                and venue.city
                and concert.event_date
            ):
                artist_setlist = self.setlist_client.search_by_city_and_date(
                    event_date=concert.event_date,
                    city_name=venue.city,
                    artist_name=artist_clean,
                    venue_name_hint=venue.name,
                )

        candidate_songs: List[str] = []
        setlist_source = artist_setlist.setlist_songs if artist_setlist else []
        if setlist_source:
            candidate_songs = [song.name for song in setlist_source if song.name]
        elif concert:
            for set_group in concert.setlist_data.get("sets", {}).get("set", []):
                candidate_songs.extend(
                    song.get("name", "")
                    for song in set_group.get("song", [])
                    if song.get("name")
                )

        matched_song: Optional[AcousticMatchResult] = None
        ai_song: Optional[AIAudioJudgeResult] = None
        shazam_is_usable = False
        if not song_title and video.file_path and Path(video.file_path).exists():
            try:
                audio_clip_path = self.audio_extractor.extract_clip(
                    video_path=video.file_path,
                    video_duration=video.duration_seconds,
                )
                matched_song = self.shazam_matcher.recognize(audio_clip_path)
                shazam_title = (matched_song.song_title or "").strip()
                shazam_is_usable = matched_song.matched and (
                    not candidate_songs
                    or any(
                        candidate.lower() == shazam_title.lower()
                        or candidate.lower() in shazam_title.lower()
                        for candidate in candidate_songs
                    )
                )
                if not shazam_is_usable:
                    ai_song = self.ai_audio_judge.evaluate_live_clip(
                        audio_path=audio_clip_path,
                        candidate_songs=candidate_songs,
                        artist_name=artist_clean,
                        venue_name=venue.name if venue else "Concert Venue",
                        event_date=concert.event_date if concert else "",
                    )
            except Exception as e:
                logger.warning("Song re-identification failed for %s: %s", video.id, e)

        # Update or create identification
        if not ident:
            resolved_song = song_title.strip() if song_title else None
            if not resolved_song and ai_song and ai_song.matched:
                resolved_song = ai_song.song_title
            if not resolved_song and matched_song and shazam_is_usable:
                resolved_song = matched_song.song_title
            resolved_confidence = (
                ai_song.confidence
                if ai_song and ai_song.matched
                else matched_song.confidence
                if matched_song and shazam_is_usable
                else 0.90
                if song_title
                else 0.75
            )
            resolved_service = (
                ai_song.service
                if ai_song and ai_song.matched
                else matched_song.fingerprint_service
                if matched_song and shazam_is_usable
                else "user_assisted_band"
            )
            ident = Identification(
                video_id=video.id,
                concert_id=concert.id if concert else None,
                detected_song_title=resolved_song or "Live Performance",
                detected_artist=artist_clean,
                match_confidence=resolved_confidence,
                fingerprint_service=resolved_service,
                setlist_verified=bool(candidate_songs and ((ai_song and ai_song.matched) or shazam_is_usable)),
                track_number_in_setlist=ai_song.setlist_index if ai_song and ai_song.matched else None,
                identified_lyrics_snippet=ai_song.lyrics_detected if ai_song and ai_song.matched else None,
                raw_service_payload=(
                    ai_song.raw_payload
                    if ai_song and ai_song.matched
                    else matched_song.raw_payload
                    if matched_song and shazam_is_usable
                    else {}
                ),
                status="USER_ASSISTED_BAND",
            )
        else:
            ident.detected_artist = artist_clean
            if song_title and song_title.strip():
                ident.detected_song_title = song_title.strip()
                ident.match_confidence = max(ident.match_confidence, 0.90)
            elif ai_song and ai_song.matched:
                ident.detected_song_title = ai_song.song_title or ident.detected_song_title
                ident.match_confidence = max(ident.match_confidence, ai_song.confidence)
                ident.fingerprint_service = ai_song.service
                ident.setlist_verified = bool(candidate_songs)
                ident.track_number_in_setlist = ai_song.setlist_index
                ident.identified_lyrics_snippet = ai_song.lyrics_detected
                ident.raw_service_payload = ai_song.raw_payload
            elif matched_song and shazam_is_usable:
                ident.detected_song_title = matched_song.song_title or ident.detected_song_title
                ident.match_confidence = max(ident.match_confidence, matched_song.confidence)
                ident.fingerprint_service = matched_song.fingerprint_service
                ident.setlist_verified = bool(candidate_songs)
                ident.raw_service_payload = matched_song.raw_payload
            if concert:
                ident.concert_id = concert.id
                ident.setlist_verified = True
            ident.status = "USER_ASSISTED_BAND"

        self.db.save_identification(ident)

        # Pre-generate updated YouTube draft metadata
        venue = None
        if concert and concert.venue_id:
            venue = self.db.get_venue(concert.venue_id)
        elif video.latitude and video.longitude:
            venue = self.db.find_venue_by_coords(video.latitude, video.longitude)

        yt_meta = self.metadata_gen.generate_metadata(
            video=video,
            identification=ident,
            concert=concert,
            venue=venue,
        )
        upload_job = self.db.get_youtube_upload_by_video_id(video.id)
        if not upload_job:
            upload_job = YouTubeUpload(
                video_id=video.id,
                title=yt_meta.title,
                description=yt_meta.description,
                tags=yt_meta.tags,
                category_id=yt_meta.category_id,
                privacy_status=PrivacyStatus(self.settings.default_privacy),
                upload_status="DRAFT",
                total_bytes=video.file_size_bytes or 0,
            )
        else:
            upload_job.title = yt_meta.title
            upload_job.description = yt_meta.description
            upload_job.tags = yt_meta.tags
        self.db.save_youtube_upload(upload_job)

        video.processing_status = VideoStatus.PENDING_REVIEW
        self.db.save_video(video)

        return video, concert, ident

    def match_video_song(self, video_id: str) -> Dict[str, Any]:
        """Run multi-window audio song matching on a single video without clobbering user edits.

        Returns a structured dictionary explicitly distinguishing:
        - outcome == "matched": A confident song title was identified and saved.
        - outcome == "no_match": Matchers executed, but no song could be confidently identified.
        - outcome == "error": Matching failed due to missing media, audio extraction, or service errors.
        """
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found.")

        # 1. Ensure local media file exists (or download on-demand for Drive clips)
        if not video.file_path or not Path(video.file_path).exists():
            if video.source_photo_id and video.source_photo_id.startswith("drive_"):
                drive_file_id = video.source_photo_id.replace("drive_", "")
                target_file = self.download_dir / f"{video.id}_{video.filename}"
                try:
                    file_path_res, sha_hash = self.drive_client.download_video(drive_file_id, target_file)
                    video.file_path = str(file_path_res)
                    video.file_hash_sha256 = sha_hash
                    video.file_size_bytes = file_path_res.stat().st_size
                    with contextlib.suppress(Exception):
                        parsed_meta = self.metadata_parser.probe_file(file_path_res)
                        video.duration_seconds = parsed_meta.duration_seconds
                        video.width = parsed_meta.width
                        video.height = parsed_meta.height
                        video.latitude = parsed_meta.latitude or video.latitude
                        video.longitude = parsed_meta.longitude or video.longitude
                        if parsed_meta.recorded_at_utc:
                            video.recorded_at_utc = parsed_meta.recorded_at_utc
                    self.db.save_video(video)
                except Exception as exc:
                    return {
                        "success": False,
                        "matched": False,
                        "outcome": "error",
                        "video_id": video.id,
                        "filename": video.filename,
                        "error": f"Failed to download video from Google Drive: {exc}",
                        "video": video,
                    }
            else:
                return {
                    "success": False,
                    "matched": False,
                    "outcome": "error",
                    "video_id": video.id,
                    "filename": video.filename,
                    "error": "Video file is missing from local storage; cannot extract audio for matching.",
                    "video": video,
                }

        file_path = Path(video.file_path)

        # 2. Load existing identification / concert / venue context (preserving user-selected band/venue)
        ident = self.db.get_identification_by_video_id(video.id)
        preserved_artist = self._usable_artist(ident.detected_artist if ident else "")
        preserved_status = ident.status if ident else "CONFIRMED"
        concert: Optional[Concert] = self.db.get_concert(ident.concert_id) if ident and ident.concert_id else None
        venue: Optional[Venue] = self.db.get_venue(concert.venue_id) if concert and concert.venue_id else None
        if not venue and video.latitude is not None and video.longitude is not None:
            venue = self.db.find_venue_by_coords(video.latitude, video.longitude)

        # If this clip was never processed at all (no ident & no concert), run initial metadata/concert resolution
        if not ident and not concert and video.processing_status in (VideoStatus.DISCOVERED, VideoStatus.DOWNLOADED):
            if video.duration_seconds is None:
                with contextlib.suppress(Exception):
                    parsed_meta = self.metadata_parser.probe_file(file_path)
                    video.duration_seconds = parsed_meta.duration_seconds
                    video.width = parsed_meta.width
                    video.height = parsed_meta.height
                    video.latitude = parsed_meta.latitude or video.latitude
                    video.longitude = parsed_meta.longitude or video.longitude
                    video.recorded_at_utc = parsed_meta.recorded_at_utc or video.recorded_at_utc
                    video.recorded_at_local = parsed_meta.recorded_at_local or video.recorded_at_local
                    video.is_concert_candidate = parsed_meta.is_concert_candidate
                    self.db.save_video(video)

        # 3. Resolve candidate setlist songs for the performing artist
        candidate_song_list: List[str] = []
        if concert:
            is_opener = bool(
                preserved_artist
                and concert.headliner_artist
                and preserved_artist.casefold() != concert.headliner_artist.casefold()
            )
            if is_opener:
                if venue and venue.city and concert.event_date:
                    with contextlib.suppress(Exception):
                        opener_setlist = self.setlist_client.search_by_city_and_date(
                            event_date=concert.event_date,
                            city_name=venue.city,
                            artist_name=preserved_artist,
                            venue_name_hint=venue.name,
                        )
                        if opener_setlist and opener_setlist.setlist_songs:
                            candidate_song_list = [s.name for s in opener_setlist.setlist_songs if s.name]
            else:
                candidate_song_list = self._setlist_song_titles(concert)

        # 4. Extract multi-window audio clips
        try:
            audio_clip_paths = self.audio_extractor.extract_clips(
                video_path=file_path,
                video_duration=video.duration_seconds,
            )
        except Exception as exc:
            return {
                "success": False,
                "matched": False,
                "outcome": "error",
                "video_id": video.id,
                "filename": video.filename,
                "error": f"Audio extraction failed: {exc}",
                "video": video,
            }

        audio_clip_paths = [p for p in audio_clip_paths if Path(p).exists()]
        if not audio_clip_paths:
            return {
                "success": False,
                "matched": False,
                "outcome": "error",
                "video_id": video.id,
                "filename": video.filename,
                "error": "No audio clips could be extracted from this video container.",
                "video": video,
            }

        # 5. Tier 1: Shazam multi-window acoustic recognition
        detected_song: Optional[str] = None
        detected_artist: Optional[str] = None
        detected_album: Optional[str] = None
        confidence: float = 0.0
        service_used = "shazamio"
        setlist_verified = False
        track_number: Optional[int] = None
        lyrics_snippet: Optional[str] = None
        raw_payload: Dict[str, Any] = {}

        shazam_results = self.shazam_matcher.recognize_many(audio_clip_paths)
        shazam_unavailable = any(r.unavailable for r in shazam_results)
        shazam_errors = [r.error for r in shazam_results if r.error and not r.unavailable]

        def _is_artist_compatible(shazam_artist: Optional[str], song_title: Optional[str]) -> bool:
            if not preserved_artist:
                return True
            if candidate_song_list and self._is_setlist_song(song_title, candidate_song_list):
                return True
            if not shazam_artist:
                return True
            a = preserved_artist.casefold()
            b = shazam_artist.strip().casefold()
            return a in b or b in a

        usable_shazam = [
            (idx, r)
            for idx, r in enumerate(shazam_results)
            if r.matched and self._usable_song(r.song_title) and _is_artist_compatible(r.artist, r.song_title)
        ]
        usable_shazam.sort(
            key=lambda item: (
                self._is_setlist_song(item[1].song_title, candidate_song_list),
                item[1].confidence,
            ),
            reverse=True,
        )

        best_shazam = usable_shazam[0][1] if usable_shazam else None
        best_shazam_idx = usable_shazam[0][0] if usable_shazam else 0

        def step_shazam():
            if best_shazam:
                payload = {
                    "selected_window": best_shazam_idx + 1,
                    "windows": [
                        {"window": i + 1, "result": r.model_dump()}
                        for i, r in enumerate(shazam_results)
                    ],
                }
                return StepStatus.SUCCESS, payload, None
            if shazam_errors:
                return StepStatus.FAILED, {"error": shazam_errors[0]}, shazam_errors[0]
            msg = "Shazam unavailable" if shazam_unavailable else "No Shazam match"
            return StepStatus.SKIPPED, {"message": msg}, None

        self._audit_step(video.id, PipelineStep.AUDIO_FINGERPRINT, step_shazam)

        if best_shazam:
            detected_song = self._usable_song(best_shazam.song_title)
            detected_artist = self._usable_artist(best_shazam.artist)
            detected_album = best_shazam.album
            confidence = best_shazam.confidence
            service_used = "shazamio"
            raw_payload = {
                "selected_window": best_shazam_idx + 1,
                "windows": [
                    {"window": i + 1, "result": r.model_dump()}
                    for i, r in enumerate(shazam_results)
                ],
            }
            if candidate_song_list and detected_song:
                setlist_verified = self._is_setlist_song(detected_song, candidate_song_list)
                if setlist_verified:
                    confidence = max(confidence, 0.95)

        # 6. Tier 2: Gemini Multimodal Audio Judge (if Shazam didn't yield high-confidence match)
        artist_for_ai = (
            preserved_artist
            or self._usable_artist(concert.headliner_artist if concert else "")
            or self._usable_artist(detected_artist)
        )
        gemini_ran = False
        gemini_error: Optional[str] = None
        gemini_reasoning: Optional[str] = None

        if (not detected_song or confidence < 0.85) and (candidate_song_list or artist_for_ai):
            gemini_ran = True
            mid_idx = len(audio_clip_paths) // 2
            window_order = [mid_idx]
            if len(audio_clip_paths) > 1:
                alt_idx = 0 if mid_idx != 0 else 1
                window_order.append(alt_idx)

            best_ai_res: Optional[AIAudioJudgeResult] = None
            for win_idx in window_order:
                ai_res = self.ai_audio_judge.evaluate_live_clip(
                    audio_path=audio_clip_paths[win_idx],
                    candidate_songs=candidate_song_list,
                    artist_name=artist_for_ai or "Live Artist",
                    venue_name=venue.name if venue else "Concert Venue",
                    event_date=concert.event_date if concert else "",
                    allow_open_catalog=bool(artist_for_ai),
                )
                best_ai_res = ai_res
                if ai_res.matched and self._usable_song(ai_res.song_title):
                    break
                if ai_res.error:
                    # Don't repeat network/API failure across more windows
                    break

            def step_llm():
                if best_ai_res and best_ai_res.matched and self._usable_song(best_ai_res.song_title):
                    return StepStatus.SUCCESS, best_ai_res.model_dump(), None
                if best_ai_res and best_ai_res.error:
                    return StepStatus.FAILED, {"error": best_ai_res.error}, best_ai_res.error
                reason = (best_ai_res.reasoning if best_ai_res else None) or "AI could not disambiguate"
                return StepStatus.SKIPPED, {"message": reason}, None

            self._audit_step(video.id, PipelineStep.LLM_DISAMBIGUATION, step_llm)

            if best_ai_res:
                gemini_error = best_ai_res.error
                gemini_reasoning = best_ai_res.reasoning
                if best_ai_res.matched and self._usable_song(best_ai_res.song_title):
                    detected_song = self._usable_song(best_ai_res.song_title)
                    detected_artist = artist_for_ai or detected_artist
                    confidence = best_ai_res.confidence
                    service_used = "gemini_multimodal"
                    setlist_verified = bool(candidate_song_list and best_ai_res.setlist_index)
                    track_number = best_ai_res.setlist_index
                    lyrics_snippet = best_ai_res.lyrics_detected
                    raw_payload = best_ai_res.raw_payload

        final_artist = (
            preserved_artist
            or self._usable_artist(detected_artist)
            or self._usable_artist(concert.headliner_artist if concert else "")
        )

        diagnostics: Dict[str, Any] = {
            "windows_checked": len(audio_clip_paths),
            "setlist_candidates_count": len(candidate_song_list),
            "artist_context": final_artist or None,
            "shazam_status": (
                "matched"
                if best_shazam
                else ("unavailable" if shazam_unavailable else ("error" if shazam_errors else "no_match"))
            ),
            "shazam_error": shazam_errors[0] if shazam_errors else None,
            "gemini_status": (
                "matched"
                if (service_used == "gemini_multimodal" and detected_song)
                else ("skipped" if not gemini_ran else ("error" if gemini_error else "no_match"))
            ),
            "gemini_error": gemini_error,
            "gemini_reasoning": gemini_reasoning,
        }

        # 7A. Outcome: MATCHED
        if detected_song and confidence >= 0.50:
            if not ident:
                ident = Identification(
                    video_id=video.id,
                    concert_id=concert.id if concert else None,
                    detected_song_title=detected_song,
                    detected_artist=final_artist or "Live Artist",
                    album_name=detected_album,
                    match_confidence=round(confidence, 2),
                    fingerprint_service=service_used,
                    setlist_verified=setlist_verified,
                    track_number_in_setlist=track_number,
                    identified_lyrics_snippet=lyrics_snippet,
                    raw_service_payload=raw_payload,
                    status=preserved_status,
                )
            else:
                ident.detected_song_title = detected_song
                if final_artist:
                    ident.detected_artist = final_artist
                if detected_album:
                    ident.album_name = detected_album
                ident.match_confidence = round(confidence, 2)
                ident.fingerprint_service = service_used
                ident.setlist_verified = setlist_verified or ident.setlist_verified
                if track_number is not None:
                    ident.track_number_in_setlist = track_number
                if lyrics_snippet:
                    ident.identified_lyrics_snippet = lyrics_snippet
                ident.raw_service_payload = raw_payload
            self.db.save_identification(ident)

            if video.processing_status in (
                VideoStatus.DISCOVERED,
                VideoStatus.DOWNLOADED,
                VideoStatus.ANALYZING_METADATA,
                VideoStatus.CONCERT_MATCHED,
                VideoStatus.FAILED,
            ):
                video.processing_status = VideoStatus.PENDING_REVIEW
                self.db.save_video(video)

            yt_meta = self.metadata_gen.generate_metadata(
                video=video,
                identification=ident,
                concert=concert,
                venue=venue,
            )
            upload_job = self.db.get_youtube_upload_by_video_id(video.id)
            if not upload_job:
                upload_job = YouTubeUpload(
                    video_id=video.id,
                    title=yt_meta.title,
                    description=yt_meta.description,
                    tags=yt_meta.tags,
                    category_id=yt_meta.category_id,
                    privacy_status=PrivacyStatus(self.settings.default_privacy),
                    upload_status="DRAFT",
                    total_bytes=video.file_size_bytes or 0,
                )
            else:
                upload_job.title = yt_meta.title
                upload_job.description = yt_meta.description
                upload_job.tags = yt_meta.tags
            self.db.save_youtube_upload(upload_job)

            service_label = "Gemini Audio AI" if service_used == "gemini_multimodal" else "Shazam"
            pct = int(round(confidence * 100))
            artist_suffix = f" by {final_artist}" if final_artist else ""
            return {
                "success": True,
                "matched": True,
                "outcome": "matched",
                "video_id": video.id,
                "filename": video.filename,
                "song_title": detected_song,
                "artist": final_artist or None,
                "confidence": round(confidence, 2),
                "service": service_used,
                "lyrics_snippet": lyrics_snippet,
                "reasoning": gemini_reasoning,
                "message": f'Matched "{detected_song}"{artist_suffix} ({pct}% confidence via {service_label}).',
                "diagnostics": diagnostics,
                "video": video,
            }

        # 7B. Outcome: ERROR (when no matcher could complete cleanly)
        shazam_failed = bool(shazam_unavailable or shazam_errors)
        gemini_failed_or_missing = bool(gemini_error or not gemini_ran)
        if shazam_failed and gemini_failed_or_missing:
            err_parts: List[str] = []
            if shazam_errors:
                err_parts.append(f"Shazam error: {shazam_errors[0]}")
            elif shazam_unavailable:
                err_parts.append("Shazam unavailable (shazamio not installed)")
            if gemini_error:
                err_parts.append(gemini_error)
            elif not gemini_ran:
                err_parts.append("Gemini AI skipped (performing band is unknown — select the band first)")
            return {
                "success": False,
                "matched": False,
                "outcome": "error",
                "video_id": video.id,
                "filename": video.filename,
                "error": " • ".join(err_parts) or "Song matching services failed.",
                "diagnostics": diagnostics,
                "video": video,
            }

        # 7C. Outcome: NO MATCH (matchers ran, no confident song match found)
        warnings: List[str] = []
        if shazam_errors:
            warnings.append(f"Shazam encountered an error ({shazam_errors[0]})")
        elif shazam_unavailable:
            warnings.append("Shazam acoustic fingerprinting is unavailable")
        if gemini_error:
            warnings.append(f"Gemini AI encountered an error ({gemini_error})")

        if not ident and (concert or final_artist):
            ident = Identification(
                video_id=video.id,
                concert_id=concert.id if concert else None,
                detected_song_title="Live Performance",
                detected_artist=final_artist or "Live Artist",
                match_confidence=0.0,
                fingerprint_service=service_used,
                setlist_verified=False,
                status=preserved_status,
            )
            self.db.save_identification(ident)

        if video.processing_status in (VideoStatus.DISCOVERED, VideoStatus.DOWNLOADED) and (
            video.is_concert_candidate or concert
        ):
            video.processing_status = VideoStatus.PENDING_REVIEW
            self.db.save_video(video)

        detail_parts = [f"Checked {len(audio_clip_paths)} audio clip window(s)"]
        if not shazam_failed:
            detail_parts.append("Shazam found no acoustic match")
        if gemini_ran and not gemini_error:
            short_reason = (gemini_reasoning or "").strip()
            if len(short_reason) > 140:
                short_reason = short_reason[:137].rstrip() + "..."
            detail_parts.append(
                f"Gemini AI could not confidently identify the song ({short_reason})"
                if short_reason
                else "Gemini AI could not confidently identify the song"
            )
        elif not gemini_ran and not final_artist:
            detail_parts.append("select the performing band first to enable AI setlist/catalog matching")

        if warnings:
            detail_parts.extend(warnings)

        return {
            "success": True,
            "matched": False,
            "outcome": "no_match",
            "video_id": video.id,
            "filename": video.filename,
            "song_title": None,
            "artist": final_artist or None,
            "confidence": 0.0,
            "reasoning": gemini_reasoning,
            "warnings": warnings,
            "message": "No song match found — " + "; ".join(detail_parts) + ".",
            "diagnostics": diagnostics,
            "video": video,
        }

    def match_unidentified_songs(self, limit: int = 25) -> Dict[str, Any]:
        """Run audio song matching across active videos whose songs are currently unidentified."""
        eligible_statuses = {
            VideoStatus.DISCOVERED,
            VideoStatus.DOWNLOADED,
            VideoStatus.CONCERT_MATCHED,
            VideoStatus.PENDING_REVIEW,
        }
        all_videos = self.db.list_videos()
        candidates = [
            v
            for v in all_videos
            if v.processing_status in eligible_statuses
            and not self._is_short_clip(v.duration_seconds)
            and self.is_video_song_unmatched(v.id)
        ][: max(1, limit)]

        if not candidates:
            return {
                "success": True,
                "total_checked": 0,
                "matched_count": 0,
                "no_match_count": 0,
                "error_count": 0,
                "results": [],
                "message": "All active clips already have identified songs.",
            }

        results: List[Dict[str, Any]] = []
        matched_count = 0
        no_match_count = 0
        error_count = 0

        for v in candidates:
            try:
                res = self.match_video_song(v.id)
            except Exception as exc:
                logger.exception("Batch song matching error on %s: %s", v.id, exc)
                res = {
                    "success": False,
                    "matched": False,
                    "outcome": "error",
                    "video_id": v.id,
                    "filename": v.filename,
                    "error": str(exc),
                }
            # Strip non-JSON-serializable raw model object in summary list
            clean_res = {k: val for k, val in res.items() if k != "video"}
            results.append(clean_res)
            outcome = res.get("outcome")
            if outcome == "matched":
                matched_count += 1
            elif outcome == "error":
                error_count += 1
            else:
                no_match_count += 1

        summary_msg = (
            f"Checked {len(results)} clip(s): "
            f"{matched_count} matched, {no_match_count} returned no match"
            + (f", {error_count} failed with errors." if error_count else ".")
        )
        return {
            "success": error_count < len(results),
            "total_checked": len(results),
            "matched_count": matched_count,
            "no_match_count": no_match_count,
            "error_count": error_count,
            "results": results,
            "message": summary_msg,
        }

    # -------------------------------------------------------------------------
    # Publication & Upload Dispatcher
    # -------------------------------------------------------------------------

    def upload_approved_videos(
        self,
        limit: Optional[int] = None,
        privacy_override: Optional[PrivacyStatus] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[UploadResult]:
        """Dispatch upload worker for approved video queue."""
        approved = self.db.list_videos(status=VideoStatus.APPROVED, limit=limit or 10)
        results: List[UploadResult] = []

        for v in approved:
            upload_job = self.db.get_youtube_upload_by_video_id(v.id)
            if not upload_job or not v.file_path:
                continue

            privacy = privacy_override or upload_job.privacy_status
            meta = FormattedYouTubeMetadata(
                title=upload_job.title,
                description=upload_job.description,
                tags=upload_job.tags,
                category_id=upload_job.category_id,
                recording_date=v.recorded_at_utc or v.recorded_at_local,
                latitude=v.latitude,
                longitude=v.longitude,
            )

            self.db.update_video_status(v.id, VideoStatus.UPLOADING)

            res = self.uploader.upload_file(
                video_id=v.id,
                file_path=v.file_path,
                metadata=meta,
                privacy=privacy,
                progress_callback=progress_callback,
                existing_resumable_uri=upload_job.upload_resumable_uri,
            )

            if res.success:
                self.db.update_video_status(v.id, VideoStatus.PUBLISHED)
            else:
                self.db.update_video_status(v.id, VideoStatus.FAILED)

            results.append(res)

        return results
