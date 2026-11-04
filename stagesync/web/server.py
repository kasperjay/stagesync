"""StageSync Web Dashboard & REST Server.

Provides a modern, lightweight web interface to monitor video discovery,
review identified concert footage, inspect setlists, and approve YouTube uploads.
Redesigned with a Figma Task & Project Management Dashboard layout (Kanban boards,
review triage queue, catalog data grid, and slide-over inspector drawer).
Powered by aiohttp.web with zero node/npm build dependencies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import mimetypes
from pathlib import Path
import threading
import uuid
from typing import Any, Dict, Optional
import webbrowser

from aiohttp import web

from stagesync.config import Settings, get_settings
from stagesync.database.connection import Database, get_db
from stagesync.database.models import PrivacyStatus, VideoStatus
from stagesync.intelligence.thumbnail_extractor import ThumbnailExtractor
from stagesync.intelligence.setlist_fm import MatchedConcert
from stagesync.pipeline import PipelineCoordinator

logger = logging.getLogger(__name__)

SETTINGS_KEY = web.AppKey("settings", Settings)
DB_KEY = web.AppKey("db", Database)
COORD_KEY = web.AppKey("coordinator", PipelineCoordinator)


@dataclass
class ScanJob:
    """Track progress and result state for one background discovery scan."""

    scan_id: str
    mode: str
    status: str = "running"
    current: int = 0
    total: int = 0
    message: str = "Starting scan..."
    scanned_count: int = 0
    error: Optional[str] = None
    cancel_requested: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, current: int, total: int, message: str) -> None:
        with self._lock:
            self.current = max(0, current)
            self.total = max(0, total)
            self.message = message

    def finish(self, scanned_count: int) -> None:
        with self._lock:
            self.status = "completed"
            self.scanned_count = scanned_count
            self.message = f"Discovery complete — found {scanned_count} candidate(s)"

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = "failed"
            self.error = error
            self.message = "Discovery failed"

    def request_cancel(self) -> bool:
        with self._lock:
            if self.status != "running":
                return False
            self.cancel_requested = True
            self.status = "cancelling"
            self.message = "Stopping scan..."
            return True

    def cancel(self) -> None:
        with self._lock:
            self.status = "cancelled"
            self.message = "Scan cancelled"

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "scan_id": self.scan_id,
                "mode": self.mode,
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "message": self.message,
                "scanned_count": self.scanned_count,
                "error": self.error,
                "cancel_requested": self.cancel_requested,
            }


class ScanState:
    """Coordinate active discovery scans and retain recent job state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.jobs: Dict[str, ScanJob] = {}
        self.active_scan_id: Optional[str] = None

    def start(self, mode: str) -> Optional[ScanJob]:
        with self._lock:
            if self.active_scan_id:
                active = self.jobs.get(self.active_scan_id)
                if active and active.status in {"running", "cancelling"}:
                    return None
            scan_id = uuid.uuid4().hex
            job = ScanJob(scan_id=scan_id, mode=mode)
            self.jobs[scan_id] = job
            self.active_scan_id = scan_id
            return job

    def finish(self, scan_id: str) -> None:
        with self._lock:
            if self.active_scan_id == scan_id:
                self.active_scan_id = None


class ScanCancelled(Exception):
    """Raised at a cooperative scan checkpoint after the user cancels."""


SCAN_STATE_KEY = web.AppKey("scan_state", ScanState)

INDEX_HTML_PATH = Path(__file__).parent / "index.html"


def _get_html_page() -> str:
    """Load the single-page dashboard HTML from disk."""
    if INDEX_HTML_PATH.exists():
        return INDEX_HTML_PATH.read_text(encoding="utf-8")
    return "<!DOCTYPE html><html><body><h1>StageSync UI loading...</h1></body></html>"


HTML_PAGE = _get_html_page()


@web.middleware
async def _api_json_error_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    """Guarantee JSON error payloads for all /api/* routes instead of plain-text 500/4xx."""
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        return await handler(request)
    except web.HTTPException as http_exc:
        if http_exc.status >= 400 and http_exc.content_type != "application/json":
            return web.json_response(
                {"success": False, "error": http_exc.reason or str(http_exc)},
                status=http_exc.status,
            )
        raise
    except Exception as exc:
        logger.exception("Unhandled API error on %s %s: %s", request.method, request.path, exc)
        return web.json_response(
            {"success": False, "error": str(exc)},
            status=500,
        )


