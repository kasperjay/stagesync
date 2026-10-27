"""StageSync Pipeline Orchestration Coordinator.

Connects Ingestion -> Metadata Parsing -> Geospatial Intelligence ->
Concert Matching -> Audio Fingerprinting -> Review Queue -> YouTube Distribution.
Tracks execution telemetry in the pipeline_runs audit table.
"""

from __future__ import annotations

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
from stagesync.ingestion.google_drive import GoogleDriveClient, is_likely_video_file
from stagesync.ingestion.google_photos import GooglePhotosClient
from stagesync.ingestion.metadata_parser import MetadataParser, ParsedVideoMetadata
from stagesync.intelligence.ai_audio_judge import AIAudioJudge
from stagesync.intelligence.audio_extractor import AudioExtractor
from stagesync.intelligence.geocoder import ReverseGeocoder
from stagesync.intelligence.setlist_fm import MatchedConcert, SetlistFmClient
from stagesync.intelligence.shazam_matcher import ShazamMatcher
from stagesync.publisher.metadata_gen import MetadataGenerator
from stagesync.publisher.resumable_upload import ResumableUploader, UploadResult
from stagesync.security import sanitize_filename, validate_safe_path

logger = logging.getLogger(__name__)


class PipelineCoordinator:
    """Master pipeline orchestrator for StageSync."""

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
        self.audio_extractor = AudioExtractor()
        self.geocoder = ReverseGeocoder(db=self.db)
        self.setlist_client = SetlistFmClient()
        self.shazam_matcher = ShazamMatcher()
        self.ai_audio_judge = AIAudioJudge()

        # Publisher components
        self.metadata_gen = MetadataGenerator()
        self.uploader = ResumableUploader(db=self.db)

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

            video = Video(
                source_photo_id=media_id,
                filename=clean_filename,
                mime_type=item.get("mimeType", "video/mp4"),
                width=int(metadata_raw.get("width", 0)) or None,
                height=int(metadata_raw.get("height", 0)) or None,
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

            video = Video(
                source_photo_id=media_id,
                filename=clean_filename,
                mime_type=item.get("mimeType", "video/mp4"),
                width=int(metadata_raw.get("width", 0)) or None,
                height=int(metadata_raw.get("height", 0)) or None,
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
        logger.info(
            "Scanning Google Drive (start_date=%s, end_date=%s, days=%s, folder_id=%s, dry_run=%s)",
            start_date,
            end_date,
            days,
            folder_id,
            dry_run,
        )
        discovered_videos: List[Video] = []

        try:
            items, _ = self.drive_client.list_videos(
                start_date=start_date,
                end_date=end_date,
                days=days,
                folder_id=folder_id,
                page_size=limit,
            )
        except Exception as e:
            logger.error("Failed to query Google Drive: %s", e)
            raise RuntimeError(f"Google Drive query failed: {e}") from e

        for item in items:
            file_id = item.get("id")
            if not file_id:
                continue

            source_id = f"drive_{file_id}"
            existing = self.db.get_video_by_source_id(source_id)
            if existing:
                continue

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

            lat = float(loc.get("latitude")) if loc.get("latitude") is not None else None
            lng = float(loc.get("longitude")) if loc.get("longitude") is not None else None

            video = Video(
                source_photo_id=source_id,
                filename=clean_filename,
                mime_type=mime_type,
                width=width,
                height=height,
                duration_seconds=duration_sec,
                latitude=lat,
                longitude=lng,
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
                saved = self.db.save_video(v)
                found_videos.append(saved)

        return found_videos

    # -------------------------------------------------------------------------
    # Core Intelligence Pipeline
    # -------------------------------------------------------------------------

    def process_video(self, video: Video) -> Video:
        """Execute end-to-end intelligence pipeline on a single video entity."""
        logger.info("Starting processing pipeline for video %s (%s)", video.id, video.filename)

        if not video.file_path or not Path(video.file_path).exists():
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
            video.latitude = parsed_meta.latitude
            video.longitude = parsed_meta.longitude
            video.altitude_meters = parsed_meta.altitude_meters
            video.device_make = parsed_meta.device_make
            video.device_model = parsed_meta.device_model
            video.is_concert_candidate = parsed_meta.is_concert_candidate
            video.concert_confidence_score = parsed_meta.concert_confidence_score
            video.processing_status = VideoStatus.ANALYZING_METADATA
            self.db.save_video(video)

        # ---------------------------------------------------------------------
        # Step 2: Reverse Geocoding (Nominatim)
        # ---------------------------------------------------------------------
        resolved_venue: Optional[Venue] = None

        if video.latitude is not None and video.longitude is not None:
            def step_geo():
                nonlocal resolved_venue
                loc = self.geocoder.reverse_geocode(video.latitude, video.longitude)
                # Save venue entity
                venue = Venue(
                    name=loc.venue_name or loc.city or "Live Venue",
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
                nonlocal matched_concert
                res: Optional[MatchedConcert] = None
                if video.latitude is not None and video.longitude is not None:
                    res = self.setlist_client.search_by_geo_and_date(
                        event_date=event_date_str,
                        latitude=video.latitude,
                        longitude=video.longitude,
                        radius_km=10,
                    )
                if not res and resolved_venue and resolved_venue.city:
                    res = self.setlist_client.search_by_city_and_date(
                        event_date=event_date_str,
                        city_name=resolved_venue.city,
                    )

                if res:
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
        audio_clip_path: Optional[Path] = None
        try:
            audio_clip_path = self.audio_extractor.extract_clip(
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

        if audio_clip_path and audio_clip_path.exists():
            # Tier 1: Shazamio
            def step_shazam():
                nonlocal detected_song, detected_artist, detected_album, confidence, raw_payload
                shazam_res = self.shazam_matcher.recognize(audio_clip_path)
                if shazam_res.matched:
                    detected_song = shazam_res.song_title
                    detected_artist = shazam_res.artist
                    detected_album = shazam_res.album
                    confidence = shazam_res.confidence
                    raw_payload = shazam_res.raw_payload
                    return StepStatus.SUCCESS, shazam_res.model_dump(), None
                return StepStatus.SKIPPED, {"message": "No Shazam match"}, None

            self._audit_step(video.id, PipelineStep.AUDIO_FINGERPRINT, step_shazam)

            # Check if Shazam match aligns with Setlist
            if matched_concert and detected_song:
                candidate_titles = matched_concert.setlist_data.get("sets", {}).get("set", [])
                for s in candidate_titles:
                    for s_item in s.get("song", []):
                        if s_item.get("name", "").lower() == detected_song.lower():
                            setlist_verified = True
                            confidence = max(confidence, 0.95)
                            break

            # Tier 2: Gemini Setlist-Constrained Audio AI (if unverified or low confidence)
            candidate_song_list = []
            if matched_concert and matched_concert.setlist_data:
                for s in matched_concert.setlist_data.get("sets", {}).get("set", []):
                    for song_entry in s.get("song", []):
                        if song_entry.get("name"):
                            candidate_song_list.append(song_entry.get("name"))

            if (not setlist_verified or confidence < 0.85) and candidate_song_list:
                def step_llm():
                    nonlocal detected_song, detected_artist, confidence, service_used
                    nonlocal setlist_verified, track_number, lyrics_snippet, raw_payload
                    ai_res = self.ai_audio_judge.evaluate_live_clip(
                        audio_path=audio_clip_path,
                        candidate_songs=candidate_song_list,
                        artist_name=matched_concert.headliner_artist,
                        venue_name=resolved_venue.name if resolved_venue else "Concert Venue",
                        event_date=matched_concert.event_date,
                    )
                    if ai_res.matched:
                        detected_song = ai_res.song_title
                        detected_artist = matched_concert.headliner_artist
                        confidence = ai_res.confidence
                        service_used = "gemini_multimodal"
                        setlist_verified = True
                        track_number = ai_res.setlist_index
                        lyrics_snippet = ai_res.lyrics_detected
                        raw_payload = ai_res.raw_payload
                        return StepStatus.SUCCESS, ai_res.model_dump(), None
                    return StepStatus.SKIPPED, {"message": "AI could not disambiguate"}, None

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
        if confidence >= self.settings.auto_approve_threshold:
            video.processing_status = VideoStatus.APPROVED
        elif confidence >= 0.50 or (matched_concert and video.is_concert_candidate):
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

    def process_pending_videos(self, limit: int = 50) -> List[Video]:
        """Process discovered or downloaded videos through the pipeline."""
        candidates = self.db.list_videos(status=VideoStatus.DOWNLOADED, limit=limit)
        if not candidates:
            candidates = self.db.list_videos(status=VideoStatus.DISCOVERED, limit=limit)

        processed = []
        for v in candidates:
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
