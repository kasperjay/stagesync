"""HTTP endpoints for identity approval and publication drafts."""
import asyncio
import datetime as dt

from aiohttp import web

from stagesync.database.models import Concert, Identification
from stagesync.workflow import Workflow


def register(app, coordinator_key):
    def service(request):
        return Workflow(request.app[coordinator_key])

    async def state(request):
        workflow = service(request)
        video_id = request.match_info['id']
        review = workflow.review(video_id)
        try:
            draft = workflow.draft(video_id)
        except ValueError:
            draft = None
        return web.json_response(dict(review=review, draft=draft, jobs=workflow.jobs(video_id)))

    async def channels(request):
        values, errors = await asyncio.to_thread(service(request).channels)
        return web.json_response(dict(channels=[{k:v for k,v in c.items() if k != 'auth'} for c in values.values()], errors=errors))

    async def action(request):
        workflow = service(request)
        video_id, action = request.match_info['id'], request.match_info['action']
        body = await request.json() if request.can_read_body else {}
        try:
            if action == 'confirm':
                result = workflow.confirm(video_id, body.get('field'), body.get('revision'), body.get('resolution','identified'), body.get('original_artist',''))
            elif action == 'approve':
                result = workflow.approve(video_id).model_dump(mode='json')
            elif action == 'flag':
                result = workflow.flag(video_id, body.get('flagged',True))
            elif action == 'reject-match':
                result = workflow.reject_match(video_id)
            elif action == 'draft':
                result = workflow.draft(video_id, body.get('edits'), body.get('regenerate',False))
            elif action == 'enqueue':
                result = await asyncio.to_thread(workflow.enqueue, video_id)
            elif action == 'retry-thumbnail':
                values, _ = await asyncio.to_thread(workflow.channels)
                for job in workflow.jobs(video_id):
                    if job['status'] == 'THUMBNAIL_FAILED':
                        if job['channel_id'] not in values:
                            raise ValueError('Channel unavailable')
                        await asyncio.to_thread(workflow.thumbnail, job, values[job['channel_id']]['auth'])
                        workflow.set_job(job, 'SCHEDULED' if job.get('publish_at') else 'COMPLETED', error_message=None)
                result = workflow.jobs(video_id)
            elif action == 'manual-show':
                venue = workflow.db.get_venue(body.get('venue_id',''))
                if not venue:
                    raise ValueError('Select a known venue first')
                date = dt.date.fromisoformat(body.get('date','')).isoformat()
                name = (body.get('name') or '').strip()
                if not name:
                    raise ValueError('Enter the show name')
                concert = Concert(venue_id=venue.id, headliner_artist='Various Artists', tour_name=name, event_date=date)
                workflow.db.save_concert(concert)
                ident = workflow.db.get_identification_by_video_id(video_id) or Identification(video_id=video_id, detected_song_title='Live Performance', detected_artist='Unknown Artist', match_confidence=0)
                ident.concert_id = concert.id
                ident.detected_artist = 'Unknown Artist'
                ident.detected_song_title = 'Live Performance'
                ident.match_confidence = 0
                ident.status = 'USER_ASSISTED_BAND'
                workflow.db.save_identification(ident)
                result = workflow.review(video_id)
            else:
                raise ValueError('Unknown workflow action')
            return web.json_response(dict(success=True, result=result))
        except (ValueError, TypeError) as exc:
            return web.json_response(dict(success=False,error=str(exc)), status=400)

    async def media(request):
        workflow = service(request)
        video = workflow.db.get_video(request.match_info['id'])
        if not video or not video.file_path:
            raise web.HTTPNotFound(text='Clip unavailable')
        return web.FileResponse(video.file_path)

    async def frame(request):
        try:
            path = await asyncio.to_thread(service(request).frame, request.match_info['id'], request.query.get('seconds',1))
            return web.FileResponse(path)
        except (ValueError, TypeError) as exc:
            return web.json_response({'error':str(exc)}, status=400)

    async def bulk(request):
        workflow = service(request)
        body = await request.json()
        ids = body.get('video_ids',[])
        if not isinstance(ids,list) or not ids or len(ids)>200:
            return web.json_response({'error':'Select 1–200 clips'}, status=400)
        results=[]
        for video_id in dict.fromkeys(ids):
            try:
                if body.get('action') == 'approve':
                    workflow.approve(video_id)
                elif body.get('action') == 'draft':
                    workflow.draft(video_id, body.get('edits',{}))
                else:
                    raise ValueError('Unknown bulk action')
                results.append(dict(video_id=video_id, success=True))
            except ValueError as exc:
                results.append(dict(video_id=video_id, success=False,error=str(exc)))
        return web.json_response({'results':results})

    async def defaults(request):
        workflow = service(request)
        data = await request.json() if request.method == 'POST' else None
        return web.json_response(workflow.defaults(data))

    async def overview(request):
        workflow = service(request)
        conn = workflow.db._get_connection()
        counts = {r['processing_status']:r['count'] for r in conn.execute('SELECT processing_status,COUNT(*) count FROM videos GROUP BY processing_status')}
        jobs = workflow.jobs()
        shows = {}
        unassigned = []
        videos = workflow.db.list_videos(limit=1000000)
        approved = 0
        verified = 0
        flagged = []
        for video in videos:
            state = workflow.review(video.id)
            approved += int(state['approved'])
            verified += int('song' in state['confirmations'])
            if state['flagged']:
                flagged.append(video.id)
            ident = workflow.db.get_identification_by_video_id(video.id)
            concert = workflow.db.get_concert(ident.concert_id) if ident and ident.concert_id else None
            if concert:
                venue = workflow.db.get_venue(concert.venue_id) if concert.venue_id else None
                show = shows.setdefault(concert.id, dict(id=concert.id, name=concert.tour_name or concert.headliner_artist, date=concert.event_date, venue=venue.name if venue else None, clips=[], approved=0, published=0))
                show['clips'].append(dict(id=video.id,filename=video.filename,status=video.processing_status.value))
                show['approved'] += int(state['approved'])
                show['published'] += int(video.processing_status.value == 'PUBLISHED')
            else:
                unassigned.append(dict(id=video.id,filename=video.filename,status=video.processing_status.value))
        jobs = workflow.jobs()
        history = [dict(r) for r in conn.execute("SELECT video_id,youtube_url,upload_status,uploaded_at,title FROM youtube_uploads WHERE youtube_video_id IS NOT NULL")]
        counts = {r['processing_status']:r['count'] for r in conn.execute('SELECT processing_status,COUNT(*) count FROM videos GROUP BY processing_status')}
        events = [dict(r) for r in conn.execute('SELECT video_id,step_name,status,error_log,started_at FROM pipeline_runs ORDER BY started_at DESC LIMIT 100')]
        return web.json_response(dict(unassigned=unassigned, events=events, counts=counts, shows=list(shows.values()), jobs=jobs, legacy_history=history, flagged=flagged,
                                     detected_shows=len(shows), verified_matches=verified, approved_clips=approved,
                                     queued_jobs=sum(j['status']=='QUEUED' for j in jobs), published_uploads=sum(1 for j in jobs if j.get('youtube_video_id') and j.get('privacy_status')=='public')))

    app.router.add_get('/api/workflow/channels', channels)
    app.router.add_get('/api/workflow/overview', overview)
    app.router.add_get('/api/workflow/defaults', defaults)
    app.router.add_post('/api/workflow/defaults', defaults)
    app.router.add_post('/api/workflow/bulk', bulk)
    app.router.add_get('/api/workflow/{id}', state)
    app.router.add_post('/api/workflow/{id}/{action}', action)
    app.router.add_get('/api/video/{id}/playback', media)
    app.router.add_get('/api/video/{id}/frame', frame)