def create_app(
    coordinator: Optional[PipelineCoordinator] = None,
    db: Optional[Database] = None,
    settings: Optional[Settings] = None,
) -> web.Application:
    """Construct and configure the aiohttp web application."""
    app = web.Application(middlewares=[_api_json_error_middleware])
    app[SETTINGS_KEY] = settings or get_settings()
    app[DB_KEY] = db or get_db()
    app[COORD_KEY] = coordinator or PipelineCoordinator(db=app[DB_KEY], settings=app[SETTINGS_KEY])
    app[SCAN_STATE_KEY] = ScanState()

    # Static assets route
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.router.add_static("/static", str(static_dir))

    # Register Routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/workspace", handle_workspace)
    app.router.add_get("/api/status", handle_get_status)
    app.router.add_get("/api/scan/{scan_id}", handle_get_scan)
    app.router.add_post("/api/scan/{scan_id}/cancel", handle_cancel_scan)
    app.router.add_get("/api/videos", handle_list_videos)
    app.router.add_get("/api/video/{id}", handle_get_video)
    app.router.add_get("/api/video/{id}/thumbnail", handle_video_thumbnail)
    app.router.add_get("/api/video/{id}/audio", handle_video_audio)
    app.router.add_get("/api/video/{id}/song-options", handle_song_options)
    app.router.add_get("/api/venues/search", handle_venue_search)
    app.router.add_post("/api/video/{id}/setlist-search", handle_setlist_search)
    app.router.add_post("/api/video/{id}/setlist-match", handle_setlist_match)
    app.router.add_post("/api/video/{id}/approve", handle_approve_video)
    app.router.add_post("/api/video/{id}/confirm-suggested-match", handle_confirm_suggested_match)
    app.router.add_post("/api/video/{id}/reject", handle_reject_video)
    app.router.add_delete("/api/video/{id}", handle_delete_video)
    app.router.add_post("/api/video/{id}/set-venue", handle_set_video_venue)
    app.router.add_post("/api/videos/apply-venue-to-date", handle_apply_venue_to_date)
    app.router.add_post("/api/video/{id}/set-band", handle_set_video_band)
    app.router.add_post("/api/video/{id}/set-song", handle_set_video_song)
    app.router.add_post("/api/video/{id}/match", handle_trigger_video_match)
    app.router.add_post("/api/match-songs", handle_trigger_batch_song_match)
    app.router.add_post("/api/scan", handle_trigger_scan)
    app.router.add_post("/api/picker/create", handle_picker_create)
    app.router.add_post("/api/picker/poll", handle_picker_poll)
    app.router.add_post("/api/process", handle_trigger_process)
    app.router.add_post("/api/upload", handle_trigger_upload)

    return app


