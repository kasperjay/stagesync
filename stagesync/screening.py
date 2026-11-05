"""Durable, fail-closed admission gate shared by every ingestion source.

Rejected candidates never need a catalog row. Legacy rows stay intact for history,
but library queries require a current accepted screening receipt.
"""
import datetime as dt
import json
import hashlib
from pathlib import Path
import uuid

from stagesync.database.models import Video
from stagesync.intelligence import concert_screen


class Screening:
    """Persist and retry automatic concert admission without deleting media."""
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.db = coordinator.db

    def accepted(self, video_id):
        return self.db.screening_accepted(video_id)

    def admit(self, video, retry=False):
        fingerprint = None
        if video.file_path and Path(video.file_path).is_file():
            try:
                with Path(video.file_path).open('rb') as media:
                    fingerprint = hashlib.file_digest(media, 'sha256').hexdigest()
            except OSError:
                pass
        if fingerprint:
            # Keep recovered source media reachable even when classification is held
            # or a cached receipt already accepts these exact bytes.
            with self.db.transaction() as cur:
                cur.execute("""UPDATE videos SET file_path=?,file_hash_sha256=?,file_size_bytes=?
                    WHERE id=?""", (video.file_path, fingerprint, Path(video.file_path).stat().st_size, video.id))
        receipt = self.db._get_connection().execute(
            'SELECT decision,media_sha256 FROM concert_screenings WHERE video_id=?', (video.id,)).fetchone()
        if receipt and receipt['decision'] == 'accept' and fingerprint and receipt['media_sha256'] == fingerprint:
            return True
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        token = str(uuid.uuid4())
        # A lease makes retries safe across CLI and web workers. Interrupted attempts
        # become retryable after 15 minutes, longer than bounded classifier work.
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)).isoformat()
        with self.db.transaction() as cur:
            cur.execute("""INSERT OR IGNORE INTO concert_screenings
                (video_id, source_id, filename, video_json, decision, reason, updated_at)
                VALUES (?,?,?,?, 'pending','Awaiting automatic concert screening',?)""",
                (video.id, video.source_photo_id, video.filename, video.model_dump_json(), now))
            changed = cur.execute("""UPDATE concert_screenings SET decision='screening',
                reason='Automatic concert screening in progress', lease=?, updated_at=?,
                video_json=?, attempts=attempts+1 WHERE video_id=?
                AND (decision!='screening' OR updated_at<?)""",
                (token, now, video.model_dump_json(), video.id, cutoff)).rowcount
        if not changed:
            return False
        try:
            if not video.file_path or not Path(video.file_path).is_file():
                result = {'decision':'review', 'reason':'Local media unavailable; restore the file or rescan the source'}
            else:
                result = concert_screen.screen_concert(Path(video.file_path))
                if not (isinstance(result, dict) and result.get('decision') == 'review'
                        and isinstance(result.get('reason'), str) and 'confidence' not in result):
                    result = concert_screen.validate_result(result)
        except Exception as exc:
            result = {'decision':'review', 'reason':f'Content analysis failed ({type(exc).__name__})'}
        if result['decision'] == 'accept':
            try:
                with Path(video.file_path).open('rb') as media:
                    unchanged = fingerprint == hashlib.file_digest(media, 'sha256').hexdigest()
            except OSError:
                unchanged = False
            if not unchanged:
                result = {'decision':'review', 'reason':'Media changed during screening; retry required'}
        with self.db.transaction() as cur:
            changed = cur.execute("""UPDATE concert_screenings SET decision=?,reason=?,
                result_json=?, updated_at=?,media_sha256=?,lease=NULL WHERE video_id=? AND lease=?""",
                (result['decision'], result['reason'], json.dumps(result),
                 dt.datetime.now(dt.timezone.utc).isoformat(), fingerprint, video.id, token)).rowcount
            if changed and result['decision'] == 'accept':
                # Audits never rewrite identity edits, status, or publishing history.
                if not self.db.get_video(video.id):
                    self.db.save_video(video)
                else:
                    cur.execute("""UPDATE videos SET file_path=?,file_hash_sha256=?,file_size_bytes=?
                        WHERE id=?""", (video.file_path, fingerprint, video.file_size_bytes, video.id))
        return bool(changed and result['decision'] == 'accept')

    def retry(self, video_id):
        row = self.db._get_connection().execute(
            'SELECT * FROM concert_screenings WHERE video_id=?', (video_id,)).fetchone()
        if not row:
            raise ValueError('Screening candidate not found')
        video = self.db.get_video(video_id) or Video.model_validate_json(row['video_json'])
        self.admit(video, retry=True)
        return next(r for r in self.list() if r['video_id'] == video_id)

    def candidate(self, source_id):
        row = self.db._get_connection().execute(
            'SELECT video_json FROM concert_screenings WHERE source_id=?', (source_id,)).fetchone()
        return Video.model_validate_json(row['video_json']) if row else None

    def list(self):
        return [dict(r) for r in self.db._get_connection().execute(
            'SELECT video_id,source_id,filename,decision,reason,attempts,updated_at FROM concert_screenings ORDER BY updated_at DESC')]

    def audit(self, limit=0, retry=False):
        rows = self.list()
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)).isoformat()
        candidates = [r for r in rows if r['decision'] == 'pending' or
                      (r['decision'] == 'screening' and r['updated_at'] < cutoff) or
                      (retry and r['decision'] != 'accept')]
        for row in candidates[:limit or None]:
            yield self.retry(row['video_id'])
