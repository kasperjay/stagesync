"""Database Connection and Repository Module for StageSync.

Manages SQLite connection with WAL mode enabled, enforces foreign keys,
initializes tables per ERD, and provides thread-safe parameterized CRUD helpers.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from stagesync.config import get_settings
from stagesync.security import enforce_file_permissions
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

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. Venues Table
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    formatted_address TEXT,
    city TEXT NOT NULL,
    state_province TEXT,
    country_code TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    osm_id TEXT UNIQUE,
    setlist_fm_venue_id TEXT UNIQUE,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_venues_geo ON venues(latitude, longitude);

-- 2. Concerts Table
CREATE TABLE IF NOT EXISTS concerts (
    id TEXT PRIMARY KEY,
    venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
    headliner_artist TEXT NOT NULL,
    lineup_artists TEXT DEFAULT '[]' NOT NULL,
    tour_name TEXT,
    event_date TEXT NOT NULL,
    setlist_fm_event_id TEXT UNIQUE,
    setlist_data TEXT DEFAULT '{}' NOT NULL,
    source_url TEXT,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concerts_date ON concerts(event_date);
CREATE INDEX IF NOT EXISTS idx_concerts_artist ON concerts(headliner_artist);

-- 3. Videos Table
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    source_photo_id TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    file_path TEXT,
    file_hash_sha256 TEXT,
    mime_type TEXT DEFAULT 'video/mp4' NOT NULL,
    file_size_bytes INTEGER,
    duration_seconds REAL,
    width INTEGER,
    height INTEGER,
    recorded_at_utc TEXT,
    recorded_at_local TEXT,
    timezone_offset_minutes INTEGER,
    latitude REAL,
    longitude REAL,
    altitude_meters REAL,
    device_make TEXT,
    device_model TEXT,
    is_concert_candidate INTEGER DEFAULT 0 NOT NULL,
    concert_confidence_score REAL DEFAULT 0.0 NOT NULL,
    processing_status TEXT DEFAULT 'DISCOVERED' NOT NULL,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(processing_status);
CREATE INDEX IF NOT EXISTS idx_videos_candidate ON videos(is_concert_candidate);

-- 4. Identifications Table
CREATE TABLE IF NOT EXISTS identifications (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    concert_id TEXT REFERENCES concerts(id) ON DELETE SET NULL,
    detected_song_title TEXT NOT NULL,
    detected_artist TEXT NOT NULL,
    album_name TEXT,
    match_confidence REAL NOT NULL,
    fingerprint_service TEXT NOT NULL,
    setlist_verified INTEGER DEFAULT 0 NOT NULL,
    track_number_in_setlist INTEGER,
    identified_lyrics_snippet TEXT,
    raw_service_payload TEXT DEFAULT '{}' NOT NULL,
    status TEXT DEFAULT 'CONFIRMED' NOT NULL,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identifications_video ON identifications(video_id);

-- 5. YouTube Uploads Table
CREATE TABLE IF NOT EXISTS youtube_uploads (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL UNIQUE REFERENCES videos(id) ON DELETE CASCADE,
    youtube_video_id TEXT UNIQUE,
    youtube_url TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT DEFAULT '[]' NOT NULL,
    category_id TEXT DEFAULT '10' NOT NULL,
    privacy_status TEXT DEFAULT 'unlisted' NOT NULL,
    upload_status TEXT DEFAULT 'DRAFT' NOT NULL,
    bytes_uploaded INTEGER DEFAULT 0 NOT NULL,
    total_bytes INTEGER DEFAULT 0 NOT NULL,
    upload_resumable_uri TEXT,
    uploaded_at TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_youtube_uploads_status ON youtube_uploads(upload_status);

-- 6. Pipeline Runs Audit Table
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    status TEXT DEFAULT 'PENDING' NOT NULL,
    attempt_count INTEGER DEFAULT 1 NOT NULL,
    duration_ms INTEGER,
    input_payload TEXT DEFAULT '{}' NOT NULL,
    output_payload TEXT DEFAULT '{}' NOT NULL,
    error_log TEXT,
    started_at TEXT DEFAULT (datetime('now')) NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_video ON pipeline_runs(video_id);

-- 7. Geocode Cache Table (TTL Caching for Nominatim)
CREATE TABLE IF NOT EXISTS geocode_cache (
    cache_key TEXT PRIMARY KEY,
    display_name TEXT,
    venue_name TEXT,
    city TEXT,
    state_province TEXT,
    country_code TEXT,
    latitude REAL,
    longitude REAL,
    osm_id TEXT,
    raw_json TEXT DEFAULT '{}' NOT NULL,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class Database:
    """SQLite Database manager with WAL mode and repository access."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            settings = get_settings()
            self.db_path = settings.database_path
        else:
            self.db_path = Path(db_path)

        self._local = threading.local()
        self._lock = threading.Lock()
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Obtain a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            self._local.conn = conn
        return self._local.conn

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager providing an atomic database transaction."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def init_db(self) -> None:
        """Initialize database tables according to ERD specifications."""
        with self._lock:
            conn = self._get_connection()
            with conn:
                conn.executescript(_SCHEMA_SQL)
            if str(self.db_path) != ":memory:" and self.db_path.exists():
                enforce_file_permissions(self.db_path, 0o600)

    # -------------------------------------------------------------------------
    # Video CRUD Operations
    # -------------------------------------------------------------------------

    def save_video(self, video: Video) -> Video:
        """Insert or replace video record."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sql = """
        INSERT INTO videos (
            id, source_photo_id, filename, file_path, file_hash_sha256,
            mime_type, file_size_bytes, duration_seconds, width, height,
            recorded_at_utc, recorded_at_local, timezone_offset_minutes,
            latitude, longitude, altitude_meters, device_make, device_model,
            is_concert_candidate, concert_confidence_score, processing_status,
            created_at, updated_at
        ) VALUES (
            :id, :source_photo_id, :filename, :file_path, :file_hash_sha256,
            :mime_type, :file_size_bytes, :duration_seconds, :width, :height,
            :recorded_at_utc, :recorded_at_local, :timezone_offset_minutes,
            :latitude, :longitude, :altitude_meters, :device_make, :device_model,
            :is_concert_candidate, :concert_confidence_score, :processing_status,
            :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            filename=excluded.filename,
            file_path=excluded.file_path,
            file_hash_sha256=excluded.file_hash_sha256,
            file_size_bytes=excluded.file_size_bytes,
            duration_seconds=excluded.duration_seconds,
            width=excluded.width,
            height=excluded.height,
            recorded_at_utc=excluded.recorded_at_utc,
            recorded_at_local=excluded.recorded_at_local,
            timezone_offset_minutes=excluded.timezone_offset_minutes,
            latitude=excluded.latitude,
            longitude=excluded.longitude,
            altitude_meters=excluded.altitude_meters,
            device_make=excluded.device_make,
            device_model=excluded.device_model,
            is_concert_candidate=excluded.is_concert_candidate,
            concert_confidence_score=excluded.concert_confidence_score,
            processing_status=excluded.processing_status,
            updated_at=:updated_at
        ON CONFLICT(source_photo_id) DO UPDATE SET
            filename=excluded.filename,
            file_path=excluded.file_path,
            processing_status=excluded.processing_status,
            updated_at=:updated_at;
        """
        params = {
            "id": video.id,
            "source_photo_id": video.source_photo_id,
            "filename": video.filename,
            "file_path": video.file_path,
            "file_hash_sha256": video.file_hash_sha256,
            "mime_type": video.mime_type,
            "file_size_bytes": video.file_size_bytes,
            "duration_seconds": video.duration_seconds,
            "width": video.width,
            "height": video.height,
            "recorded_at_utc": video.recorded_at_utc,
            "recorded_at_local": video.recorded_at_local,
            "timezone_offset_minutes": video.timezone_offset_minutes,
            "latitude": video.latitude,
            "longitude": video.longitude,
            "altitude_meters": video.altitude_meters,
            "device_make": video.device_make,
            "device_model": video.device_model,
            "is_concert_candidate": 1 if video.is_concert_candidate else 0,
            "concert_confidence_score": video.concert_confidence_score,
            "processing_status": video.processing_status.value if isinstance(video.processing_status, VideoStatus) else video.processing_status,
            "created_at": video.created_at,
            "updated_at": now,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)
        video.updated_at = now
        return video

    def get_video(self, video_id: str) -> Optional[Video]:
        """Fetch video by primary key ID."""
        sql = "SELECT * FROM videos WHERE id = :id LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"id": video_id}).fetchone()
        if not row:
            return None
        return self._row_to_video(row)

    def get_video_by_source_id(self, source_photo_id: str) -> Optional[Video]:
        """Fetch video by Google Photos mediaItem ID."""
        sql = "SELECT * FROM videos WHERE source_photo_id = :sid LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"sid": source_photo_id}).fetchone()
        if not row:
            return None
        return self._row_to_video(row)

    def update_video_status(self, video_id: str, status: VideoStatus) -> None:
        """Update processing status for a video."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sql = "UPDATE videos SET processing_status = :status, updated_at = :now WHERE id = :id"
        with self.transaction() as cur:
            cur.execute(sql, {"status": status.value, "now": now, "id": video_id})

    def list_videos(self, status: Optional[VideoStatus] = None, limit: int = 100) -> List[Video]:
        """List videos optionally filtered by processing status."""
        conn = self._get_connection()
        if status:
            sql = "SELECT * FROM videos WHERE processing_status = :status ORDER BY created_at DESC LIMIT :limit"
            rows = conn.execute(sql, {"status": status.value, "limit": limit}).fetchall()
        else:
            sql = "SELECT * FROM videos ORDER BY created_at DESC LIMIT :limit"
            rows = conn.execute(sql, {"limit": limit}).fetchall()
        return [self._row_to_video(r) for r in rows]

    def _row_to_video(self, row: sqlite3.Row) -> Video:
        return Video(
            id=row["id"],
            source_photo_id=row["source_photo_id"],
            filename=row["filename"],
            file_path=row["file_path"],
            file_hash_sha256=row["file_hash_sha256"],
            mime_type=row["mime_type"],
            file_size_bytes=row["file_size_bytes"],
            duration_seconds=row["duration_seconds"],
            width=row["width"],
            height=row["height"],
            recorded_at_utc=row["recorded_at_utc"],
            recorded_at_local=row["recorded_at_local"],
            timezone_offset_minutes=row["timezone_offset_minutes"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            altitude_meters=row["altitude_meters"],
            device_make=row["device_make"],
            device_model=row["device_model"],
            is_concert_candidate=bool(row["is_concert_candidate"]),
            concert_confidence_score=float(row["concert_confidence_score"] or 0.0),
            processing_status=VideoStatus(row["processing_status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Venue Operations
    # -------------------------------------------------------------------------

    def save_venue(self, venue: Venue) -> Venue:
        """Insert or update venue record."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = self._get_connection()
        if venue.osm_id:
            row = conn.execute("SELECT id FROM venues WHERE osm_id = :osm LIMIT 1", {"osm": venue.osm_id}).fetchone()
            if row:
                venue.id = row[0]
        elif venue.setlist_fm_venue_id:
            row = conn.execute("SELECT id FROM venues WHERE setlist_fm_venue_id = :sid LIMIT 1", {"sid": venue.setlist_fm_venue_id}).fetchone()
            if row:
                venue.id = row[0]

        sql = """
        INSERT INTO venues (
            id, name, formatted_address, city, state_province, country_code,
            latitude, longitude, osm_id, setlist_fm_venue_id, created_at, updated_at
        ) VALUES (
            :id, :name, :formatted_address, :city, :state_province, :country_code,
            :latitude, :longitude, :osm_id, :setlist_fm_venue_id, :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            formatted_address=excluded.formatted_address,
            city=excluded.city,
            state_province=excluded.state_province,
            country_code=excluded.country_code,
            latitude=excluded.latitude,
            longitude=excluded.longitude,
            osm_id=COALESCE(excluded.osm_id, venues.osm_id),
            setlist_fm_venue_id=COALESCE(excluded.setlist_fm_venue_id, venues.setlist_fm_venue_id),
            updated_at=:updated_at;
        """
        params = {
            "id": venue.id,
            "name": venue.name,
            "formatted_address": venue.formatted_address,
            "city": venue.city,
            "state_province": venue.state_province,
            "country_code": venue.country_code,
            "latitude": venue.latitude,
            "longitude": venue.longitude,
            "osm_id": venue.osm_id,
            "setlist_fm_venue_id": venue.setlist_fm_venue_id,
            "created_at": venue.created_at,
            "updated_at": now,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)
        venue.updated_at = now
        return venue

    def get_venue(self, venue_id: str) -> Optional[Venue]:
        """Fetch venue by ID."""
        sql = "SELECT * FROM venues WHERE id = :id LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"id": venue_id}).fetchone()
        if not row:
            return None
        return self._row_to_venue(row)

    def get_venue_by_setlist_id(self, setlist_fm_venue_id: str) -> Optional[Venue]:
        """Fetch venue by Setlist.fm venue ID."""
        sql = "SELECT * FROM venues WHERE setlist_fm_venue_id = :sid LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"sid": setlist_fm_venue_id}).fetchone()
        if not row:
            return None
        return self._row_to_venue(row)

    def find_venue_by_coords(self, lat: float, lon: float, tolerance: float = 0.005) -> Optional[Venue]:
        """Find venue within approx coordinate bounding box (~500m)."""
        sql = """
        SELECT * FROM venues
        WHERE latitude BETWEEN :min_lat AND :max_lat
          AND longitude BETWEEN :min_lon AND :max_lon
        ORDER BY ((latitude - :lat) * (latitude - :lat) + (longitude - :lon) * (longitude - :lon)) ASC
        LIMIT 1
        """
        conn = self._get_connection()
        row = conn.execute(
            sql,
            {
                "lat": lat,
                "lon": lon,
                "min_lat": lat - tolerance,
                "max_lat": lat + tolerance,
                "min_lon": lon - tolerance,
                "max_lon": lon + tolerance,
            },
        ).fetchone()
        if not row:
            return None
        return self._row_to_venue(row)

    def _row_to_venue(self, row: sqlite3.Row) -> Venue:
        return Venue(
            id=row["id"],
            name=row["name"],
            formatted_address=row["formatted_address"],
            city=row["city"],
            state_province=row["state_province"],
            country_code=row["country_code"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            osm_id=row["osm_id"],
            setlist_fm_venue_id=row["setlist_fm_venue_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Concert Operations
    # -------------------------------------------------------------------------

    def save_concert(self, concert: Concert) -> Concert:
        """Insert or update concert record."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if concert.setlist_fm_event_id:
            conn = self._get_connection()
            row = conn.execute("SELECT id FROM concerts WHERE setlist_fm_event_id = :sid LIMIT 1", {"sid": concert.setlist_fm_event_id}).fetchone()
            if row:
                concert.id = row[0]

        sql = """
        INSERT INTO concerts (
            id, venue_id, headliner_artist, lineup_artists, tour_name,
            event_date, setlist_fm_event_id, setlist_data, source_url,
            created_at, updated_at
        ) VALUES (
            :id, :venue_id, :headliner_artist, :lineup_artists, :tour_name,
            :event_date, :setlist_fm_event_id, :setlist_data, :source_url,
            :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            venue_id=excluded.venue_id,
            headliner_artist=excluded.headliner_artist,
            lineup_artists=excluded.lineup_artists,
            tour_name=excluded.tour_name,
            event_date=excluded.event_date,
            setlist_fm_event_id=COALESCE(excluded.setlist_fm_event_id, concerts.setlist_fm_event_id),
            setlist_data=excluded.setlist_data,
            source_url=excluded.source_url,
            updated_at=:updated_at;
        """
        params = {
            "id": concert.id,
            "venue_id": concert.venue_id,
            "headliner_artist": concert.headliner_artist,
            "lineup_artists": json.dumps(concert.lineup_artists),
            "tour_name": concert.tour_name,
            "event_date": concert.event_date,
            "setlist_fm_event_id": concert.setlist_fm_event_id,
            "setlist_data": json.dumps(concert.setlist_data),
            "source_url": concert.source_url,
            "created_at": concert.created_at,
            "updated_at": now,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)
        concert.updated_at = now
        return concert

    def get_concert(self, concert_id: str) -> Optional[Concert]:
        """Fetch concert by ID."""
        sql = "SELECT * FROM concerts WHERE id = :id LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"id": concert_id}).fetchone()
        if not row:
            return None
        return self._row_to_concert(row)

    def get_concert_by_setlist_id(self, setlist_fm_event_id: str) -> Optional[Concert]:
        """Fetch concert by Setlist.fm event ID."""
        sql = "SELECT * FROM concerts WHERE setlist_fm_event_id = :sid LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"sid": setlist_fm_event_id}).fetchone()
        if not row:
            return None
        return self._row_to_concert(row)

    def _row_to_concert(self, row: sqlite3.Row) -> Concert:
        lineup = []
        if row["lineup_artists"]:
            with contextlib.suppress(Exception):
                lineup = json.loads(row["lineup_artists"])
        setlist_data = {}
        if row["setlist_data"]:
            with contextlib.suppress(Exception):
                setlist_data = json.loads(row["setlist_data"])
        return Concert(
            id=row["id"],
            venue_id=row["venue_id"],
            headliner_artist=row["headliner_artist"],
            lineup_artists=lineup,
            tour_name=row["tour_name"],
            event_date=row["event_date"],
            setlist_fm_event_id=row["setlist_fm_event_id"],
            setlist_data=setlist_data,
            source_url=row["source_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Identification Operations
    # -------------------------------------------------------------------------

    def save_identification(self, ident: Identification) -> Identification:
        """Insert or replace identification record."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sql = """
        INSERT INTO identifications (
            id, video_id, concert_id, detected_song_title, detected_artist,
            album_name, match_confidence, fingerprint_service, setlist_verified,
            track_number_in_setlist, identified_lyrics_snippet, raw_service_payload,
            status, created_at, updated_at
        ) VALUES (
            :id, :video_id, :concert_id, :detected_song_title, :detected_artist,
            :album_name, :match_confidence, :fingerprint_service, :setlist_verified,
            :track_number_in_setlist, :identified_lyrics_snippet, :raw_service_payload,
            :status, :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            concert_id=excluded.concert_id,
            detected_song_title=excluded.detected_song_title,
            detected_artist=excluded.detected_artist,
            album_name=excluded.album_name,
            match_confidence=excluded.match_confidence,
            fingerprint_service=excluded.fingerprint_service,
            setlist_verified=excluded.setlist_verified,
            track_number_in_setlist=excluded.track_number_in_setlist,
            identified_lyrics_snippet=excluded.identified_lyrics_snippet,
            raw_service_payload=excluded.raw_service_payload,
            status=excluded.status,
            updated_at=:updated_at;
        """
        params = {
            "id": ident.id,
            "video_id": ident.video_id,
            "concert_id": ident.concert_id,
            "detected_song_title": ident.detected_song_title,
            "detected_artist": ident.detected_artist,
            "album_name": ident.album_name,
            "match_confidence": ident.match_confidence,
            "fingerprint_service": ident.fingerprint_service,
            "setlist_verified": 1 if ident.setlist_verified else 0,
            "track_number_in_setlist": ident.track_number_in_setlist,
            "identified_lyrics_snippet": ident.identified_lyrics_snippet,
            "raw_service_payload": json.dumps(ident.raw_service_payload),
            "status": ident.status,
            "created_at": ident.created_at,
            "updated_at": now,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)
        ident.updated_at = now
        return ident

    def get_identification_by_video_id(self, video_id: str) -> Optional[Identification]:
        """Fetch identification associated with a video."""
        sql = "SELECT * FROM identifications WHERE video_id = :vid ORDER BY match_confidence DESC LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"vid": video_id}).fetchone()
        if not row:
            return None
        return self._row_to_identification(row)

    def _row_to_identification(self, row: sqlite3.Row) -> Identification:
        raw_payload = {}
        if row["raw_service_payload"]:
            with contextlib.suppress(Exception):
                raw_payload = json.loads(row["raw_service_payload"])
        return Identification(
            id=row["id"],
            video_id=row["video_id"],
            concert_id=row["concert_id"],
            detected_song_title=row["detected_song_title"],
            detected_artist=row["detected_artist"],
            album_name=row["album_name"],
            match_confidence=float(row["match_confidence"]),
            fingerprint_service=row["fingerprint_service"],
            setlist_verified=bool(row["setlist_verified"]),
            track_number_in_setlist=row["track_number_in_setlist"],
            identified_lyrics_snippet=row["identified_lyrics_snippet"],
            raw_service_payload=raw_payload,
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # YouTube Upload Operations
    # -------------------------------------------------------------------------

    def save_youtube_upload(self, upload: YouTubeUpload) -> YouTubeUpload:
        """Insert or update YouTube upload record."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sql = """
        INSERT INTO youtube_uploads (
            id, video_id, youtube_video_id, youtube_url, title, description,
            tags, category_id, privacy_status, upload_status, bytes_uploaded,
            total_bytes, upload_resumable_uri, uploaded_at, error_message,
            created_at, updated_at
        ) VALUES (
            :id, :video_id, :youtube_video_id, :youtube_url, :title, :description,
            :tags, :category_id, :privacy_status, :upload_status, :bytes_uploaded,
            :total_bytes, :upload_resumable_uri, :uploaded_at, :error_message,
            :created_at, :updated_at
        )
        ON CONFLICT(video_id) DO UPDATE SET
            youtube_video_id=COALESCE(excluded.youtube_video_id, youtube_uploads.youtube_video_id),
            youtube_url=COALESCE(excluded.youtube_url, youtube_uploads.youtube_url),
            title=excluded.title,
            description=excluded.description,
            tags=excluded.tags,
            privacy_status=excluded.privacy_status,
            upload_status=excluded.upload_status,
            bytes_uploaded=excluded.bytes_uploaded,
            total_bytes=excluded.total_bytes,
            upload_resumable_uri=excluded.upload_resumable_uri,
            uploaded_at=COALESCE(excluded.uploaded_at, youtube_uploads.uploaded_at),
            error_message=excluded.error_message,
            updated_at=:updated_at;
        """
        params = {
            "id": upload.id,
            "video_id": upload.video_id,
            "youtube_video_id": upload.youtube_video_id,
            "youtube_url": upload.youtube_url,
            "title": upload.title,
            "description": upload.description,
            "tags": json.dumps(upload.tags),
            "category_id": upload.category_id,
            "privacy_status": upload.privacy_status.value if isinstance(upload.privacy_status, PrivacyStatus) else upload.privacy_status,
            "upload_status": upload.upload_status,
            "bytes_uploaded": upload.bytes_uploaded,
            "total_bytes": upload.total_bytes,
            "upload_resumable_uri": upload.upload_resumable_uri,
            "uploaded_at": upload.uploaded_at,
            "error_message": upload.error_message,
            "created_at": upload.created_at,
            "updated_at": now,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)
        upload.updated_at = now
        return upload

    def get_youtube_upload_by_video_id(self, video_id: str) -> Optional[YouTubeUpload]:
        """Fetch upload job by video ID."""
        sql = "SELECT * FROM youtube_uploads WHERE video_id = :vid LIMIT 1"
        conn = self._get_connection()
        row = conn.execute(sql, {"vid": video_id}).fetchone()
        if not row:
            return None
        return self._row_to_upload(row)

    def get_todays_upload_count(self) -> int:
        """Count how many videos were uploaded in the past 24 hours."""
        sql = """
        SELECT COUNT(*) as cnt FROM youtube_uploads
        WHERE upload_status = 'COMPLETED'
          AND uploaded_at >= datetime('now', '-24 hours')
        """
        conn = self._get_connection()
        row = conn.execute(sql).fetchone()
        return int(row["cnt"]) if row else 0

    def _row_to_upload(self, row: sqlite3.Row) -> YouTubeUpload:
        tags = []
        if row["tags"]:
            with contextlib.suppress(Exception):
                tags = json.loads(row["tags"])
        return YouTubeUpload(
            id=row["id"],
            video_id=row["video_id"],
            youtube_video_id=row["youtube_video_id"],
            youtube_url=row["youtube_url"],
            title=row["title"],
            description=row["description"],
            tags=tags,
            category_id=row["category_id"],
            privacy_status=PrivacyStatus(row["privacy_status"]),
            upload_status=row["upload_status"],
            bytes_uploaded=row["bytes_uploaded"] or 0,
            total_bytes=row["total_bytes"] or 0,
            upload_resumable_uri=row["upload_resumable_uri"],
            uploaded_at=row["uploaded_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Pipeline Audit Runs
    # -------------------------------------------------------------------------

    def log_pipeline_run(self, run: PipelineRun) -> PipelineRun:
        """Insert or update pipeline audit step."""
        sql = """
        INSERT INTO pipeline_runs (
            id, video_id, step_name, status, attempt_count, duration_ms,
            input_payload, output_payload, error_log, started_at, completed_at
        ) VALUES (
            :id, :video_id, :step_name, :status, :attempt_count, :duration_ms,
            :input_payload, :output_payload, :error_log, :started_at, :completed_at
        )
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            attempt_count=excluded.attempt_count,
            duration_ms=excluded.duration_ms,
            output_payload=excluded.output_payload,
            error_log=excluded.error_log,
            completed_at=excluded.completed_at;
        """
        params = {
            "id": run.id,
            "video_id": run.video_id,
            "step_name": run.step_name.value if isinstance(run.step_name, PipelineStep) else run.step_name,
            "status": run.status.value if isinstance(run.status, StepStatus) else run.status,
            "attempt_count": run.attempt_count,
            "duration_ms": run.duration_ms,
            "input_payload": json.dumps(run.input_payload),
            "output_payload": json.dumps(run.output_payload),
            "error_log": run.error_log,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)
        return run

    # -------------------------------------------------------------------------
    # Geocode Cache Operations
    # -------------------------------------------------------------------------

    def get_cached_geocode(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve active geocode result from local TTL cache."""
        sql = """
        SELECT * FROM geocode_cache
        WHERE cache_key = :k AND expires_at > datetime('now')
        LIMIT 1
        """
        conn = self._get_connection()
        row = conn.execute(sql, {"k": cache_key}).fetchone()
        if not row:
            return None
        raw = {}
        if row["raw_json"]:
            with contextlib.suppress(Exception):
                raw = json.loads(row["raw_json"])
        return {
            "display_name": row["display_name"],
            "venue_name": row["venue_name"],
            "city": row["city"],
            "state_province": row["state_province"],
            "country_code": row["country_code"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "osm_id": row["osm_id"],
            "raw": raw,
        }

    def set_cached_geocode(
        self,
        cache_key: str,
        data: Dict[str, Any],
        ttl_days: int = 30,
    ) -> None:
        """Cache reverse geocoding result with expiration."""
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now + datetime.timedelta(days=ttl_days)).isoformat()
        sql = """
        INSERT INTO geocode_cache (
            cache_key, display_name, venue_name, city, state_province,
            country_code, latitude, longitude, osm_id, raw_json,
            created_at, expires_at
        ) VALUES (
            :cache_key, :display_name, :venue_name, :city, :state_province,
            :country_code, :latitude, :longitude, :osm_id, :raw_json,
            :created_at, :expires_at
        )
        ON CONFLICT(cache_key) DO UPDATE SET
            display_name=excluded.display_name,
            venue_name=excluded.venue_name,
            city=excluded.city,
            state_province=excluded.state_province,
            country_code=excluded.country_code,
            latitude=excluded.latitude,
            longitude=excluded.longitude,
            osm_id=excluded.osm_id,
            raw_json=excluded.raw_json,
            expires_at=excluded.expires_at;
        """
        params = {
            "cache_key": cache_key,
            "display_name": data.get("display_name"),
            "venue_name": data.get("venue_name"),
            "city": data.get("city"),
            "state_province": data.get("state_province"),
            "country_code": data.get("country_code"),
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "osm_id": str(data.get("osm_id") or ""),
            "raw_json": json.dumps(data.get("raw", {})),
            "created_at": now.isoformat(),
            "expires_at": expires_at,
        }
        with self.transaction() as cur:
            cur.execute(sql, params)

    # -------------------------------------------------------------------------
    # Composite Review Query
    # -------------------------------------------------------------------------

    def get_review_queue(self) -> List[Tuple[Video, Optional[Identification], Optional[Concert], Optional[Venue]]]:
        """Fetch all PENDING_REVIEW videos along with correlated metadata."""
        videos = self.list_videos(status=VideoStatus.PENDING_REVIEW)
        results = []
        for vid in videos:
            ident = self.get_identification_by_video_id(vid.id)
            concert = None
            venue = None
            if ident and ident.concert_id:
                concert = self.get_concert(ident.concert_id)
            if concert and concert.venue_id:
                venue = self.get_venue(concert.venue_id)
            elif vid.latitude and vid.longitude:
                venue = self.find_venue_by_coords(vid.latitude, vid.longitude)
            results.append((vid, ident, concert, venue))
        return results


_DB_INSTANCE: Optional[Database] = None


def get_db(db_path: Optional[Union[str, Path]] = None) -> Database:
    """Retrieve or initialize singleton Database repository."""
    global _DB_INSTANCE
    if _DB_INSTANCE is None or (db_path and Path(db_path) != _DB_INSTANCE.db_path):
        _DB_INSTANCE = Database(db_path)
    return _DB_INSTANCE