async def handle_index(request: web.Request) -> web.Response:
    """Serve the single-page dashboard HTML."""
    return web.Response(
        text=_get_html_page(),
        content_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def handle_workspace(request: web.Request) -> web.Response:
    return web.Response(
        text=(Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def handle_get_status(request: web.Request) -> web.Response:
    """Return JSON system status, integration health, and quotas."""
    db: Database = request.app[DB_KEY]
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    settings: Settings = request.app[SETTINGS_KEY]

    all_videos = db.list_videos(limit=-1)
    status_counts: Dict[str, int] = {}
    for v in all_videos:
        st = v.processing_status.value
        status_counts[st] = status_counts.get(st, 0) + 1

    active_videos = [v for v in all_videos if v.processing_status != VideoStatus.REJECTED]

    uploads_today = db.get_todays_upload_count()

    payload = {
        "total_videos": len(active_videos),
        "status_counts": status_counts,
        "uploads_today": uploads_today,
        "max_daily_uploads": settings.max_daily_uploads,
        "services": {
            "google_drive": {
                "authenticated": bool(coordinator.drive_client.is_authenticated()),
            },
            "google_photos": {
                "authenticated": bool(coordinator.photos_client.is_authenticated()),
            },
            "youtube": {
                "authenticated": bool(coordinator.uploader.auth.is_authenticated()),
            },
            "setlist_fm": {
                "has_key": bool(settings.setlist_fm_api_key),
            },
            "gemini_audio": {
                "has_key": bool(settings.gemini_api_key),
            },
        },
    }
    return web.json_response(payload)


def _run_scan_job(
    coordinator: PipelineCoordinator,
    job: ScanJob,
    payload: Dict[str, Any],
) -> None:
    def report(current: int, total: int, message: str) -> None:
        if job.cancel_requested:
            raise ScanCancelled()
        job.update(current, total, message)

    try:
        directory = payload.get("directory")
        if directory:
            discovered = coordinator.scan_local_directory(directory, progress_callback=report)
        elif payload.get("provider") in ("google_drive", "drive") or payload.get("drive"):
            discovered = coordinator.scan_google_drive(
                start_date=payload.get("start_date"),
                end_date=payload.get("end_date"),
                days=payload.get("days"),
                folder_id=payload.get("folder_id"),
                limit=payload.get("limit", 50),
                dry_run=payload.get("dry_run", False),
                progress_callback=report,
            )
        else:
            discovered = coordinator.scan_google_photos(
                start_date=payload.get("start_date"),
                end_date=payload.get("end_date"),
                days=payload.get("days"),
                limit=payload.get("limit", 50),
                dry_run=payload.get("dry_run", False),
                progress_callback=report,
            )
        if job.cancel_requested:
            raise ScanCancelled()
        job.finish(len(discovered))
    except ScanCancelled:
        job.cancel()
    except Exception as exc:
        logger.exception("Scan job %s failed: %s", job.scan_id, exc)
        job.fail(str(exc))


async def _run_scan_job_async(
    app: web.Application,
    job: ScanJob,
    payload: Dict[str, Any],
) -> None:
    try:
        await asyncio.to_thread(_run_scan_job, app[COORD_KEY], job, payload)
    finally:
        app[SCAN_STATE_KEY].finish(job.scan_id)


async def handle_get_scan(request: web.Request) -> web.Response:
    """Return progress for a background discovery scan."""
    job = request.app[SCAN_STATE_KEY].jobs.get(request.match_info["scan_id"])
    if not job:
        return web.json_response({"success": False, "error": "Scan not found"}, status=404)
    return web.json_response({"success": True, **job.snapshot()})


async def handle_cancel_scan(request: web.Request) -> web.Response:
    """Request cooperative cancellation of a background discovery scan."""
    job = request.app[SCAN_STATE_KEY].jobs.get(request.match_info["scan_id"])
    if not job:
        return web.json_response({"success": False, "error": "Scan not found"}, status=404)
    if not job.request_cancel():
        return web.json_response({"success": False, **job.snapshot()}, status=409)
    return web.json_response({"success": True, **job.snapshot()}, status=202)


def serialize_model(obj: Any) -> Any:
    """Safely serialize Pydantic or dictionary models to JSON-compatible dict."""
    if obj is None:
        return None
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump"):
        dumped = obj.model_dump(mode="json")
        if isinstance(dumped, (dict, list, str, int, float, bool)) or dumped is None:
            return dumped
        return None
    if hasattr(obj, "to_dict"):
        dumped = obj.to_dict()
        if isinstance(dumped, (dict, list, str, int, float, bool)) or dumped is None:
            return dumped
        return None
    if hasattr(obj, "dict"):
        dumped = obj.dict()
        if isinstance(dumped, (dict, list, str, int, float, bool)) or dumped is None:
            return dumped
        return None
    return obj


def _serialize_video_bundle(db: Database, video: Any) -> Optional[Dict[str, Any]]:
    """Serialize a video along with its identification, concert, venue, and upload records."""
    if video is None:
        return None
    data = serialize_model(video)
    if not isinstance(data, dict):
        return data

    video_id = getattr(video, "id", None) or data.get("id")
    ident = db.get_identification_by_video_id(video_id) if video_id else None
    ident_data = serialize_model(ident)
    data["identification"] = ident_data

    concert = None
    venue = None
    concert_id = (
        getattr(ident, "concert_id", None)
        if ident_data is not None and not isinstance(ident, dict)
        else (ident_data.get("concert_id") if isinstance(ident_data, dict) else None)
    )
    if concert_id and isinstance(concert_id, str):
        concert = db.get_concert(concert_id)
    concert_data = serialize_model(concert)
    venue_id = (
        getattr(concert, "venue_id", None)
        if concert_data is not None and not isinstance(concert, dict)
        else (concert_data.get("venue_id") if isinstance(concert_data, dict) else None)
    )
    if venue_id and isinstance(venue_id, str):
        venue = db.get_venue(venue_id)
    else:
        lat = getattr(video, "latitude", None) if hasattr(video, "latitude") else data.get("latitude")
        lon = getattr(video, "longitude", None) if hasattr(video, "longitude") else data.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat and lon:
            venue = db.find_venue_by_coords(lat, lon)

    data["concert"] = concert_data
    data["venue"] = serialize_model(venue)

    upload = db.get_youtube_upload_by_video_id(video_id) if video_id else None
    data["upload"] = serialize_model(upload)
    return data


async def handle_list_videos(request: web.Request) -> web.Response:
    """Return JSON list of cataloged videos with embedded related records."""
    db: Database = request.app[DB_KEY]
    status_param = request.query.get("status")
    limit = int(request.query.get("limit", 500))

    target_status = VideoStatus(status_param) if status_param else None
    videos = db.list_videos(status=target_status, limit=limit)

    results = [_serialize_video_bundle(db, v) for v in videos]
    return web.json_response({"videos": results, "count": len(results)})


async def handle_get_video(request: web.Request) -> web.Response:
    """Return JSON for a single video by ID."""
    db: Database = request.app[DB_KEY]
    video_id = request.match_info["id"]
    video = db.get_video(video_id)
    if not video:
        return web.json_response({"error": "Video not found"}, status=404)

    return web.json_response(_serialize_video_bundle(db, video))


async def handle_approve_video(request: web.Request) -> web.Response:
    """Approve a video for YouTube upload."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]

    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}

    try:
        title_override = body.get("title")
        song_title_override = body.get("song_title")
        artist_override = body.get("artist") or body.get("artist_name")
        privacy_raw = body.get("privacy")
        privacy: Optional[PrivacyStatus] = None
        if privacy_raw is not None and str(privacy_raw).strip():
            normalized_privacy = str(privacy_raw).strip().lower()
            try:
                privacy = PrivacyStatus(normalized_privacy)
            except ValueError:
                return web.json_response(
                    {
                        "success": False,
                        "error": f"Invalid privacy status '{privacy_raw}'. Expected public, unlisted, or private.",
                    },
                    status=400,
                )

        approve_kwargs: Dict[str, Any] = {
            "video_id": video_id,
            "title_override": title_override,
            "privacy_override": privacy,
        }
        if song_title_override:
            approve_kwargs["song_title_override"] = song_title_override
        if artist_override:
            approve_kwargs["artist_override"] = artist_override

        updated = coordinator.approve_video(**approve_kwargs)
        return web.json_response({"success": True, "video": serialize_model(updated)})
    except ValueError as e:
        return web.json_response({"success": False, "error": str(e)}, status=404)
    except Exception as e:
        logger.exception("Failed to approve video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_confirm_suggested_match(request: web.Request) -> web.Response:
    """Confirm a lyrics-based suggestion after the user has reviewed playback."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}

    privacy: Optional[PrivacyStatus] = None
    if body.get("privacy"):
        try:
            privacy = PrivacyStatus(str(body["privacy"]).strip().lower())
        except ValueError:
            return web.json_response({"success": False, "error": "Invalid privacy status."}, status=400)

    try:
        updated = coordinator.confirm_suggested_match(
            video_id=video_id,
            title_override=body.get("title"),
            privacy_override=privacy,
        )
        return web.json_response({"success": True, "video": serialize_model(updated)})
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("Failed to confirm suggested match for %s", video_id)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_reject_video(request: web.Request) -> web.Response:
    """Reject a video from upload queue."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    try:
        updated = coordinator.reject_video(video_id)
        return web.json_response({"success": True, "video": serialize_model(updated)})
    except ValueError as e:
        return web.json_response({"success": False, "error": str(e)}, status=404)
    except Exception as e:
        logger.exception("Failed to reject video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_delete_video(request: web.Request) -> web.Response:
    """Permanently delete a rejected video and its cached media."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    try:
        deleted = coordinator.delete_video(video_id)
        return web.json_response({"success": True, "video": deleted})
    except ValueError as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        logger.exception("Failed to delete video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_video_thumbnail(request: web.Request) -> web.Response:
    """Return JPEG video frame still or fallback SVG."""
    db: Database = request.app[DB_KEY]
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    video = db.get_video(video_id)
    if not video:
        return web.Response(
            body=ThumbnailExtractor.get_fallback_svg(),
            content_type="image/svg+xml",
        )

    try:
        frame_index = max(0, int(request.query.get("frame", "0")))
    except ValueError:
        frame_index = 0

    cached_path = coordinator.thumbnail_extractor.get_cached_path(video.id, frame_index)
    if cached_path.exists() and cached_path.stat().st_size > 0:
        return web.FileResponse(cached_path)

    if video.file_path and Path(video.file_path).exists():
        try:
            thumb = coordinator.thumbnail_extractor.extract_thumbnail(
                video_path=video.file_path,
                output_path=cached_path,
                offset_seconds=_thumbnail_offset(video.duration_seconds, frame_index),
            )
            return web.FileResponse(thumb)
        except Exception as e:
            logger.warning("Failed to extract thumbnail for %s: %s", video.id, e)

    return web.Response(
        body=ThumbnailExtractor.get_fallback_svg(),
        content_type="image/svg+xml",
    )


_AUDIO_EXTRACT_LOCKS: Dict[str, asyncio.Lock] = {}
_PURE_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".aac", ".ogg", ".wav", ".flac", ".opus"}


def _get_audio_extract_lock(video_id: str) -> asyncio.Lock:
    """Return a per-video asyncio lock so concurrent requests share a single extraction pass."""
    lock = _AUDIO_EXTRACT_LOCKS.get(video_id)
    if lock is None:
        lock = asyncio.Lock()
        _AUDIO_EXTRACT_LOCKS[video_id] = lock
    return lock


async def handle_video_audio(request: web.Request) -> web.Response:
    """Serve a compact cached full-length audio track (faststart M4A) for instant web playback."""
    db: Database = request.app[DB_KEY]
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video = db.get_video(request.match_info["id"])
    if not video or not video.file_path:
        return web.json_response({"error": "Video file not available"}, status=404)

    media_path = Path(video.file_path)
    if not media_path.exists() or not media_path.is_file():
        return web.json_response({"error": "Video file not available"}, status=404)

    cache_headers = {
        "Content-Disposition": "inline",
        "Cache-Control": "private, max-age=86400",
    }

    if media_path.suffix.lower() not in _PURE_AUDIO_EXTENSIONS:
        audio_extractor = getattr(coordinator, "audio_extractor", None)
        get_cached_fn = getattr(audio_extractor, "get_cached_playback_path", None)
        extract_full_fn = getattr(audio_extractor, "extract_full_audio", None)
        cached_audio_path = get_cached_fn(video.id) if callable(get_cached_fn) else None

        if isinstance(cached_audio_path, Path):
            if cached_audio_path.exists() and cached_audio_path.stat().st_size > 0:
                return web.FileResponse(
                    cached_audio_path,
                    headers={"Content-Type": "audio/mp4", **cache_headers},
                )
            if callable(extract_full_fn):
                async with _get_audio_extract_lock(video.id):
                    if cached_audio_path.exists() and cached_audio_path.stat().st_size > 0:
                        return web.FileResponse(
                            cached_audio_path,
                            headers={"Content-Type": "audio/mp4", **cache_headers},
                        )
                    try:
                        extracted = await asyncio.to_thread(
                            extract_full_fn,
                            media_path,
                            cached_audio_path,
                        )
                        if isinstance(extracted, Path) and extracted.exists() and extracted.stat().st_size > 0:
                            return web.FileResponse(
                                extracted,
                                headers={"Content-Type": "audio/mp4", **cache_headers},
                            )
                    except Exception as exc:
                        logger.warning(
                            "Full-audio extraction failed for %s (%s); falling back to raw media container: %s",
                            video.id,
                            media_path.name,
                            exc,
                        )

    content_type = video.mime_type or mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    return web.FileResponse(
        media_path,
        headers={
            "Content-Type": content_type,
            **cache_headers,
        },
    )


def _thumbnail_offset(duration_seconds: Optional[float], frame_index: int) -> float:
    """Pick a monotonically advancing timestamp for the requested frame."""
    duration = max(0.0, float(duration_seconds or 0.0))
    if frame_index <= 0 or duration <= 1.0:
        return 1.0

    step = max(1.0, min(8.0, duration / 8.0))
    return min(duration - 0.05, max(0.0, 1.0 + (frame_index * step)))


async def handle_venue_search(request: web.Request) -> web.Response:
    """Search venues across local DB, Setlist.fm, and OpenStreetMap."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    query = request.query.get("q", "").strip()
    city = request.query.get("city", "").strip() or None
    limit = int(request.query.get("limit", 15))

    if not query:
        return web.json_response({"venues": [], "count": 0})

    try:
        results = coordinator.venue_search_engine.search(query=query, city=city, limit=limit)
        return web.json_response({"venues": results, "count": len(results)})
    except Exception as e:
        logger.exception("Venue search error for query '%s': %s", query, e)
        return web.json_response({"venues": [], "count": 0, "error": str(e)})


async def handle_setlist_search(request: web.Request) -> web.Response:
    """Search Setlist.fm for manually selectable concert records."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    body = await request.json() if request.can_read_body else {}
    try:
        results = await asyncio.to_thread(
            coordinator.setlist_client.search_concerts,
            event_date=body.get("event_date"),
            artist_name=body.get("artist_name"),
            venue_name=body.get("venue_name"),
            city_name=body.get("city_name"),
            tour_name=body.get("tour_name"),
            state=body.get("state"),
            country_code=body.get("country_code"),
        )
        return web.json_response({"success": True, "video_id": video_id, "concerts": [serialize_model(item) for item in results]})
    except Exception as exc:
        logger.exception("Setlist.fm manual search failed for video %s", video_id)
        return web.json_response({"success": False, "error": str(exc), "concerts": []}, status=502)


async def handle_setlist_match(request: web.Request) -> web.Response:
    """Attach a user-selected Setlist.fm concert to a video."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    body = await request.json() if request.can_read_body else {}
    try:
        matched = MatchedConcert.parse_obj(body.get("concert") or body)
        video, concert, ident = coordinator.attach_setlist_concert(video_id, matched)
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("Failed to attach Setlist.fm concert to video %s", video_id)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_set_video_venue(request: web.Request) -> web.Response:
    """Associate venue with video and re-run concert matching and setlist identification."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]

    body = await request.json() if request.can_read_body else {}
    venue_name = (body.get("venue_name") or "").strip()
    if not venue_name:
        return web.json_response({"success": False, "error": "Missing venue_name"}, status=400)

    city = body.get("city")
    state_province = body.get("state_province") or body.get("state")
    country_code = body.get("country_code") or body.get("country") or "US"
    lat_raw = body.get("latitude")
    lon_raw = body.get("longitude")
    latitude = float(lat_raw) if lat_raw is not None else None
    longitude = float(lon_raw) if lon_raw is not None else None
    setlist_fm_venue_id = body.get("setlist_fm_venue_id")
    osm_id = body.get("osm_id")
    formatted_address = body.get("formatted_address")

    try:
        video, concert, ident = coordinator.reidentify_video_with_venue(
            video_id=video_id,
            venue_name=venue_name,
            city=city,
            state_province=state_province,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            setlist_fm_venue_id=setlist_fm_venue_id,
            osm_id=osm_id,
            formatted_address=formatted_address,
        )
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except Exception as e:
        logger.exception("Failed to set venue for video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_apply_venue_to_date(request: web.Request) -> web.Response:
    """Apply a selected venue to other cataloged videos from the same date."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    body = await request.json() if request.can_read_body else {}
    recorded_date = (body.get("recorded_date") or "").strip()
    venue_name = (body.get("venue_name") or "").strip()
    source_video_id = (body.get("source_video_id") or "").strip()
    if not recorded_date or not venue_name or not source_video_id:
        return web.json_response(
            {"success": False, "error": "Missing recorded_date, venue_name, or source_video_id"},
            status=400,
        )

    try:
        result = coordinator.apply_venue_to_date(
            source_video_id=source_video_id,
            recorded_date=recorded_date,
            venue_name=venue_name,
            city=body.get("city"),
            state_province=body.get("state_province") or body.get("state"),
            country_code=body.get("country_code") or body.get("country") or "US",
            latitude=float(body["latitude"]) if body.get("latitude") is not None else None,
            longitude=float(body["longitude"]) if body.get("longitude") is not None else None,
            setlist_fm_venue_id=body.get("setlist_fm_venue_id"),
            osm_id=body.get("osm_id"),
            formatted_address=body.get("formatted_address"),
        )
        return web.json_response({"success": True, **result})
    except Exception as e:
        logger.exception("Failed applying venue to %s videos: %s", recorded_date, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_set_video_band(request: web.Request) -> web.Response:
    """Assign specific performing band to video from show lineup."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]

    body = await request.json() if request.can_read_body else {}
    artist_name = (body.get("artist_name") or "").strip()
    if not artist_name:
        return web.json_response({"success": False, "error": "Missing artist_name"}, status=400)

    song_title = body.get("song_title")

    try:
        video, concert, ident = coordinator.reidentify_video_with_band(
            video_id=video_id,
            artist_name=artist_name,
            song_title=song_title,
        )
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except Exception as e:
        logger.exception("Failed to set band for video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_song_options(request: web.Request) -> web.Response:
    """Return setlist choices or discography search choices for manual matching."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    query = request.query.get("q", "")
    try:
        data = await asyncio.to_thread(coordinator.get_song_options, video_id, query)
        status = 502 if data.get("error") else 200
        return web.json_response(data, status=status)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "songs": []}, status=404)
    except Exception as exc:
        logger.exception("Song option search failed for video %s: %s", video_id, exc)
        return web.json_response({"error": str(exc), "songs": []}, status=500)


async def handle_set_video_song(request: web.Request) -> web.Response:
    """Save the user's selected song title using the existing band re-identification path."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    body = await request.json() if request.can_read_body else {}
    song_title = (body.get("song_title") or "").strip()
    artist_name = (body.get("artist_name") or "").strip()
    if not song_title:
        return web.json_response({"success": False, "error": "Missing song_title"}, status=400)

    if not artist_name:
        video = request.app[DB_KEY].get_video(video_id)
        ident = request.app[DB_KEY].get_identification_by_video_id(video_id) if video else None
        concert = request.app[DB_KEY].get_concert(ident.concert_id) if ident and ident.concert_id else None
        candidate_artist = (ident.detected_artist if ident else "").strip()
        if candidate_artist.casefold() in {"live artist", "unknown artist", "unknown"}:
            candidate_artist = ""
        artist_name = candidate_artist or (concert.headliner_artist if concert else "")
    if not artist_name:
        return web.json_response({"success": False, "error": "Select the performing band first"}, status=400)

    try:
        video, concert, ident = coordinator.reidentify_video_with_band(
            video_id=video_id,
            artist_name=artist_name,
            song_title=song_title,
        )
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except Exception as exc:
        logger.exception("Failed to set song for video %s: %s", video_id, exc)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_trigger_video_match(request: web.Request) -> web.Response:
    """Run local multi-window audio song matching for one video and report explicit outcome."""
    db: Database = request.app[DB_KEY]
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    video = db.get_video(video_id)
    if not video:
        return web.json_response(
            {"success": False, "matched": False, "outcome": "error", "error": "Video not found"},
            status=404,
        )
    try:
        result = await asyncio.to_thread(coordinator.match_video_song, video_id)
        payload = dict(result or {})
        updated_video = payload.get("video") or db.get_video(video_id) or video
        payload["video"] = _serialize_video_bundle(db, updated_video)
        is_error = payload.get("outcome") == "error" or not payload.get("success", False)
        return web.json_response(payload, status=502 if is_error else 200)
    except ValueError as exc:
        return web.json_response(
            {"success": False, "matched": False, "outcome": "error", "error": str(exc)},
            status=404,
        )
    except Exception as exc:
        logger.exception("Manual song matching failed for video %s", video_id)
        return web.json_response(
            {"success": False, "matched": False, "outcome": "error", "error": str(exc)},
            status=500,
        )


async def handle_trigger_batch_song_match(request: web.Request) -> web.Response:
    """Run audio song matching across active unidentified videos and report explicit counts."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    try:
        raw_limit = int(body.get("limit", 25))
    except (TypeError, ValueError):
        raw_limit = 25
    limit = max(1, min(100, raw_limit))

    try:
        summary = await asyncio.to_thread(coordinator.match_unidentified_songs, limit)
        status = 200 if summary.get("success", True) else 502
        return web.json_response(summary, status=status)
    except Exception as exc:
        logger.exception("Batch song matching failed: %s", exc)
        return web.json_response(
            {
                "success": False,
                "checked_count": 0,
                "matched_count": 0,
                "no_match_count": 0,
                "error_count": 1,
                "error": str(exc),
                "message": f"Batch song matching failed: {exc}",
            },
            status=500,
        )


async def handle_trigger_scan(request: web.Request) -> web.Response:
    """Start a Google Photos, Google Drive, or local discovery scan in the background."""
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    payload = dict(body)
    if payload.get("days"):
        payload["days"] = int(payload["days"])
    payload["limit"] = int(payload.get("limit", 50))
    if payload.get("directory"):
        mode = "local_directory"
    else:
        provider = payload.get("provider", "google_drive" if payload.get("drive") else "google_photos")
        mode = "google_drive" if provider in ("google_drive", "drive") or payload.get("drive") else "google_photos"

    scan_state: ScanState = request.app[SCAN_STATE_KEY]
    job = scan_state.start(mode)
    if not job:
        return web.json_response({
            "success": False,
            "error": "A discovery scan is already running.",
        }, status=409)

    asyncio.create_task(_run_scan_job_async(request.app, job, payload))
    return web.json_response({
        "success": True,
        "scan_id": job.scan_id,
        "mode": mode,
        "status": "running",
    }, status=202)


async def handle_picker_create(request: web.Request) -> web.Response:
    """Create a Google Photos Picker session."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    try:
        session = coordinator.photos_client.create_picker_session()
        return web.json_response({"success": True, "session": session})
    except Exception as e:
        logger.warning("Failed to create picker session: %s", e)
        return web.json_response({"success": False, "error": str(e)})


async def handle_picker_poll(request: web.Request) -> web.Response:
    """Poll a Google Photos Picker session and ingest items if complete."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    body = await request.json() if request.can_read_body else {}
    session_id = body.get("session_id")
    dry_run = bool(body.get("dry_run", False))
    if not session_id:
        return web.json_response({"success": False, "error": "Missing session_id"}, status=400)
    try:
        is_done = coordinator.photos_client.poll_picker_session(session_id)
        if not is_done:
            return web.json_response({"success": True, "completed": False})

        items = coordinator.photos_client.get_picker_items(session_id)
        videos = coordinator.ingest_picker_items(items, dry_run=dry_run)
        coordinator.photos_client.delete_picker_session(session_id)
        return web.json_response({
            "success": True,
            "completed": True,
            "ingested_count": len(videos),
        })
    except Exception as e:
        logger.warning("Failed to poll picker session %s: %s", session_id, e)
        return web.json_response({"success": False, "error": str(e)})


async def handle_trigger_process(request: web.Request) -> web.Response:
    """Trigger processing of pending candidate videos."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    try:
        processed = await asyncio.to_thread(coordinator.process_pending_videos, 25)
        count = len(processed)
        message = (
            f"Processed {count} pending video{'s' if count != 1 else ''}."
            if count
            else "No pending candidate videos needed processing."
        )
        return web.json_response({"success": True, "processed_count": count, "message": message})
    except Exception as e:
        logger.exception("Failed to process videos: %s", e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_trigger_upload(request: web.Request) -> web.Response:
    """Trigger upload of approved videos to YouTube."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    try:
        results = await asyncio.to_thread(coordinator.upload_approved_videos)
        uploaded_count = len([r for r in results if r.success])
        failed_results = [r for r in results if not r.success]
        failed_count = len(failed_results)
        failure_details = [r.error_message for r in failed_results if r.error_message]
        first_reason = f" Reason: {failure_details[0]}" if failure_details else ""
        if uploaded_count and failed_count:
            message = f"Uploaded {uploaded_count} video{'s' if uploaded_count != 1 else ''}; {failed_count} failed.{first_reason}"
        elif uploaded_count:
            message = f"Uploaded {uploaded_count} approved video{'s' if uploaded_count != 1 else ''} to YouTube."
        elif failed_count:
            message = f"All {failed_count} approved video upload{'s' if failed_count != 1 else ''} failed.{first_reason}"
        else:
            message = "No approved videos were queued for YouTube upload."
        return web.json_response({
            "success": True,
            "uploaded_count": uploaded_count,
            "failed_count": failed_count,
            "errors": failure_details,
            "message": message,
        })
    except Exception as e:
        logger.exception("Failed to upload approved videos: %s", e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


def run_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    """Start the aiohttp server loop."""
    app = create_app()
    if open_browser:
        webbrowser.open(f"http://{host}:{port}")
    web.run_app(app, host=host, port=port)
