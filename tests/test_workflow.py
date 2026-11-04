"""Regression checks for independent identity and publishing consent."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from stagesync.config import Settings
from stagesync.database.connection import Database
from stagesync.database.models import Video, Venue, Concert, Identification, VideoStatus, YouTubeUpload
from stagesync.pipeline import PipelineCoordinator
from stagesync.workflow import Workflow


class TestWorkflow(unittest.TestCase):
    """Exercise the real SQLite lifecycle with local provider fakes."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name)/'state.db')
        self.coord = PipelineCoordinator(db=self.db, settings=Settings())
        self.workflow = Workflow(self.coord)
        self.video = Video(source_photo_id='test',filename='clip.mp4',duration_seconds=50,processing_status=VideoStatus.PENDING_REVIEW)
        self.db.save_video(self.video)
        venue = Venue(name='Venue',city='Austin',country_code='US',latitude=30,longitude=-97)
        self.db.save_venue(venue)
        concert = Concert(venue_id=venue.id,headliner_artist='Marzomets',lineup_artists=['Marzomets','Selected Artist'],event_date='2014-06-13')
        self.db.save_concert(concert)
        self.ident = Identification(video_id=self.video.id,concert_id=concert.id,detected_artist='Selected Artist',detected_song_title='Selected Song',match_confidence=.98,status='USER_ASSISTED_BAND',fingerprint_service='manual')
        self.db.save_identification(self.ident)

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self):
        for field in ('venue','show','artist','song'):
            state=self.workflow.review(self.video.id)
            self.workflow.confirm(self.video.id,field,state['revision'])
        self.workflow.approve(self.video.id)

    def queue(self):
        self.workflow.draft(self.video.id,{'channel_ids':['channel-a']})
        with patch.object(self.workflow,'channels',return_value=({'channel-a':{'auth':Mock()}},[])):
            return self.workflow.enqueue(self.video.id)

    def test_order_and_explicit_queue(self):
        state=self.workflow.review(self.video.id)
        with self.assertRaisesRegex(ValueError,'in order'):
            self.workflow.confirm(self.video.id,'artist',state['revision'])
        self.approve()
        self.assertEqual(self.workflow.jobs(),[])
        self.assertEqual(self.db.get_video(self.video.id).processing_status,VideoStatus.APPROVED)
        self.queue()
        self.queue()
        self.assertEqual(len(self.workflow.jobs()),1)
        self.assertEqual(self.workflow.jobs()[0]['status'],'QUEUED')

    def test_revision_invalidation_preserves_text(self):
        self.approve()
        self.workflow.draft(self.video.id,{'title':'My edited title'})
        self.queue()
        self.ident.detected_artist='Different Artist'
        self.ident.detected_song_title='Live Performance'
        self.db.save_identification(self.ident)
        state=self.workflow.review(self.video.id)
        self.assertEqual(set(state['confirmations']),{'venue','show'})
        self.assertFalse(state['approved'])
        self.assertEqual(self.workflow.jobs()[0]['status'],'BLOCKED')
        self.assertEqual(self.workflow.draft(self.video.id)['title'],'My edited title')
        self.assertTrue(self.workflow.draft(self.video.id)['needs_review'])

    def test_atomic_claim_blocks_identity_changes(self):
        self.approve();self.queue()
        job=self.workflow.jobs()[0]
        self.assertTrue(self.workflow.claim(job))
        self.assertFalse(self.workflow.claim(job))
        self.ident.detected_song_title='Changed'
        with self.assertRaisesRegex(Exception,'upload is active'):
            self.db.save_identification(self.ident)

    def test_legacy_queue_is_not_consent(self):
        self.db.save_youtube_upload(YouTubeUpload(video_id=self.video.id,title='Legacy',description='',upload_status='QUEUED'))
        self.db.update_video_status(self.video.id,VideoStatus.APPROVED)
        with patch.object(self.workflow,'channels',return_value=({},[])):
            self.assertEqual(self.workflow.dispatch(),[])
        self.assertEqual(self.db.get_youtube_upload_by_video_id(self.video.id).upload_status,'QUEUED')

    def test_manual_confidence_and_flag_reject(self):
        self.assertIsNone(self.workflow.review(self.video.id)['confidence'])
        self.workflow.flag(self.video.id)
        self.assertTrue(Workflow(self.coord).review(self.video.id)['flagged'])
        self.workflow.reject_match(self.video.id)
        self.assertEqual(self.db.get_identification_by_video_id(self.video.id).detected_artist,'Selected Artist')
        self.assertNotEqual(self.db.get_video(self.video.id).processing_status,VideoStatus.REJECTED)

    def test_explicit_unknown(self):
        self.ident.detected_song_title='Live Performance';self.db.save_identification(self.ident)
        for field in ('venue','show','artist'):
            state=self.workflow.review(self.video.id);self.workflow.confirm(self.video.id,field,state['revision'])
        state=self.workflow.review(self.video.id)
        with self.assertRaises(ValueError):self.workflow.confirm(self.video.id,'song',state['revision'])
        self.workflow.confirm(self.video.id,'song',state['revision'],'unknown')
        self.workflow.approve(self.video.id)

    def test_timezone_transitions(self):
        with self.assertRaises(ValueError):self.workflow.local_release('2027-03-14T02:30')
        first=self.workflow.local_release('2027-11-07T01:30',0)
        second=self.workflow.local_release('2027-11-07T01:30',1)
        self.assertEqual((dt.datetime.fromisoformat(second)-dt.datetime.fromisoformat(first)).total_seconds(),3600)
        self.assertTrue(self.workflow.local_release('2027-01-10T18:00').startswith('2027-01-11T00:00'))

    def test_invalid_draft_and_metadata_independence(self):
        self.approve()
        with self.assertRaises(ValueError):self.workflow.draft(self.video.id,{'thumbnail_seconds':float('nan')})
        with self.assertRaises(ValueError):self.workflow.draft(self.video.id,{'publish_at':'2020-01-01T00:00:00Z'})
        self.workflow.draft(self.video.id,{'title':'Different publishing title'})
        self.assertEqual(self.db.get_identification_by_video_id(self.video.id).detected_song_title,'Selected Song')

    def test_resume_metadata_is_frozen(self):
        self.approve();self.queue()
        job=self.workflow.jobs()[0]
        self.workflow.set_job(job,'FAILED',upload_resumable_uri='https://example.test/resume')
        self.workflow.draft(self.video.id,{'privacy_status':'public'})
        with patch.object(self.workflow,'channels',return_value=({'channel-a':{}},[])):
            with self.assertRaisesRegex(ValueError,'original metadata'):
                self.workflow.enqueue(self.video.id)

    def test_resolution_change_revokes_consent_and_unknown_title(self):
        self.approve();self.queue()
        state=self.workflow.review(self.video.id)
        self.workflow.confirm(self.video.id,'song',state['revision'],'unknown')
        self.assertFalse(self.workflow.review(self.video.id)['approved'])
        self.assertEqual(self.workflow.jobs()[0]['status'],'BLOCKED')
        self.workflow.approve(self.video.id)
        draft=self.workflow.draft(self.video.id,regenerate=True)
        self.assertIn('Unknown track',draft['title'])
        self.assertNotIn('Selected Song',draft['title'])

    def test_channel_isolation_partial_failure_and_thumbnail_failure(self):
        from stagesync.publisher.resumable_upload import UploadResult
        self.approve()
        self.workflow.draft(self.video.id,{'channel_ids':['a','b'],'privacy_status':'public'})
        channels={'a':{'auth':Mock(name='auth-a')},'b':{'auth':Mock(name='auth-b')}}
        with patch.object(self.workflow,'channels',return_value=(channels,[])):
            self.workflow.enqueue(self.video.id)
            seen=[]
            def make_uploader(**kwargs):
                seen.append(kwargs['auth'])
                uploader=Mock()
                uploader.auth=kwargs['auth']
                uploader.upload_file.return_value=UploadResult(success=True,youtube_video_id='uploaded',youtube_url='https://youtu.be/uploaded') if kwargs['auth'] is channels['a']['auth'] else UploadResult(success=False,error_message='Provider refused')
                return uploader
            with patch('stagesync.workflow.ResumableUploader',side_effect=make_uploader), patch.object(self.workflow,'thumbnail',side_effect=ValueError('thumbnail denied')):
                self.workflow.dispatch()
            self.assertCountEqual(seen,[channels['a']['auth'],channels['b']['auth']])
            jobs={j['channel_id']:j for j in self.workflow.jobs()}
            self.assertEqual(jobs['a']['status'],'THUMBNAIL_FAILED')
            self.assertEqual(jobs['b']['status'],'FAILED')
            self.workflow.enqueue(self.video.id)
            self.assertEqual({j['channel_id']:j['status'] for j in self.workflow.jobs()},{'a':'THUMBNAIL_FAILED','b':'QUEUED'})

    def test_scheduled_upload_is_not_public(self):
        from stagesync.publisher.resumable_upload import UploadResult
        self.approve()
        future=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=5)).isoformat()
        self.workflow.draft(self.video.id,{'publish_at':future,'privacy_status':'private','channel_ids':['a']})
        with patch.object(self.workflow,'channels',return_value=({'a':{'auth':Mock()}},[])), patch('stagesync.workflow.ResumableUploader') as uploader, patch.object(self.workflow,'thumbnail'):
            uploader.return_value.upload_file.return_value=UploadResult(success=True,youtube_video_id='scheduled',youtube_url='https://youtu.be/scheduled')
            self.workflow.enqueue(self.video.id)
            self.workflow.dispatch()
        self.assertEqual(self.db.get_video(self.video.id).processing_status,VideoStatus.SCHEDULED)
        self.assertEqual(self.workflow.jobs()[0]['status'],'SCHEDULED')

    def test_stale_confirmation_and_stale_claim(self):
        old=self.workflow.review(self.video.id)
        self.ident.detected_artist='Changed';self.db.save_identification(self.ident)
        with self.assertRaisesRegex(ValueError,'changed'):
            self.workflow.confirm(self.video.id,'venue',old['revision'])
        self.approve();self.queue()
        job=self.workflow.jobs()[0]
        self.ident.detected_song_title='New';self.db.save_identification(self.ident)
        self.assertFalse(self.workflow.claim(job))

    def test_defaults_do_not_replace_existing_drafts(self):
        self.approve()
        before=self.workflow.draft(self.video.id)['description']
        self.workflow.defaults({'description':'new default'})
        self.assertEqual(self.workflow.draft(self.video.id)['description'],before)

