"""Admission, migration, retries and all ingestion entrypoints use real isolated SQLite."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from stagesync.config import Settings
from stagesync.database.connection import Database
from stagesync.database.models import Video, VideoStatus, YouTubeUpload
from stagesync.pipeline import PipelineCoordinator
from stagesync.screening import Screening
from stagesync.workflow import Workflow

ACCEPT = dict(decision='accept', confidence=.99, visual_live_performance=True,
              audio_live_performance=True, reason='Live stage and live performance audio')


class TestScreeningGate(unittest.TestCase):
    """Verify safety boundaries independently from identification/provider behavior."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root/'state.db')
        self.coord = PipelineCoordinator(db=self.db, settings=Settings(cache_dir=self.root/'cache'))
        self.service = Screening(self.coord)
        self.media = self.root/'clip.mp4'
        self.media.write_bytes(b'retained original')
        self.video = Video(source_photo_id='test', filename='clip.mp4', file_path=str(self.media))

    def test_uncertain_retry_accepts_without_deleting_original(self):
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value={'decision':'review','reason':'Missing credentials'}):
            self.assertFalse(self.service.admit(self.video))
        self.assertIsNone(self.db.get_video(self.video.id))
        self.assertEqual(self.db.list_videos(), [])
        self.assertEqual(self.service.list()[0]['reason'], 'Missing credentials')
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value=ACCEPT):
            self.service.retry(self.video.id)
        self.assertEqual(len(self.db.list_videos()), 1)
        self.assertEqual(self.service.list()[0]['attempts'], 2)
        self.assertEqual(self.media.read_bytes(), b'retained original')

    def test_exception_and_malformed_accept_fail_closed(self):
        with patch('stagesync.intelligence.concert_screen.screen_concert', side_effect=RuntimeError('secret')):
            self.assertFalse(self.service.admit(self.video))
        self.assertIn('RuntimeError', self.service.list()[0]['reason'])
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value={'decision':'accept'}):
            self.assertFalse(self.service.admit(self.video))
        self.assertEqual(self.db.list_videos(), [])

    def test_missing_media_visible_and_retryable(self):
        self.video.file_path = None
        self.assertFalse(self.service.admit(self.video))
        self.assertIn('Local media unavailable', self.service.list()[0]['reason'])

    def test_migration_audit_retains_published_history_and_edits(self):
        self.video.processing_status = VideoStatus.PUBLISHED
        self.db.save_video(self.video)
        upload = YouTubeUpload(video_id=self.video.id, title='User title', description='User text', youtube_video_id='original')
        self.db.save_youtube_upload(upload)
        with self.db.transaction() as cur:
            cur.execute('DELETE FROM concert_screenings')
        self.db.init_db()
        self.db.init_db()
        self.assertEqual(len(self.service.list()), 1)
        self.assertEqual(self.db.list_videos(), [])
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value={**ACCEPT,'decision':'reject'}):
            list(self.service.audit())
        self.assertEqual(self.db.get_video(self.video.id).processing_status, VideoStatus.PUBLISHED)
        self.assertEqual(self.db.get_youtube_upload_by_video_id(self.video.id).youtube_video_id, 'original')
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value=ACCEPT):
            list(self.service.audit(retry=True))
        self.assertEqual(self.db.get_youtube_upload_by_video_id(self.video.id).title, 'User title')
        self.assertEqual(self.db.list_videos()[0].processing_status, VideoStatus.PUBLISHED)

    def test_pending_cannot_process_approve_or_publish(self):
        self.db.save_video(self.video)
        workflow = Workflow(self.coord)
        for call in [lambda:self.coord.process_video(self.video), lambda:workflow.approve(self.video.id), lambda:workflow.enqueue(self.video.id)]:
            with self.assertRaisesRegex(ValueError, 'screening'):
                call()
        self.assertFalse(workflow.claim({'video_id':self.video.id}))

    def test_active_lease_prevents_duplicate_classifier_work(self):
        self.db.save_video(self.video)
        import datetime as dt
        with self.db.transaction() as cur:
            cur.execute("UPDATE concert_screenings SET decision='screening',updated_at=?", (dt.datetime.now(dt.timezone.utc).isoformat(),))
        with patch('stagesync.intelligence.concert_screen.screen_concert') as classifier:
            self.assertFalse(self.service.admit(self.video))
            classifier.assert_not_called()
        with self.db.transaction() as cur:
            cur.execute("UPDATE concert_screenings SET updated_at='2000-01-01'")
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value=ACCEPT):
            self.assertTrue(self.service.admit(self.video))

    def test_every_source_uses_shared_gate_and_dry_runs_are_unsaved(self):
        for source in ('photos','picker','drive','local'):
            for accepted in (False, True):
                with self.subTest(source=source, accepted=accepted):
                    # Separate databases prevent source IDs or content deduplication masking a gate.
                    self.coord.db = Database(self.root/f'{source}-{accepted}.db')
                    self.coord.photos_client = MagicMock()
                    self.coord.drive_client = MagicMock()
                    self.coord.metadata_parser = MagicMock()
                    self.coord.metadata_parser.probe_file.side_effect = RuntimeError('No fixture metadata')
                    self.coord.spatial_correlator = MagicMock()
                    self.coord.spatial_correlator.scan_local_directory_for_photos.return_value = []
                    item = {'id':'item','name':'clip.mp4','filename':'clip.mp4','baseUrl':'https://example.invalid/media','size':'5000000'}
                    self.coord.photos_client.list_videos.return_value = ([item], None)
                    self.coord.drive_client.list_videos.return_value = ([item], None)
                    self.coord.photos_client.download_video.return_value = (self.media, 'hash')
                    self.coord.drive_client.download_video.return_value = (self.media, 'hash')
                    run = {'photos':lambda:self.coord.scan_google_photos(), 'picker':lambda:self.coord.ingest_picker_items([item]), 'drive':lambda:self.coord.scan_google_drive(), 'local':lambda:self.coord.scan_local_directory(self.root)}[source]
                    with patch('stagesync.intelligence.concert_screen.screen_concert', return_value={**ACCEPT,'decision':'accept' if accepted else 'review'}) as classifier:
                        result = run()
                    self.assertEqual(len(result), int(accepted))
                    classifier.assert_called_once()
                    self.assertEqual(len(self.coord.db.list_videos()), int(accepted))
                    if source!='local':
                        dry = {'photos':lambda:self.coord.scan_google_photos(dry_run=True), 'picker':lambda:self.coord.ingest_picker_items([dict(item,id='dry')],dry_run=True), 'drive':lambda:self.coord.scan_google_drive(dry_run=True)}[source]
                        before = len(Screening(self.coord).list())
                        dry()
                        self.assertEqual(len(Screening(self.coord).list()),before)

    def test_changed_media_invalidates_cached_acceptance(self):
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value=ACCEPT) as classifier:
            self.assertTrue(self.service.admit(self.video))
            self.assertTrue(self.service.admit(self.video))
            classifier.assert_called_once()
        self.media.write_bytes(b'changed personal footage')
        with patch('stagesync.intelligence.concert_screen.screen_concert', return_value={**ACCEPT,'decision':'reject'}) as classifier:
            self.assertFalse(self.service.admit(self.video))
            classifier.assert_called_once()
        self.assertEqual(self.db.list_videos(), [])

    def test_missing_legacy_media_is_redownloaded_without_changing_status(self):
        for source in ('photos','picker','drive'):
            with self.subTest(source=source):
                self.coord.db = Database(self.root/f'recovery-{source}.db')
                video = Video(source_photo_id='drive_item' if source=='drive' else 'item',
                              filename='clip.mp4',file_path='/missing/clip.mp4',processing_status=VideoStatus.PUBLISHED)
                self.coord.db.save_video(video)
                item = {'id':'item','name':'clip.mp4','filename':'clip.mp4','baseUrl':'https://example.invalid/media'}
                self.coord.photos_client = MagicMock()
                self.coord.drive_client = MagicMock()
                self.coord.metadata_parser = MagicMock()
                self.coord.metadata_parser.probe_file.side_effect = RuntimeError('fixture')
                self.coord.photos_client.list_videos.return_value = ([item],None)
                self.coord.drive_client.list_videos.return_value = ([item],None)
                self.coord.photos_client.download_video.return_value = (self.media,source)
                self.coord.drive_client.download_video.return_value = (self.media,source)
                run = {'photos':lambda:self.coord.scan_google_photos(), 'picker':lambda:self.coord.ingest_picker_items([item]), 'drive':lambda:self.coord.scan_google_drive()}[source]
                with patch('stagesync.intelligence.concert_screen.screen_concert',return_value=ACCEPT):
                    run()
                saved = self.coord.db.get_video(video.id)
                self.assertEqual(saved.file_path,str(self.media))
                self.assertEqual(saved.processing_status,VideoStatus.PUBLISHED)
                self.assertEqual(len(self.coord.db.list_videos()),1)

    def test_replaced_accepted_media_cannot_approve_or_dispatch(self):
        for operation in ('process','approve','enqueue','claim'):
            with self.subTest(operation=operation):
                self.media.write_bytes(b'original concert')
                with patch('stagesync.intelligence.concert_screen.screen_concert',return_value=ACCEPT):
                    self.service.admit(self.video)
                self.media.write_bytes(b'personal replacement footage')
                workflow = Workflow(self.coord)
                if operation == 'claim':
                    self.assertFalse(workflow.claim({'video_id':self.video.id}))
                else:
                    with self.assertRaisesRegex(ValueError,'screening'):
                        if operation == 'process':
                            self.coord.process_video(self.video)
                        else:
                            getattr(workflow,operation)(self.video.id)
                self.assertEqual(self.db.list_videos(),[])
                self.assertIn('changed',self.service.list()[0]['reason'])

    def test_local_dry_run_leaves_no_receipts_or_catalog(self):
        self.coord.spatial_correlator = MagicMock()
        self.assertEqual(len(self.coord.scan_local_directory(self.root,dry_run=True)),1)
        self.assertEqual(self.service.list(),[])
        self.assertEqual(self.db.list_videos(),[])

    def test_recovered_media_remains_retryable_after_uncertain_result(self):
        self.video.file_path = '/missing/clip.mp4'
        self.db.save_video(self.video)
        self.video.file_path = str(self.media)
        with patch('stagesync.intelligence.concert_screen.screen_concert',return_value={**ACCEPT,'decision':'review'}):
            self.service.admit(self.video)
        self.assertEqual(self.db.get_video(self.video.id).file_path,str(self.media))
        with patch('stagesync.intelligence.concert_screen.screen_concert',return_value=ACCEPT) as classifier:
            self.service.retry(self.video.id)
            classifier.assert_called_once_with(self.media)
        self.assertTrue(self.db.screening_accepted(self.video.id))

    def test_recovered_path_persists_when_bytes_match_accepted_receipt(self):
        with patch('stagesync.intelligence.concert_screen.screen_concert',return_value=ACCEPT):
            self.service.admit(self.video)
        moved = self.root/'recovered.mp4'
        self.media.rename(moved)
        self.video.file_path = str(moved)
        with patch('stagesync.intelligence.concert_screen.screen_concert') as classifier:
            self.assertTrue(self.service.admit(self.video))
            classifier.assert_not_called()
        self.assertEqual(self.db.get_video(self.video.id).file_path,str(moved))


