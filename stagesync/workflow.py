"""Identity approval and explicit publication state machine."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
import functools
import math
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

from stagesync.database.models import PrivacyStatus, VideoStatus, YouTubeUpload
from stagesync.publisher.metadata_gen import FormattedYouTubeMetadata
from stagesync.publisher.resumable_upload import ResumableUploader, UploadResult
from stagesync.publisher.youtube_auth import YouTubeAuth

FIELDS = ('venue', 'show', 'artist', 'song')


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def atomic(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        conn = self.db._get_connection()
        if conn.in_transaction:
            return method(self, *args, **kwargs)
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = method(self, *args, **kwargs)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
    return wrapped


class Workflow:
    """Persist ordered review and isolated destination jobs."""
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.db = coordinator.db
        self.db._get_connection().executescript('''
            CREATE TABLE IF NOT EXISTS identity_reviews (
                video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                revision TEXT NOT NULL, confirmations TEXT NOT NULL DEFAULT '{}',
                approved_revision TEXT, flagged INTEGER NOT NULL DEFAULT 0,
                resolution TEXT NOT NULL DEFAULT 'identified', original_artist TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS publication_drafts (
                video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                revision TEXT NOT NULL, data TEXT NOT NULL, needs_review INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS publication_jobs (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                channel_id TEXT NOT NULL, revision TEXT NOT NULL, status TEXT NOT NULL,
                data TEXT NOT NULL, UNIQUE(video_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS publication_defaults (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        ''')

        conn = self.db._get_connection()
        conn.execute("""CREATE TRIGGER IF NOT EXISTS protect_active_resolution
            BEFORE UPDATE OF resolution,original_artist ON identity_reviews
            WHEN (NEW.resolution != OLD.resolution OR NEW.original_artist != OLD.original_artist)
            AND EXISTS(SELECT 1 FROM publication_jobs WHERE video_id=NEW.video_id AND status='UPLOADING')
            BEGIN SELECT RAISE(ABORT, 'An upload is active; resolution edits are temporarily blocked'); END""")
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            ref = 'OLD' if operation == 'DELETE' else 'NEW'
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS protect_active_identity_{operation}
                BEFORE {operation} ON identifications
                WHEN EXISTS(SELECT 1 FROM publication_jobs WHERE video_id={ref}.video_id AND status='UPLOADING')
                BEGIN SELECT RAISE(ABORT, 'An upload is active; identity edits are temporarily blocked'); END""")
        for table, relation in (('concerts', 'i.concert_id=OLD.id'), ('venues', 'i.concert_id IN (SELECT id FROM concerts WHERE venue_id=OLD.id)')):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS protect_active_{table}
                BEFORE UPDATE ON {table}
                WHEN EXISTS(SELECT 1 FROM identifications i JOIN publication_jobs j ON j.video_id=i.video_id
                WHERE {relation} AND j.status='UPLOADING')
                BEGIN SELECT RAISE(ABORT, 'An upload is active; event edits are temporarily blocked'); END""")

    def identity(self, video_id):
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError('Video not found')
        ident = self.db.get_identification_by_video_id(video_id)
        concert = self.db.get_concert(ident.concert_id) if ident and ident.concert_id else None
        venue = self.db.get_venue(concert.venue_id) if concert and concert.venue_id else None
        if not venue and video.latitude is not None and video.longitude is not None:
            venue = self.db.find_venue_by_coords(video.latitude, video.longitude)
        values = {
            'venue': {'id': venue.id, 'name': venue.name} if venue else None,
            'show': {'id': concert.id, 'date': concert.event_date, 'name': concert.tour_name or concert.headliner_artist} if concert else None,
            'artist': ident.detected_artist if ident else '',
            'song': ident.detected_song_title if ident else '',
        }
        review_row = self.db._get_connection().execute('SELECT resolution,original_artist FROM identity_reviews WHERE video_id=?', (video_id,)).fetchone()
        semantics = dict(review_row) if review_row else {'resolution':'identified','original_artist':''}
        revision = hashlib.sha256(json.dumps({'values':values, **semantics}, sort_keys=True).encode()).hexdigest()
        return video, ident, concert, venue, values, revision

    @atomic
    def review(self, video_id):
        video, ident, concert, venue, values, revision = self.identity(video_id)
        with self.db.transaction() as cur:
            row = cur.execute('SELECT * FROM identity_reviews WHERE video_id=?', (video_id,)).fetchone()
            if not row:
                cur.execute('INSERT INTO identity_reviews(video_id,revision) VALUES (?,?)', (video_id, revision))
                confirmations, approved, flagged, resolution, original = {}, None, False, 'identified', ''
            else:
                confirmations = json.loads(row['confirmations'])
                approved, flagged, resolution, original = row['approved_revision'], bool(row['flagged']), row['resolution'], row['original_artist']
                if row['revision'] != revision:
                    invalidate = False
                    for field in FIELDS:
                        invalidate = invalidate or confirmations.get(field) != values[field]
                        if invalidate:
                            confirmations.pop(field, None)
                    approved = None
                    cur.execute('UPDATE identity_reviews SET revision=?, confirmations=?, approved_revision=NULL WHERE video_id=?', (revision, json.dumps(confirmations), video_id))
                    cur.execute('UPDATE publication_drafts SET needs_review=1 WHERE video_id=?', (video_id,))
                    cur.execute("UPDATE publication_jobs SET status='BLOCKED' WHERE video_id=? AND status IN ('QUEUED','FAILED')", (video_id,))
                    cur.execute("UPDATE videos SET processing_status='PENDING_REVIEW' WHERE id=? AND processing_status='APPROVED'", (video_id,))
        return dict(video_id=video_id, revision=revision, values=values, confirmations=confirmations,
                    approved=approved == revision, flagged=flagged, resolution=resolution, original_artist=original,
                    evidence=ident.raw_service_payload if ident else {},
                    source=ident.fingerprint_service if ident else None,
                    confidence=None if ident and ident.fingerprint_service in ('manual','user_assisted_band') else ident.match_confidence if ident else None)

    @atomic
    def confirm(self, video_id, field, revision, resolution='identified', original_artist=''):
        state = self.review(video_id)
        if revision != state['revision']:
            raise ValueError('Identity changed. Reload before confirming.')
        if field not in FIELDS:
            raise ValueError('Unknown approval step')
        if any(f not in state['confirmations'] for f in FIELDS[:FIELDS.index(field)]):
            raise ValueError('Confirm Venue → Show → Artist → Song in order')
        value = state['values'][field]
        if not value or (field == 'artist' and not self.coordinator._usable_artist(value)):
            raise ValueError('Select a value before confirming')
        if field == 'song':
            if resolution not in ('identified', 'manual', 'cover', 'improvisation', 'unknown'):
                raise ValueError('Invalid song resolution')
            if resolution in ('identified','manual','cover') and not self.coordinator._usable_song(value):
                raise ValueError('Choose a song or explicitly resolve it as unknown/improvisation')
            if resolution == 'cover' and not original_artist.strip():
                raise ValueError('Record the original artist for this cover')
        state['confirmations'][field] = value
        with self.db.transaction() as cur:
            cur.execute('UPDATE identity_reviews SET confirmations=?, resolution=?, original_artist=? WHERE video_id=? AND revision=?',
                        (json.dumps(state['confirmations']), resolution if field == 'song' else state['resolution'], original_artist if field == 'song' else state['original_artist'], video_id, revision))
        return self.review(video_id)

    @atomic
    def approve(self, video_id):
        state = self.review(video_id)
        if any(f not in state['confirmations'] for f in FIELDS):
            raise ValueError('Confirm Venue → Show → Artist → Song before approving')
        with self.db.transaction() as cur:
            cur.execute('UPDATE identity_reviews SET approved_revision=revision, flagged=0 WHERE video_id=?', (video_id,))
            cur.execute("UPDATE videos SET processing_status='APPROVED' WHERE id=? AND processing_status NOT IN ('PUBLISHED','UPLOADING')", (video_id,))
        self.draft(video_id)
        return self.db.get_video(video_id)

    @atomic
    def flag(self, video_id, flagged=True):
        self.review(video_id)
        with self.db.transaction() as cur:
            cur.execute('UPDATE identity_reviews SET flagged=? WHERE video_id=?', (int(flagged), video_id))
        return self.review(video_id)

    @atomic
    def reject_match(self, video_id):
        ident = self.db.get_identification_by_video_id(video_id)
        if not ident:
            raise ValueError('No candidate to reject')
        ident.detected_song_title = 'Live Performance'
        ident.match_confidence = 0
        ident.status = 'USER_ASSISTED_BAND'
        ident.fingerprint_service = 'manual'
        ident.raw_service_payload = {'rejected_candidate': True}
        self.db.save_identification(ident)
        self.db.update_video_status(video_id, VideoStatus.PENDING_REVIEW)
        return self.review(video_id)

    def defaults(self, data=None):
        if data is not None:
            data = {k: data[k] for k in ('description','tags','privacy_status') if k in data}
            with self.db.transaction() as cur:
                cur.execute('INSERT INTO publication_defaults VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (json.dumps(data),))
        row = self.db._get_connection().execute('SELECT data FROM publication_defaults WHERE id=1').fetchone()
        return json.loads(row['data']) if row else {}

    @atomic
    def draft(self, video_id, edits=None, regenerate=False):
        state = self.review(video_id)
        row = self.db._get_connection().execute('SELECT * FROM publication_drafts WHERE video_id=?', (video_id,)).fetchone()
        if row and not regenerate:
            data = json.loads(row['data'])
        else:
            if not state['approved']:
                raise ValueError('Approve identity before preparing publication')
            video, ident, concert, venue, _, _ = self.identity(video_id)
            if ident and state['resolution'] in ('unknown','improvisation'):
                ident = ident.model_copy(update={'detected_song_title':'Unknown track' if state['resolution']=='unknown' else 'Improvisation'})
            meta = self.coordinator.metadata_gen.generate_metadata(video, ident, concert, venue)
            if state['resolution'] == 'cover':
                meta.description += '\n\nCover performance; original recording artist: ' + state['original_artist']
            data = dict(title=meta.title, description=meta.description, tags=meta.tags,
                        privacy_status=self.coordinator.settings.default_privacy, thumbnail_seconds=1,
                        publish_at=None, channel_ids=[])
            data.update(self.defaults())
        if edits is not None:
            if not isinstance(edits, dict):
                raise ValueError('Draft must be an object')
            for key in ('title','description','tags','privacy_status','thumbnail_seconds','publish_at','channel_ids'):
                if key in edits:
                    data[key] = edits[key]
            if 'publish_local' in edits:
                data['publish_at'] = self.local_release(edits['publish_local'], edits.get('fold',0)) if edits['publish_local'] else None
            self.validate_draft(data, enqueue=False)
        if row is None or edits is not None or regenerate:
            if not state['approved']:
                raise ValueError('Approve current identity before saving publication')
            with self.db.transaction() as cur:
                cur.execute('INSERT INTO publication_drafts VALUES (?,?,?,0) ON CONFLICT(video_id) DO UPDATE SET revision=excluded.revision,data=excluded.data,needs_review=0', (video_id, state['revision'], json.dumps(data)))
        return dict(**data, needs_review=bool(row['needs_review']) if row and edits is None and not regenerate else False, revision=state['revision'])

    @staticmethod
    def local_release(value, fold=0):
        local = dt.datetime.fromisoformat(value)
        if local.tzinfo is not None:
            raise ValueError('Enter a local Chicago time without a timezone suffix')
        zone = ZoneInfo('America/Chicago')
        aware = local.replace(tzinfo=zone, fold=int(fold))
        if aware.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
            raise ValueError('This Chicago time does not exist during daylight-saving transition')
        return aware.astimezone(dt.timezone.utc).isoformat()

    def validate_draft(self, data, enqueue=True):
        if not isinstance(data.get('title'), str) or not data['title'].strip() or len(data['title']) > 100 or any(c in data['title'] for c in '<>'):
            raise ValueError('Title must contain 1–100 characters without angle brackets')
        if not isinstance(data.get('description'), str) or len(data['description'].encode()) > 5000:
            raise ValueError('Description must be at most 5000 bytes')
        tags = data.get('tags')
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags) or sum(len(t)+1 for t in tags) > 500:
            raise ValueError('Tags must be a list totaling at most 500 characters')
        PrivacyStatus(data.get('privacy_status'))
        seconds = data.get('thumbnail_seconds', 1)
        if not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Thumbnail position must be nonnegative')
        if not isinstance(data.get('channel_ids'), list) or any(not isinstance(c,str) for c in data['channel_ids']):
            raise ValueError('Select valid channels')
        if enqueue and not data['channel_ids']:
            raise ValueError('Select at least one authenticated YouTube channel')
        if data.get('publish_at'):
            when = dt.datetime.fromisoformat(data['publish_at'].replace('Z','+00:00'))
            if when.tzinfo is None or when <= utcnow():
                raise ValueError('Scheduled release must have an explicit timezone and be in the future')
            if data['privacy_status'] != 'private':
                raise ValueError('Scheduled releases must upload privately')
            data['publish_at'] = when.astimezone(dt.timezone.utc).isoformat()

    def channels(self):
        configured = [self.coordinator.uploader.auth]
        token_path = self.coordinator.settings.youtube_token_path
        directory = Path(token_path).parent / 'channels'
        if directory.exists():
            configured.extend(YouTubeAuth(token_path=p) for p in sorted(directory.glob('*.json')))
        channels, errors = {}, []
        for auth in configured:
            if not auth.is_authenticated():
                continue
            try:
                response = requests.get('https://www.googleapis.com/youtube/v3/channels', params={'part':'snippet','mine':'true'}, headers={'Authorization': 'Bearer ' + auth.get_access_token()}, timeout=20)
                response.raise_for_status()
                for item in response.json().get('items', []):
                    channels[item['id']] = {'id':item['id'], 'title':item['snippet']['title'], 'auth':auth}
            except Exception as exc:
                errors.append('Channel identity unavailable; reconnect YouTube or retry.')
        return channels, errors

    @atomic
    def enqueue(self, video_id):
        state = self.review(video_id)
        if not state['approved']:
            raise ValueError('Current identity is not approved')
        draft = self.draft(video_id)
        if draft['needs_review']:
            raise ValueError('Review and save the changed draft first')
        self.validate_draft(draft)
        video = self.db.get_video(video_id)
        if draft['thumbnail_seconds'] > (video.duration_seconds or 0):
            raise ValueError('Thumbnail position is outside the clip')
        channels, _ = self.channels()
        if any(c not in channels for c in draft['channel_ids']):
            raise ValueError('A selected YouTube channel is unavailable')
        with self.db.transaction() as cur:
            for channel_id in set(draft['channel_ids']):
                row = cur.execute('SELECT * FROM publication_jobs WHERE video_id=? AND channel_id=?', (video_id, channel_id)).fetchone()
                if row and row['status'] in ('QUEUED','UPLOADING','COMPLETED','SCHEDULED','THUMBNAIL_FAILED'):
                    continue
                data = {k:v for k,v in draft.items() if k not in ("revision", "needs_review")}
                if row:
                    previous = json.loads(row['data'])
                    if previous.get('youtube_video_id'):
                        raise ValueError('This destination already has an uploaded video')
                    if previous.get('upload_resumable_uri') and any(previous.get(k) != data.get(k) for k in ('title','description','tags','privacy_status','publish_at')):
                        raise ValueError('An interrupted upload retains its original metadata. Restore that draft before retrying; do not start a duplicate upload.')
                    if row['revision'] == state['revision']:
                        data.update({k:previous[k] for k in ('upload_resumable_uri','bytes_uploaded') if k in previous})
                job_id = row['id'] if row else str(uuid.uuid4())
                cur.execute('INSERT INTO publication_jobs VALUES (?,?,?,?,?,?) ON CONFLICT(video_id,channel_id) DO UPDATE SET revision=excluded.revision,status=excluded.status,data=excluded.data', (job_id, video_id, channel_id, state['revision'], 'QUEUED', json.dumps(data)))
        return self.jobs(video_id)

    def jobs(self, video_id=None):
        sql = 'SELECT * FROM publication_jobs'
        rows = self.db._get_connection().execute(sql + (' WHERE video_id=?' if video_id else ''), (video_id,) if video_id else ()).fetchall()
        return [dict(id=r['id'], video_id=r['video_id'], channel_id=r['channel_id'], revision=r['revision'], status=r['status'], **json.loads(r['data'])) for r in rows]

    def dispatch(self, limit=10, progress_callback=None):
        results = []
        self.recover_jobs()
        channels, _ = self.channels()
        self.refresh_releases(channels)
        for job in self.jobs():
            if len(results) >= limit or job['status'] != 'QUEUED':
                continue
            state = self.review(job['video_id'])
            if not state['approved'] or state['revision'] != job['revision']:
                continue
            try:
                self.validate_draft(job)
                if job['channel_id'] not in channels:
                    raise ValueError('Selected channel is unavailable')
            except ValueError as exc:
                self.set_job(job, 'BLOCKED', error_message=str(exc))
                continue
            if not self.claim(job):
                continue
            video = self.db.get_video(job['video_id'])
            adapter = JobDatabase(self, job)
            uploader = ResumableUploader(auth=channels[job['channel_id']]['auth'], db=adapter, max_daily_uploads=self.coordinator.settings.max_daily_uploads)
            meta = FormattedYouTubeMetadata(title=job['title'], description=job['description'], tags=job['tags'], publish_at=job.get('publish_at'), recording_date=video.recorded_at_utc or video.recorded_at_local)
            try:
                result = uploader.upload_file(video.id, video.file_path or '', meta, privacy=PrivacyStatus(job['privacy_status']), progress_callback=progress_callback, existing_resumable_uri=job.get('upload_resumable_uri'))
                if result.success:
                    job.update(youtube_video_id=result.youtube_video_id, youtube_url=result.youtube_url)
                    self.set_job(job, 'SCHEDULED' if job.get('publish_at') else 'COMPLETED')
                    self.db.update_video_status(video.id, VideoStatus.SCHEDULED if job.get('publish_at') else VideoStatus.PUBLISHED if job['privacy_status'] == 'public' else VideoStatus.UPLOADED)
                    try:
                        self.thumbnail(job, uploader.auth)
                    except Exception as exc:
                        self.set_job(job, 'THUMBNAIL_FAILED', error_message='Video uploaded; thumbnail failed: ' + str(exc))
                else:
                    self.set_job(job, 'FAILED', error_message=result.error_message)
                results.append(result)
            except Exception as exc:
                self.set_job(job, 'FAILED', error_message=str(exc))
                results.append(UploadResult(success=False, error_message=str(exc)))
        return results

    def refresh_releases(self, channels):
        for job in self.jobs():
            if job['status'] != 'SCHEDULED' or job['channel_id'] not in channels:
                continue
            try:
                auth = channels[job['channel_id']]['auth']
                response = requests.get('https://www.googleapis.com/youtube/v3/videos', params={'part':'status','id':job['youtube_video_id']}, headers={'Authorization':'Bearer '+auth.get_access_token()}, timeout=20)
                response.raise_for_status()
                items = response.json().get('items', [])
                if items and items[0]['status'].get('privacyStatus') == 'public':
                    self.set_job(job, 'COMPLETED', privacy_status='public')
                    self.db.update_video_status(job['video_id'], VideoStatus.PUBLISHED)
            except requests.RequestException:
                continue

    def recover_jobs(self):
        cutoff = utcnow() - dt.timedelta(minutes=30)
        with self.db.transaction() as cur:
            for row in cur.execute("SELECT * FROM publication_jobs WHERE status='UPLOADING'").fetchall():
                data = json.loads(row['data'])
                heartbeat = dt.datetime.fromisoformat(data['heartbeat']) if data.get('heartbeat') else cutoff
                if heartbeat <= cutoff:
                    status = 'THUMBNAIL_FAILED' if data.get('youtube_video_id') else 'QUEUED'
                    cur.execute('UPDATE publication_jobs SET status=? WHERE id=? AND status=?', (status, row['id'], 'UPLOADING'))

    @atomic
    def claim(self, job):
        state = self.review(job['video_id'])
        if not state['approved'] or state['revision'] != job['revision']:
            return False
        with self.db.transaction() as cur:
            row = cur.execute('SELECT data FROM publication_jobs WHERE id=?', (job['id'],)).fetchone()
            data = json.loads(row['data'])
            data['heartbeat'] = utcnow().isoformat()
            cur.execute("UPDATE publication_jobs SET status='UPLOADING',data=? WHERE id=? AND status='QUEUED'", (json.dumps(data), job['id']))
            return cur.rowcount == 1

    def set_job(self, job, status, **updates):
        row = self.db._get_connection().execute('SELECT data FROM publication_jobs WHERE id=?', (job['id'],)).fetchone()
        data = json.loads(row['data'])
        data.update({k:v for k,v in job.items() if k not in ('id','video_id','channel_id','revision','status')})
        data.update(updates)
        data['heartbeat'] = utcnow().isoformat()
        with self.db.transaction() as cur:
            cur.execute('UPDATE publication_jobs SET status=?,data=? WHERE id=?', (status, json.dumps(data), job['id']))

    def thumbnail(self, job, auth):
        path = self.frame(job['video_id'], job.get('thumbnail_seconds', 1))
        with path.open('rb') as image:
            response = requests.post('https://www.googleapis.com/upload/youtube/v3/thumbnails/set', params={'videoId':job['youtube_video_id']}, headers={'Authorization':'Bearer '+auth.get_access_token(), 'Content-Type':'image/jpeg'}, data=image, timeout=30)
        response.raise_for_status()

    def frame(self, video_id, seconds):
        import subprocess
        video = self.db.get_video(video_id)
        if not video or not video.file_path or not Path(video.file_path).is_file():
            raise ValueError('Local clip unavailable')
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0 or seconds > (video.duration_seconds or 0):
            raise ValueError('Frame position is outside the clip')
        directory = self.db.db_path.parent / 'cache' / 'publication-frames'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (video_id + '-' + str(int(seconds * 1000)) + '.jpg')
        if not path.exists():
            subprocess.run(['ffmpeg','-v','error','-ss',str(seconds),'-i',video.file_path,'-frames:v','1','-vf','scale=1280:-2','-y',str(path)], check=True, timeout=30, capture_output=True)
        return path


class JobDatabase:
    """Adapt the resumable uploader to one channel-specific job."""
    def __init__(self, workflow, job):
        self.workflow, self.job = workflow, job

    def get_todays_upload_count(self):
        cutoff = (utcnow() - dt.timedelta(days=1)).isoformat()
        count = sum(1 for j in self.workflow.jobs() if j.get('uploaded_at', '') >= cutoff)
        return count + self.workflow.db.get_todays_upload_count()

    def get_youtube_upload_by_video_id(self, video_id):
        return YouTubeUpload(id=self.job['id'], video_id=video_id, **{k:v for k,v in self.job.items() if k in YouTubeUpload.model_fields and k not in ('id','video_id')})

    def save_youtube_upload(self, upload):
        self.job.update(upload.model_dump(mode='json', exclude={'id','video_id'}))
        self.workflow.set_job(self.job, upload.upload_status)
        return upload
