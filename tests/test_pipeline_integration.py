"""End-to-End Pipeline Integration Tests.

Validates end-to-end relational data flow:
Ingestion -> Container Probe -> Reverse Geocode -> Setlist Match ->
Review Queue -> Approval -> Draft Publication.
"""

import tempfile
import unittest
from pathlib import Path
from stagesync.database.connection import Database
from stagesync.database.models import (
    Concert,
    Identification,
    PrivacyStatus,
    Venue,
    Video,
    VideoStatus,
    YouTubeUpload,
)
from stagesync.ingestion.metadata_parser import MetadataParser
from stagesync.intelligence.geocoder import ReverseGeocoder
from stagesync.intelligence.setlist_fm import SetlistFmClient
from stagesync.publisher.metadata_gen import MetadataGenerator
from tests.conftest import (
    SAMPLE_NOMINATIM_PAYLOAD,
    SAMPLE_RED_ROCKS_PROBE_JSON,
    SAMPLE_SETLIST_FM_PAYLOAD,
    SAMPLE_SHAZAM_PAYLOAD,
)


class TestPipelineIntegration(unittest.TestCase):
    """Integration test suite executing the complete StageSync state machine."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_stagesync.db"
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_end_to_end_pipeline_flow(self) -> None:
        # 1. Ingestion: Video discovered
        video = Video(
            source_photo_id="google_media_item_9999",
            filename="PXL_20240815_214201.mp4",
            processing_status=VideoStatus.DISCOVERED,
        )
        saved_video = self.db.save_video(video)
        self.assertEqual(saved_video.processing_status, VideoStatus.DISCOVERED)

        # 2. Metadata Extraction from Container
        parser = MetadataParser()
        dummy_file = Path(self.temp_dir.name) / "PXL_20240815_214201.mp4"
        meta = parser.parse_probe_dict(
            probe_data=SAMPLE_RED_ROCKS_PROBE_JSON,
            path=dummy_file,
            file_size=192937984,
            file_hash="sha256_mock_hash_val",
        )

        saved_video.file_path = str(dummy_file)
        saved_video.file_hash_sha256 = meta.file_hash_sha256
        saved_video.file_size_bytes = meta.file_size_bytes
        saved_video.duration_seconds = meta.duration_seconds
        saved_video.width = meta.width
        saved_video.height = meta.height
        saved_video.recorded_at_utc = meta.recorded_at_utc
        saved_video.recorded_at_local = meta.recorded_at_local
        saved_video.latitude = meta.latitude
        saved_video.longitude = meta.longitude
        saved_video.altitude_meters = meta.altitude_meters
        saved_video.device_make = meta.device_make
        saved_video.device_model = meta.device_model
        saved_video.is_concert_candidate = meta.is_concert_candidate
        saved_video.concert_confidence_score = meta.concert_confidence_score
        saved_video.processing_status = VideoStatus.ANALYZING_METADATA
        self.db.save_video(saved_video)

        self.assertTrue(saved_video.is_concert_candidate)
        self.assertEqual(saved_video.latitude, 39.6653)

        # 3. Reverse Geocode (Nominatim payload cached & stored)
        geocoder = ReverseGeocoder(db=self.db)
        cache_key = f"nom_{round(saved_video.latitude, 3):.3f}_{round(saved_video.longitude, 3):.3f}"
        self.db.set_cached_geocode(
            cache_key=cache_key,
            data={
                "display_name": SAMPLE_NOMINATIM_PAYLOAD["display_name"],
                "venue_name": "Red Rocks Amphitheatre",
                "city": "Morrison",
                "state_province": "Colorado",
                "country_code": "US",
                "latitude": 39.6653,
                "longitude": -105.2058,
                "osm_id": str(SAMPLE_NOMINATIM_PAYLOAD["osm_id"]),
                "raw": SAMPLE_NOMINATIM_PAYLOAD,
            },
        )
        resolved_loc = geocoder.reverse_geocode(saved_video.latitude, saved_video.longitude)
        venue = Venue(
            name=resolved_loc.venue_name,
            formatted_address=resolved_loc.display_name,
            city=resolved_loc.city,
            state_province=resolved_loc.state_province,
            country_code=resolved_loc.country_code,
            latitude=resolved_loc.latitude,
            longitude=resolved_loc.longitude,
            osm_id=resolved_loc.osm_id,
        )
        saved_venue = self.db.save_venue(venue)
        self.assertEqual(saved_venue.name, "Red Rocks Amphitheatre")

        # 4. Concert Search (Setlist.fm match)
        setlist_client = SetlistFmClient(api_key="test_key")
        matched = setlist_client.parse_setlist_payload(
            item=SAMPLE_SETLIST_FM_PAYLOAD["setlist"][0],
            fallback_date="2024-08-15",
        )
        concert = Concert(
            venue_id=saved_venue.id,
            headliner_artist=matched.headliner_artist,
            lineup_artists=matched.lineup_artists,
            tour_name=matched.tour_name,
            event_date=matched.event_date,
            setlist_fm_event_id=matched.setlist_fm_event_id,
            setlist_data=matched.raw_setlist_data,
            source_url=matched.source_url,
        )
        saved_concert = self.db.save_concert(concert)
        self.assertEqual(saved_concert.headliner_artist, "King Gizzard & The Lizard Wizard")

        # 5. Acoustic Identification
        ident = Identification(
            video_id=saved_video.id,
            concert_id=saved_concert.id,
            detected_song_title="Dragon",
            detected_artist="King Gizzard & The Lizard Wizard",
            album_name="PetroDragonic Apocalypse",
            match_confidence=0.95,
            fingerprint_service="shazamio",
            setlist_verified=True,
            track_number_in_setlist=5,
            raw_service_payload=SAMPLE_SHAZAM_PAYLOAD,
        )
        saved_ident = self.db.save_identification(ident)
        self.assertTrue(saved_ident.setlist_verified)

        # Stage for Review
        saved_video.processing_status = VideoStatus.PENDING_REVIEW
        self.db.save_video(saved_video)

        # Pre-generate YouTube Upload Draft
        gen = MetadataGenerator()
        yt_meta = gen.generate_metadata(saved_video, saved_ident, saved_concert, saved_venue)
        upload_job = YouTubeUpload(
            video_id=saved_video.id,
            title=yt_meta.title,
            description=yt_meta.description,
            tags=yt_meta.tags,
            category_id="10",
            privacy_status=PrivacyStatus.UNLISTED,
            upload_status="DRAFT",
            total_bytes=saved_video.file_size_bytes or 0,
        )
        self.db.save_youtube_upload(upload_job)

        # 6. Verify Review Queue Composite Fetch
        queue = self.db.get_review_queue()
        self.assertEqual(len(queue), 1)
        q_video, q_ident, q_concert, q_venue = queue[0]

        self.assertEqual(q_video.id, saved_video.id)
        self.assertEqual(q_ident.detected_song_title, "Dragon")
        self.assertEqual(q_concert.headliner_artist, "King Gizzard & The Lizard Wizard")
        self.assertEqual(q_venue.name, "Red Rocks Amphitheatre")

        # 7. Approve Candidate with Privacy Override
        saved_video.processing_status = VideoStatus.APPROVED
        self.db.save_video(saved_video)
        saved_upload = self.db.get_youtube_upload_by_video_id(saved_video.id)
        saved_upload.upload_status = "QUEUED"
        saved_upload.privacy_status = PrivacyStatus.PUBLIC
        self.db.save_youtube_upload(saved_upload)

        approved_list = self.db.list_videos(status=VideoStatus.APPROVED)
        self.assertEqual(len(approved_list), 1)

        # 8. Simulate Successful Publication
        saved_upload.youtube_video_id = "dQw4w9WgXcQ"
        saved_upload.youtube_url = "https://youtu.be/dQw4w9WgXcQ"
        saved_upload.upload_status = "COMPLETED"
        saved_upload.bytes_uploaded = saved_upload.total_bytes
        self.db.save_youtube_upload(saved_upload)

        saved_video.processing_status = VideoStatus.PUBLISHED
        self.db.save_video(saved_video)

        published_video = self.db.get_video(saved_video.id)
        self.assertEqual(published_video.processing_status, VideoStatus.PUBLISHED)
        published_upload = self.db.get_youtube_upload_by_video_id(saved_video.id)
        self.assertEqual(published_upload.youtube_video_id, "dQw4w9WgXcQ")
        self.assertEqual(published_upload.upload_status, "COMPLETED")


if __name__ == "__main__":
    unittest.main()