from aiohttp.test_utils import AioHTTPTestCase
from stagesync.web.server import create_app


class TestScreeningHTTP(AioHTTPTestCase):
    """Verify held records, reasons and explicit retry through production routes."""
    async def get_application(self):
        self.fixture = TestScreeningGate()
        self.fixture.setUp()
        self.fixture.db.save_video(self.fixture.video)
        return create_app(coordinator=self.fixture.coord,db=self.fixture.db,settings=self.fixture.coord.settings)

    async def tearDownAsync(self):
        await super().tearDownAsync()
        self.fixture.doCleanups()

    async def test_held_queue_retry_and_library_admission(self):
        response = await self.client.get('/api/screening')
        payload = await response.json()
        self.assertEqual(payload['items'][0]['decision'],'pending')
        self.assertTrue(payload['items'][0]['reason'])
        self.assertEqual(self.fixture.db.list_videos(),[])
        response = await self.client.get('/screening')
        self.assertEqual(response.status,200)
        self.assertIn('Retry screening',await response.text())
        with patch('stagesync.intelligence.concert_screen.screen_concert',return_value=ACCEPT):
            response = await self.client.post(f'/api/screening/{self.fixture.video.id}/retry')
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['decision'],'accept')
        self.assertEqual(len(self.fixture.db.list_videos()),1)
        response = await self.client.post('/api/screening/not-found/retry')
        self.assertEqual(response.status,404)