from aiohttp.test_utils import AioHTTPTestCase
from stagesync.web.server import create_app


class TestWorkflowHTTP(AioHTTPTestCase):
    """Check real HTTP contract against mixed publication fixtures."""
    async def get_application(self):
        self.fixture=TestWorkflow()
        self.fixture.setUp()
        return create_app(coordinator=self.fixture.coord,db=self.fixture.db,settings=self.fixture.coord.settings)

    async def tearDownAsync(self):
        await super().tearDownAsync()
        self.fixture.tearDown()

    async def test_overview_queued_and_uploaded_thumbnail_failure(self):
        self.fixture.approve();self.fixture.queue()
        response=await self.client.get('/api/workflow/overview')
        self.assertEqual(response.status,200)
        payload=await response.json()
        self.assertEqual(payload['queued_jobs'],1)
        self.assertEqual(payload['published_uploads'],0)
        job=self.fixture.workflow.jobs()[0]
        self.fixture.workflow.set_job(job,'THUMBNAIL_FAILED',youtube_video_id='uploaded',privacy_status='public')
        response=await self.client.get('/api/workflow/overview')
        self.assertEqual((await response.json())['published_uploads'],1)

    async def test_overview_refreshes_stale_queue_before_counting(self):
        self.fixture.approve();self.fixture.queue()
        self.fixture.ident.detected_artist='Changed'
        self.fixture.db.save_identification(self.fixture.ident)
        response=await self.client.get('/api/workflow/overview')
        payload=await response.json()
        self.assertEqual(payload['queued_jobs'],0)
        self.assertEqual(payload['approved_clips'],0)
        self.assertEqual(payload['jobs'][0]['status'],'BLOCKED')

    async def test_approval_http_never_queues(self):
        video_id=self.fixture.video.id
        for field in ('venue','show','artist','song'):
            state=self.fixture.workflow.review(video_id)
            response=await self.client.post(f'/api/workflow/{video_id}/confirm',json={'field':field,'revision':state['revision']})
            self.assertEqual(response.status,200)
        response=await self.client.post(f'/api/workflow/{video_id}/approve',json={})
        self.assertEqual(response.status,200)
        self.assertEqual(self.fixture.workflow.jobs(),[])
