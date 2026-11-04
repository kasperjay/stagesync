import tempfile
import unittest
from pathlib import Path

from stagesync.database.connection import Database
from stagesync.database.models import Video, Concert, Identification


class TestDateContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'test.db')

    def video(self, name, local='2026-11-03T21:00:00', utc='2026-11-04T03:00:00Z'):
        video = Video(source_photo_id=name, filename=name, recorded_at_local=local, recorded_at_utc=utc)
        self.db.save_video(video)
        return video

    def match(self, video, concert):
        ident = self.db.get_identification_by_video_id(video.id) or Identification(
            video_id=video.id, detected_artist='Opener', detected_song_title='Song', match_confidence=.8)
        ident.concert_id = concert.id
        self.db.save_identification(ident)

    def test_match_manual_override_and_later_ingestion(self):
        a, b = self.video('a'), self.video('b')
        first = self.db.save_concert(Concert(headliner_artist='First', event_date='2026-11-03'))
        second = self.db.save_concert(Concert(headliner_artist='Second', lineup_artists=['Second','Opener'], event_date='2026-11-03'))
        self.match(a, first)
        self.assertEqual(self.db.get_identification_by_video_id(b.id).concert_id, first.id)
        self.match(b, second)
        c = self.video('c')
        for video in (a,b,c):
            self.assertEqual(self.db.get_identification_by_video_id(video.id).concert_id, second.id)
        ident = self.db.get_identification_by_video_id(a.id)
        self.assertEqual((ident.detected_artist, ident.detected_song_title), ('Opener','Song'))

    def test_local_date_takes_precedence_and_missing_dates_do_not_group(self):
        a = self.video('a')
        b = self.video('b', local='2026-11-04T20:00:00')
        c = self.video('c', local=None, utc=None)
        concert = self.db.save_concert(Concert(headliner_artist='First', event_date='2026-11-03'))
        self.match(a, concert)
        self.assertIsNone(self.db.get_identification_by_video_id(b.id))
        self.assertIsNone(self.db.get_identification_by_video_id(c.id))

    def test_unmatched_audio_result_inherits_date_context(self):
        a = self.video('a')
        concert = self.db.save_concert(Concert(headliner_artist='First', event_date='2026-11-03'))
        self.match(a, concert)
        b = self.video('b')
        ident = Identification(video_id=b.id, detected_artist='Band', detected_song_title='Track', match_confidence=.99)
        self.db.save_identification(ident)
        self.assertEqual(ident.concert_id, concert.id)
