"""Unit tests for StageSync Web Dashboard & REST Server."""

import unittest
from pathlib import Path
import tempfile
from unittest.mock import MagicMock

from aiohttp.test_utils import AioHTTPTestCase

from stagesync.database.models import PrivacyStatus, Video, VideoStatus
from stagesync.web.server import _thumbnail_offset, create_app


class TestWebServer(AioHTTPTestCase):
    """Test suite for Web Dashboard REST endpoints."""

    async def get_application(self):
        self.mock_coord = MagicMock()
        self.mock_db = MagicMock()
        self.mock_settings = MagicMock()
        self.mock_settings.max_daily_uploads = 5
        self.mock_settings.setlist_fm_api_key = "test_key"
        self.mock_settings.gemini_api_key = "test_key"

        return create_app(
            coordinator=self.mock_coord,
            db=self.mock_db,
            settings=self.mock_settings,
        )

    async def test_get_index(self):
        """Verify dashboard HTML is served at root."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("StageSync", text)
        self.assertIn("Review Queue", text)
        self.assertIn("setStatusFilter('REJECTED')", text)
        self.assertIn("activeStatusFilter === 'ALL') return v.processing_status !== 'REJECTED'", text)
        self.assertIn("Lineup Identification Required", text)
        self.assertIn("openBandAssistModal", text)
        self.assertIn("venue-assist-refresh", text)
        self.assertIn("band-assist-refresh", text)
        self.assertIn("refreshVideoStill", text)
        self.assertIn("const songUnmatched = isSongUnmatched(v);", text)
        self.assertIn("const needsSongAssist = songUnmatched && Boolean(knownArtistForVideo(v));", text)
        self.assertIn("const needsVenueAssist = !hasKnownVenue;", text)
        self.assertIn("${needsVenueAssist ? `", text)
        self.assertIn("${needsSongAssist ? `", text)

    async def test_get_status_json(self):
        """Verify /api/status returns valid system statistics."""
        fake_video = Video(
            source_photo_id="sp_1",
            filename="live.mp4",
            duration_seconds=45.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        rejected_video = Video(
            source_photo_id="sp_rejected",
            filename="IMG_1058.MOV",
            duration_seconds=2.0,
            processing_status=VideoStatus.REJECTED,
        )
        self.mock_db.list_videos.return_value = [fake_video, rejected_video]
        self.mock_db.get_todays_upload_count.return_value = 1
        self.mock_coord.photos_client.is_authenticated.return_value = True
        self.mock_coord.drive_client.is_authenticated.return_value = True
        self.mock_coord.uploader.auth.is_authenticated.return_value = False

        resp = await self.client.get("/api/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["total_videos"], 1)
        self.assertEqual(data["status_counts"]["REJECTED"], 1)
        self.assertEqual(data["uploads_today"], 1)
        self.assertTrue(data["services"]["google_photos"]["authenticated"])
        self.assertTrue(data["services"]["google_drive"]["authenticated"])
        self.assertFalse(data["services"]["youtube"]["authenticated"])

    async def test_approve_video_endpoint(self):
        """Verify /api/video/{id}/approve calls coordinator and returns updated video."""
        fake_video = Video(
            id="v_123",
            source_photo_id="sp_1",
            filename="live.mp4",
            duration_seconds=45.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
            processing_status=VideoStatus.APPROVED,
        )
        self.mock_coord.approve_video.return_value = fake_video

        resp = await self.client.post("/api/video/v_123/approve", json={"title": "Custom Live Title"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["video"]["id"], "v_123")
        self.mock_coord.approve_video.assert_called_once_with(
            video_id="v_123",
            title_override="Custom Live Title",
            privacy_override=None,
        )

    async def test_reject_video_endpoint(self):
        """Verify /api/video/{id}/reject marks video rejected."""
        fake_video = Video(
            id="v_123",
            source_photo_id="sp_1",
            filename="live.mp4",
            duration_seconds=45.0,
            width=1920,
            height=1080,
            recorded_at_utc="2024-08-15T21:00:00Z",
            processing_status=VideoStatus.REJECTED,
        )
        self.mock_coord.reject_video.return_value = fake_video

        resp = await self.client.post("/api/video/v_123/reject")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.mock_coord.reject_video.assert_called_once_with("v_123")

    async def test_scan_local_directory_endpoint(self):
        """Verify /api/scan with directory invokes scan_local_directory."""
        fake_video = Video(source_photo_id="loc_1", filename="clip.mp4")
        self.mock_coord.scan_local_directory.return_value = [fake_video]

        resp = await self.client.post("/api/scan", json={"directory": "/tmp/videos"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["scanned_count"], 1)
        self.assertEqual(data["mode"], "local_directory")
        self.mock_coord.scan_local_directory.assert_called_once_with("/tmp/videos")

    async def test_scan_google_drive_endpoint(self):
        """Verify /api/scan with provider=google_drive calls scan_google_drive."""
        fake_video = Video(source_photo_id="drive_1", filename="drive_clip.mp4")
        self.mock_coord.scan_google_drive.return_value = [fake_video]

        resp = await self.client.post("/api/scan", json={"provider": "google_drive", "days": 30})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["scanned_count"], 1)
        self.assertEqual(data["mode"], "google_drive")
        self.mock_coord.scan_google_drive.assert_called_once()

    async def test_scan_google_photos_scope_error_handling(self):
        """Verify 403 insufficient scopes error returns transparent diagnostics."""
        self.mock_coord.scan_google_photos.side_effect = RuntimeError(
            "Google Photos query failed: 403 Client Error: Forbidden: Request had insufficient authentication scopes."
        )

        resp = await self.client.post("/api/scan", json={"start_date": "2024-01-01"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertFalse(data["success"])
        self.assertTrue(data["is_scope_error"])
        self.assertIn("insufficient authentication scopes", data["error"])

    async def test_picker_create_and_poll_endpoints(self):
        """Verify /api/picker/create and /api/picker/poll flow."""
        self.mock_coord.photos_client.create_picker_session.return_value = {
            "id": "sess_123",
            "pickerUri": "https://photos.google.com/picker/sess_123",
        }
        resp = await self.client.post("/api/picker/create")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["session"]["id"], "sess_123")

        # Incomplete poll
        self.mock_coord.photos_client.poll_picker_session.return_value = False
        resp = await self.client.post("/api/picker/poll", json={"session_id": "sess_123"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["completed"])

        # Completed poll
        self.mock_coord.photos_client.poll_picker_session.return_value = True
        self.mock_coord.photos_client.get_picker_items.return_value = [{"id": "item_1", "baseUrl": "http://img"}]
        fake_v = Video(source_photo_id="item_1", filename="picker.mp4")
        self.mock_coord.ingest_picker_items.return_value = [fake_v]

        resp = await self.client.post("/api/picker/poll", json={"session_id": "sess_123"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["completed"])
        self.assertEqual(data["ingested_count"], 1)
        self.mock_coord.photos_client.delete_picker_session.assert_called_once_with("sess_123")

    async def test_video_thumbnail_fallback(self):
        """Verify /api/video/{id}/thumbnail returns fallback SVG when not found."""
        self.mock_db.get_video.return_value = None
        resp = await self.client.get("/api/video/v_nonexistent/thumbnail")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/svg+xml")
        text = await resp.text()
        self.assertIn("<svg", text)
        self.assertIn("Video Preview Frame", text)

    async def test_video_thumbnail_uses_requested_alternate_frame(self):
        """Verify alternate frame requests use a separate cache path and timestamp."""
        fake_video = Video(
            id="v_frame",
            source_photo_id="sp_frame",
            filename="live.mp4",
            duration_seconds=40.0,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        self.mock_db.get_video.return_value = fake_video
        self.mock_coord.thumbnail_extractor.get_cached_path.return_value = Path("/tmp/stagesync-frame-does-not-exist.jpg")

        resp = await self.client.get("/api/video/v_frame/thumbnail?frame=2")
        self.assertEqual(resp.status, 200)
        self.mock_coord.thumbnail_extractor.get_cached_path.assert_called_once_with("v_frame", 2)
        self.mock_coord.thumbnail_extractor.extract_thumbnail.assert_not_called()

    async def test_video_audio_streams_cataloged_media(self):
        """Verify the song picker can play a cataloged video's audio track."""
        with tempfile.NamedTemporaryFile(suffix=".mp4") as media:
            media.write(b"media bytes")
            media.flush()
            fake_video = Video(
                id="v_audio",
                source_photo_id="sp_audio",
                filename="concert.mp4",
                file_path=media.name,
                mime_type="video/mp4",
            )
            self.mock_db.get_video.return_value = fake_video
            resp = await self.client.get("/api/video/v_audio/audio")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type, "video/mp4")
            self.assertEqual(await resp.read(), b"media bytes")

    async def test_video_audio_returns_not_found_without_media(self):
        """Verify audio playback does not expose missing or arbitrary paths."""
        self.mock_db.get_video.return_value = None
        resp = await self.client.get("/api/video/v_missing/audio")
        self.assertEqual(resp.status, 404)

    def test_thumbnail_offset_changes_for_alternate_frames(self):
        """Verify alternate frame indexes map to later timestamps."""
        self.assertEqual(_thumbnail_offset(40.0, 0), 1.0)
        self.assertEqual(_thumbnail_offset(40.0, 1), 14.0)
        self.assertEqual(_thumbnail_offset(40.0, 2), 24.0)

    async def test_venue_search_endpoint(self):
        """Verify /api/venues/search calls venue_search_engine and returns results."""
        self.mock_coord.venue_search_engine.search.return_value = [
            {
                "name": "Come and Take It Live",
                "city": "Austin",
                "state": "TX",
                "country": "US",
                "formatted_address": "2015 E Riverside Dr, Austin, TX",
                "latitude": 30.2408,
                "longitude": -97.7288,
                "setlist_fm_venue_id": "63d55e97",
                "osm_id": "382484067",
                "sources": ["local", "setlist_fm"],
            }
        ]
        resp = await self.client.get("/api/venues/search?q=Come&city=Austin")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["venues"][0]["name"], "Come and Take It Live")
        self.mock_coord.venue_search_engine.search.assert_called_once_with(
            query="Come",
            city="Austin",
            limit=15,
        )

    async def test_set_video_venue_endpoint(self):
        """Verify /api/video/{id}/set-venue links venue and re-identifies concert."""
        fake_video = Video(
            id="v_100",
            source_photo_id="sp_100",
            filename="IMG_6130.MOV",
            duration_seconds=25.0,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        fake_concert = MagicMock()
        fake_concert.model_dump.return_value = {"id": "c_1", "headliner_artist": "Attila"}
        fake_ident = MagicMock()
        fake_ident.model_dump.return_value = {"id": "id_1", "detected_song_title": "About That Life"}

        self.mock_coord.reidentify_video_with_venue.return_value = (fake_video, fake_concert, fake_ident)

        payload = {
            "venue_name": "Come and Take It Live",
            "city": "Austin",
            "state_province": "TX",
            "country_code": "US",
            "latitude": 30.2408,
            "longitude": -97.7288,
            "setlist_fm_venue_id": "63d55e97",
        }
        resp = await self.client.post("/api/video/v_100/set-venue", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["video"]["id"], "v_100")
        self.mock_coord.reidentify_video_with_venue.assert_called_once()

    async def test_set_video_venue_validation_error(self):
        """Verify /api/video/{id}/set-venue requires venue_name."""
        resp = await self.client.post("/api/video/v_100/set-venue", json={})
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertFalse(data["success"])
        self.assertIn("Missing venue_name", data["error"])

    async def test_set_video_band_endpoint(self):
        """Verify /api/video/{id}/set-band assigns band to video from show lineup."""
        fake_video = Video(
            id="v_100",
            source_photo_id="sp_100",
            filename="IMG_6142.MOV",
            duration_seconds=30.0,
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        fake_concert = MagicMock()
        fake_concert.model_dump.return_value = {
            "id": "c_1",
            "headliner_artist": "Attila",
            "lineup_artists": ["Attila", "Gideon", "ten56.", "Until I Wake"],
        }
        fake_ident = MagicMock()
        fake_ident.model_dump.return_value = {
            "id": "id_1",
            "detected_artist": "Gideon",
            "detected_song_title": "Live Performance",
            "status": "USER_ASSISTED_BAND",
        }

        self.mock_coord.reidentify_video_with_band.return_value = (fake_video, fake_concert, fake_ident)

        payload = {"artist_name": "Gideon"}
        resp = await self.client.post("/api/video/v_100/set-band", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["video"]["id"], "v_100")
        self.assertEqual(data["identification"]["detected_artist"], "Gideon")
        self.mock_coord.reidentify_video_with_band.assert_called_once_with(
            video_id="v_100",
            artist_name="Gideon",
            song_title=None,
        )

    async def test_set_video_band_validation_error(self):
        """Verify /api/video/{id}/set-band requires artist_name."""
        resp = await self.client.post("/api/video/v_100/set-band", json={})
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertFalse(data["success"])
        self.assertIn("Missing artist_name", data["error"])

    async def test_song_options_endpoint(self):
        """Verify song choices are returned for the manual song picker."""
        self.mock_coord.get_song_options.return_value = {
            "source": "setlist",
            "songs": [{"title": "Dragon", "source": "setlist"}],
        }
        resp = await self.client.get("/api/video/v_100/song-options?q=Drag")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["source"], "setlist")
        self.assertEqual(data["songs"][0]["title"], "Dragon")
        self.mock_coord.get_song_options.assert_called_once_with("v_100", "Drag")

    async def test_set_video_song_endpoint(self):
        """Verify a manually selected song is persisted through the coordinator."""
        fake_video = Video(
            id="v_100",
            source_photo_id="sp_100",
            filename="IMG_6142.MOV",
            processing_status=VideoStatus.PENDING_REVIEW,
        )
        fake_ident = MagicMock()
        fake_ident.model_dump.return_value = {"detected_song_title": "Dragon"}
        self.mock_coord.reidentify_video_with_band.return_value = (fake_video, None, fake_ident)

        resp = await self.client.post("/api/video/v_100/set-song", json={"song_title": "Dragon", "artist_name": "King Gizzard"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.mock_coord.reidentify_video_with_band.assert_called_once_with(
            video_id="v_100",
            artist_name="King Gizzard",
            song_title="Dragon",
        )

    async def test_manual_video_match_endpoint(self):
        """Verify the review queue can explicitly rerun song matching for one video and receive rich outcome."""
        fake_video = Video(id="v_100", source_photo_id="sp_100", filename="clip.mov")
        self.mock_db.get_video.return_value = fake_video
        self.mock_coord.match_video_song.return_value = {
            "success": True,
            "matched": True,
            "outcome": "matched",
            "song_title": "Dragon",
            "artist": "King Gizzard",
            "confidence": 0.94,
            "service": "shazam",
            "message": 'Matched "Dragon" by King Gizzard (94% confidence via shazam).',
            "error": None,
            "diagnostics": {"windows_checked": 1},
            "video": fake_video,
        }

        resp = await self.client.post("/api/video/v_100/match")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["matched"])
        self.assertEqual(data["outcome"], "matched")
        self.assertEqual(data["song_title"], "Dragon")
        self.assertEqual(data["video"]["id"], "v_100")
        self.mock_coord.match_video_song.assert_called_once_with("v_100")

    async def test_manual_video_match_no_match_and_error_outcomes(self):
        """Verify /api/video/{id}/match surfaces explicit no-match (200) and error (502) payloads instead of failing silently."""
        fake_video = Video(id="v_100", source_photo_id="sp_100", filename="clip.mov")
        self.mock_db.get_video.return_value = fake_video

        # No-match outcome
        self.mock_coord.match_video_song.return_value = {
            "success": True,
            "matched": False,
            "outcome": "no_match",
            "song_title": None,
            "artist": "Gideon",
            "confidence": 0.12,
            "service": "gemini_audio",
            "message": "No song match found across 3 audio windows. Crowd noise obscured vocals.",
            "error": None,
            "diagnostics": {"windows_checked": 3},
            "video": fake_video,
        }
        resp = await self.client.post("/api/video/v_100/match")
        self.assertEqual(resp.status, 200)
        no_match_data = await resp.json()
        self.assertTrue(no_match_data["success"])
        self.assertFalse(no_match_data["matched"])
        self.assertEqual(no_match_data["outcome"], "no_match")
        self.assertIn("No song match found", no_match_data["message"])

        # Error outcome returns HTTP 502 with error details
        self.mock_coord.match_video_song.return_value = {
            "success": False,
            "matched": False,
            "outcome": "error",
            "song_title": None,
            "artist": "Gideon",
            "confidence": 0.0,
            "service": "none",
            "message": "Local video file is missing on disk; download or re-scan the clip before matching.",
            "error": "Local video file is missing on disk; download or re-scan the clip before matching.",
            "diagnostics": {"windows_checked": 0},
            "video": fake_video,
        }
        err_resp = await self.client.post("/api/video/v_100/match")
        self.assertEqual(err_resp.status, 502)
        err_data = await err_resp.json()
        self.assertFalse(err_data["success"])
        self.assertEqual(err_data["outcome"], "error")
        self.assertIn("missing on disk", err_data["error"])

    async def test_batch_song_match_endpoint(self):
        """Verify POST /api/match-songs triggers batch unidentified song matching and returns summary counts."""
        self.mock_coord.match_unidentified_songs.return_value = {
            "success": True,
            "checked_count": 3,
            "matched_count": 2,
            "no_match_count": 1,
            "error_count": 0,
            "message": "Checked 3 unidentified clips: 2 matched, 1 had no confident match.",
            "results": [
                {"video_id": "v_1", "outcome": "matched", "song_title": "Bite Down"},
                {"video_id": "v_2", "outcome": "matched", "song_title": "No Love/No One"},
                {"video_id": "v_3", "outcome": "no_match", "song_title": None},
            ],
        }
        resp = await self.client.post("/api/match-songs", json={"limit": 10})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["checked_count"], 3)
        self.assertEqual(data["matched_count"], 2)
        self.assertEqual(data["no_match_count"], 1)
        self.mock_coord.match_unidentified_songs.assert_called_once_with(10)

    async def test_song_options_error_returns_502(self):
        """Verify GET /api/video/{id}/song-options surfaces MusicBrainz errors with HTTP 502 instead of silent empty 200."""
        self.mock_coord.get_song_options.return_value = {
            "artist_name": "Gideon",
            "source": "error",
            "songs": [],
            "error": "MusicBrainz discography search failed: connection timed out",
            "message": "MusicBrainz discography search failed: connection timed out",
        }
        resp = await self.client.get("/api/video/v_100/song-options?q=Bite")
        self.assertEqual(resp.status, 502)
        data = await resp.json()
        self.assertEqual(data["source"], "error")
        self.assertIn("MusicBrainz discography search failed", data["error"])

    async def test_manual_song_match_ui_controls_and_feedback(self):
        """Verify dashboard HTML includes manual song match triggers, toast feedback, and status banners."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()

        self.assertIn('id="toast-container"', text)
        self.assertIn('id="top-btn-match-songs"', text)
        self.assertIn('id="sidebar-btn-match-songs"', text)
        self.assertIn('id="overview-btn-match-songs"', text)
        self.assertIn('id="drawer-btn-match-song"', text)
        self.assertIn('id="drawer-match-feedback"', text)
        self.assertIn('id="btn-modal-auto-match"', text)
        self.assertIn('id="song-assist-match-feedback"', text)
        self.assertIn('id="btn-song-search"', text)
        self.assertIn('id="song-search-status-banner"', text)
        self.assertIn("triggerBatchSongMatching", text)
        self.assertIn("triggerModalAudioMatch", text)
        self.assertIn("runManualSongSearch", text)
        self.assertIn("renderVideoMatchBanner", text)

    async def test_song_assist_modal_playback_progress_bar(self):
        """Verify the song selection modal contains an interactive playback progress bar and seeking controls."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()

        # Modal elements
        self.assertIn('id="song-assist-progress"', text)
        self.assertIn('id="song-assist-time-display"', text)
        self.assertIn('id="song-assist-audio"', text)
        self.assertIn('type="range"', text)

        # Quick seek buttons
        self.assertIn("seekSongAssistRelative(-10)", text)
        self.assertIn("seekSongAssistRelative(10)", text)

        # Interactive seeking handlers & time formatters
        self.assertIn("onSongAssistSeekInput", text)
        self.assertIn("onSongAssistSeekChange", text)
        self.assertIn("formatAudioTime", text)
        self.assertIn("updateSongAssistTimeDisplay", text)
        self.assertIn("isSongAssistScrubbing", text)



if __name__ == "__main__":
    unittest.main()
