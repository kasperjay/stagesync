(() => {
  let active = null, generation = 0;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = async (path, body) => {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers:{'Content-Type':'application/json',Accept:'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok || data.error) throw Error(data.error || `Request failed (${response.status})`);
    return data;
  };
  const dialog = document.createElement('dialog');
  dialog.id = 'workflow-dialog';
  dialog.style.cssText = 'width:min(1100px,96vw);max-height:94vh;overflow:auto;background:#11111b;color:#eee;border:1px solid #444;border-radius:16px;padding:24px';
  document.body.append(dialog);
  const style = document.createElement('style');
  style.textContent = '#workflow-dialog::backdrop{background:#000b} #workflow-dialog button{padding:8px 12px;border:1px solid #555;border-radius:8px;margin:4px} #workflow-dialog input,#workflow-dialog textarea,#workflow-dialog select{background:#202030;color:#fff;padding:8px;border:1px solid #555;border-radius:6px;width:100%} #workflow-dialog label{display:block;margin:10px 0} #workflow-dialog section{padding:12px;border:1px solid #333;border-radius:10px;margin:10px 0} #workflow-dialog button:disabled{opacity:.4} #workflow-dialog .columns{display:grid;grid-template-columns:1fr 1fr;gap:20px} @media(max-width:700px){#workflow-dialog .columns{grid-template-columns:1fr}}';
  document.head.append(style);

  const run = async fn => {try {await fn();} catch(error){const output=dialog.querySelector('[role=status]');if(output)output.textContent=error.message;else alert(error.message);}};
  const button = (id, fn) => {const el=dialog.querySelector('#'+id);if(el)el.onclick=()=>run(fn);};
  const chicagoInput = value => value ? new Intl.DateTimeFormat('sv-SE',{timeZone:'America/Chicago',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value)).replace(' ','T') : '';
  const field = id => dialog.querySelector('#'+id)?.value;
  window.openWorkflow = async (id, publishing=false) => run(async () => {
    active=id;
    const act = async (name, body={}) => {if(active!==id)throw Error("The selected clip changed. Reopen this review.");return api(`/api/workflow/${encodeURIComponent(id)}/${name}`,body);};
    const ticket=++generation;
    const data=await api(`/api/workflow/${encodeURIComponent(id)}`);
    if(ticket!==generation)return;
    const r=data.review;
    dialog.innerHTML=`<header style="display:flex;justify-content:space-between"><h2>${publishing?'Publishing':'Approve clip identity'}</h2><button id="wf-close">Close</button></header><p role="status" aria-live="polite"></p><div class="columns"><div><video controls preload="metadata" style="width:100%" src="/api/video/${encodeURIComponent(id)}/playback"></video><p>Source: ${esc(r.source||'No recognition evidence')} ${r.confidence===null?'· Manual confirmation':r.confidence!==null?'· Confidence '+esc(Math.round(r.confidence*100))+'%':''}</p><details><summary>Evidence</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify(r.evidence,null,2))}</pre></details><button id="wf-flag">${r.flagged?'Unflag':'Flag for Later'}</button><button id="wf-reject">Reject Match</button></div><div id="wf-controls"></div></div>`;
    button('wf-close',()=>{generation++;active=null;dialog.close();});
    button('wf-flag',async()=>{await act('flag',{flagged:!r.flagged});await openWorkflow(id,publishing);});
    button('wf-reject',async()=>{await act('reject-match');await openWorkflow(id);});
    if(!dialog.open)dialog.showModal();
    const controls=dialog.querySelector('#wf-controls');
    if(!publishing){
      controls.innerHTML=['venue','show','artist','song'].map((key,index)=>`<section><h3>${index+1}. ${key[0].toUpperCase()+key.slice(1)} ${r.confirmations[key]?'✓':''}</h3><p>${esc(typeof r.values[key]==='object'?r.values[key]?.name+(r.values[key]?.date?' · '+r.values[key].date:''):r.values[key])}</p><button id="wf-edit-${key}">Choose / edit</button><button id="wf-confirm-${key}" ${index && !r.confirmations[['venue','show','artist','song'][index-1]]?'disabled':''}>Confirm ${key}</button>${key==='show'?'<button id="wf-manual-show">Enter show manually</button>':''}${key==='song'?'<label>Resolution<select id="wf-resolution"><option value="identified">Identified song</option><option value="manual">Manual song entry</option><option value="cover">Cover</option><option value="improvisation">Improvisation</option><option value="unknown">Unknown track</option></select></label><label>Original artist (covers)<input id="wf-original"></label><button id="wf-match">Run song matching</button><button id="wf-discography">Search artist discography</button>':''}</section>`).join('')+`<button id="wf-approve">Approve & Next</button><button id="wf-publish" ${!r.approved?'disabled':''}>Open publishing editor</button>`;
      dialog.querySelector('#wf-resolution').value=r.resolution;
      dialog.querySelector('#wf-original').value=r.original_artist;
      const editors={venue:openVenueAssistModal,show:openSetlistAssistModal,artist:openBandAssistModal,song:openSongAssistModal};
      for(const key of Object.keys(editors)){
        button('wf-edit-'+key,()=>{dialog.close();editors[key](id);});
        button('wf-confirm-'+key,async()=>{await act('confirm',{field:key,revision:r.revision,resolution:field('wf-resolution'),original_artist:field('wf-original')});await openWorkflow(id);});
      }
      button('wf-manual-show',async()=>{
        const name=prompt('Show / festival name');if(!name)return;
        const date=prompt('Performance date (YYYY-MM-DD)',r.values.show?.date||'');if(!date)return;
        await act('manual-show',{name,date,venue_id:r.values.venue?.id});await openWorkflow(id);
      });
      button('wf-discography',async()=>{const query=prompt('Song title to search in '+r.values.artist+' discography');if(!query)return;const result=await api(`/api/video/${id}/song-options?source=discography&q=${encodeURIComponent(query)}`);if(ticket!==generation)return;const songs=result.songs||[];if(!songs.length)throw Error(result.message||'No discography matches. Enter a song manually.');const choice=prompt(songs.slice(0,20).map((song,i)=>(i+1)+'. '+song.title).join('\n')+'\nEnter a song number');if(!choice)return;const song=songs[Number(choice)-1];if(!song)throw Error('Choose a listed song number');await api(`/api/video/${id}/set-song`,{song_title:song.title,artist_name:r.values.artist});await openWorkflow(id);});
      button('wf-match',async()=>{await api(`/api/video/${id}/match`,{});await openWorkflow(id);});
      button('wf-approve',async()=>{
        await act('approve');await loadData();if(ticket!==generation)return;
        const response=await api('/api/videos?status=PENDING_REVIEW&limit=1000000');
        const list=Array.isArray(response)?response:response.videos||[];
        const overview=await api('/api/workflow/overview');const next=list.find(v=>v.id!==id&&!overview.flagged.includes(v.id));
        if(next)await openWorkflow(next.id);else await openWorkflow(id,true);
      });
      button('wf-publish',()=>openWorkflow(id,true));
    }else{
      if(!data.draft){controls.innerHTML='<p>Confirm and approve identity before publishing.</p><button id="wf-review">Review identity</button>';button('wf-review',()=>openWorkflow(id));return;}
      const d=data.draft;
      const channels=await api('/api/workflow/channels');if(ticket!==generation)return;
      controls.innerHTML=`${d.needs_review?'<p>Identity changed. Review this draft before queueing.</p>':''}<label>Title<input id="wf-title" maxlength="100" value="${esc(d.title)}"></label><label>Description<textarea id="wf-description" rows="5">${esc(d.description)}</textarea></label><label>Tags (comma separated)<input id="wf-tags" value="${esc(d.tags.join(', '))}"></label><label>Visibility<select id="wf-privacy"><option>private</option><option>unlisted</option><option>public</option></select></label><label>Release time · America/Chicago<input id="wf-time" type="datetime-local" value="${esc(chicagoInput(d.publish_at))}"></label><label>Repeated fall-back hour<select id="wf-fold"><option value="0">First occurrence (daylight time)</option><option value="1">Second occurrence (standard time)</option></select></label><label>Thumbnail position (seconds)<input id="wf-frame" type="number" min="0" step="0.1" value="${esc(d.thumbnail_seconds)}"></label><button id="wf-preview">Preview frame</button><img id="wf-image" style="max-width:100%"><fieldset><legend>YouTube destinations</legend>${channels.channels.map(c=>`<label><input style="width:auto" type="checkbox" name="wf-channel" value="${esc(c.id)}" ${d.channel_ids.includes(c.id)?'checked':''}> ${esc(c.title)}</label>`).join('')||'<p>No authenticated channels available.</p>'}${channels.errors.map(e=>`<p>${esc(e)}</p>`).join('')}</fieldset><button id="wf-save">Save draft</button><button id="wf-defaults">Save as defaults</button><button id="wf-regenerate">Regenerate from identity</button><button id="wf-enqueue">Upload Now / Schedule Release</button><button id="wf-thumbnail-retry">Retry failed thumbnail</button><h3>Destinations and history</h3>${data.jobs.map(j=>`<section>${esc(j.channel_id)} · ${esc(j.status)}<p>${esc(j.bytes_uploaded||0)} / ${esc(j.total_bytes||0)} bytes</p><p>${esc(j.error_message||'')}</p>${j.youtube_url?`<a target="_blank" rel="noopener" href="${esc(j.youtube_url)}">View uploaded video</a>`:''}</section>`).join('')}`;
      dialog.querySelector('#wf-privacy').value=d.privacy_status;
      if(d.publish_at){const before=new Date(new Date(d.publish_at).getTime()-3600000);if(chicagoInput(before.toISOString())===chicagoInput(d.publish_at))dialog.querySelector('#wf-fold').value='1';}
      const edits=()=>({title:field('wf-title'),description:field('wf-description'),tags:field('wf-tags').split(',').map(t=>t.trim()).filter(Boolean),privacy_status:field('wf-privacy'),publish_local:field('wf-time')||null,fold:Number(field('wf-fold')),thumbnail_seconds:Number(field('wf-frame')),channel_ids:[...dialog.querySelectorAll('[name=wf-channel]:checked')].map(c=>c.value)});
      button('wf-save',async()=>{await act('draft',{edits:edits()});await openWorkflow(id,true);});
      button('wf-defaults',async()=>{await api('/api/workflow/defaults',edits());dialog.querySelector('[role=status]').textContent='Defaults saved for future drafts.';});
      button('wf-regenerate',async()=>{if(confirm('Replace this draft text with newly generated metadata?')){await act('draft',{regenerate:true});await openWorkflow(id,true);}});
      button('wf-preview',()=>{dialog.querySelector('#wf-image').src=`/api/video/${id}/frame?seconds=${encodeURIComponent(field('wf-frame'))}`;});
      button('wf-thumbnail-retry',async()=>{await act('retry-thumbnail');await openWorkflow(id,true);});
      button('wf-enqueue',async()=>{
        await act('draft',{edits:edits()});if(ticket!==generation)return;await act('enqueue');
        dialog.querySelector('[role=status]').textContent='Explicitly queued. Uploading selected destinations…';
        await api('/api/upload',{});await loadData();await openWorkflow(id,true);
      });
    }
  });
  window.openPublicationQueue=async()=>run(async()=>{
    const response=await api('/api/videos?limit=1000000');
    const clips=(Array.isArray(response)?response:response.videos||[]).filter(v=>['APPROVED','UPLOADED','SCHEDULED','PUBLISHED','FAILED'].includes(v.processing_status));
    dialog.innerHTML='<h2>Publishing drafts</h2><p>Approval never starts an upload. Open a clip to edit and choose destinations.</p><p role="status"></p>'+clips.map(v=>`<section><input type="checkbox" name="wf-bulk" value="${esc(v.id)}" style="width:auto"> <button data-clip="${esc(v.id)}">${esc(v.filename)}</button></section>`).join('')+'<button id="wf-bulk-description">Bulk edit selected drafts</button><button id="wf-queue-close">Close</button>';
    if(!clips.length)dialog.innerHTML+='<p>No approved clips.</p>';
    dialog.querySelectorAll('[data-clip]').forEach(el=>el.onclick=()=>openWorkflow(el.dataset.clip,true));
    button('wf-queue-close',()=>dialog.close());
    button('wf-bulk-description',async()=>{const ids=[...dialog.querySelectorAll('[name=wf-bulk]:checked')].map(el=>el.value);if(!ids.length)throw Error('Select clips first');const key=prompt('Field to change: title, description, tags, privacy_status','description');if(!key)return;if(!['title','description','tags','privacy_status'].includes(key))throw Error('Choose title, description, tags or privacy_status');let value=prompt('New '+key+' for selected clips');if(value===null)return;if(key==='tags')value=value.split(',').map(t=>t.trim()).filter(Boolean);const names=clips.filter(v=>ids.includes(v.id)).map(v=>v.filename).join('\n');if(!confirm('Apply '+key+' to these clips?\n'+names))return;const result=await api('/api/workflow/bulk',{action:'draft',video_ids:ids,edits:{[key]:value}});dialog.querySelector('[role=status]').textContent=result.results.map(r=>r.success?'Saved':r.error).join('; ');});
    if(!dialog.open)dialog.showModal();
  });
  window.openWorkflowLibrary=async (mode='library')=>run(async()=>{
    ++generation;active=null;
    const data=await api('/api/workflow/overview');
    dialog.innerHTML=`<h2>${mode==='archive'?'Upload Archive':mode==='review'?'Approval Queue':mode==='overview'?'Workflow Overview':'Library · Shows'}</h2><p role="status"></p><button id="wf-library-close">Close</button><label>Search<input id="wf-library-search" placeholder="Show, venue, date or filename"></label><div id="wf-library-content"></div>`;
    button('wf-library-close',()=>dialog.close());
    const content=dialog.querySelector('#wf-library-content');
    const render=()=>{
      const query=field('wf-library-search').toLowerCase();
      if(mode==='overview'){
        content.innerHTML=`<section>Detected shows: ${data.detected_shows} · Verified matches: ${data.verified_matches} · Approved clips: ${data.approved_clips} · Queued destination jobs: ${data.queued_jobs} · Public uploads (new workflow): ${data.published_uploads}</section><p>Counts reflect saved confirmations and destination jobs. Historical uploads are listed separately in Archive.</p><h3>Recent processing events (latest 100)</h3>${data.events.map(e=>`<p>${esc(e.started_at)} · ${esc(e.step_name)} · ${esc(e.status)} ${esc(e.error_log||'')}</p>`).join('')||'<p>No recorded processing events.</p>'}`;
      }else if(mode==='archive'){
        content.innerHTML=data.jobs.filter(j=>JSON.stringify(j).toLowerCase().includes(query)).map(j=>`<section><button data-clip="${esc(j.video_id)}">${esc(j.title)}</button> · ${esc(j.channel_id)} · ${esc(j.status)}<p>${esc(j.bytes_uploaded||0)} / ${esc(j.total_bytes||0)} bytes · ${esc(j.error_message||'')}</p>${j.publish_at?`<p>Release: ${esc(new Date(j.publish_at).toLocaleString('en-US',{timeZone:'America/Chicago'}))} America/Chicago</p>`:''}${j.youtube_url?`<a href="${esc(j.youtube_url)}" target="_blank" rel="noopener">View video</a>`:''}</section>`).join('') + '<h3>Existing upload history</h3>'+data.legacy_history.filter(j=>JSON.stringify(j).toLowerCase().includes(query)).map(j=>`<section>${esc(j.title)} · ${esc(j.upload_status)} <a href="${esc(j.youtube_url)}" target="_blank" rel="noopener">View video</a></section>`).join('');
      }else{
        const shows=data.shows.map(show=>({...show,clips:show.clips.filter(c=>mode!=='review'||c.status==='PENDING_REVIEW')})).filter(show=>(mode!=='review'||show.clips.length>0)&&JSON.stringify(show).toLowerCase().includes(query));
        content.innerHTML=shows.map(show=>`<section><h3>${esc(show.name)} · ${esc(show.date)}</h3><p>${esc(show.venue||'Venue unconfirmed')} · ${mode==='review'?show.clips.length+' awaiting approval':show.clips.length+' clips · '+show.approved+' approved · '+show.published+' public'}</p>${show.clips.filter(c=>mode!=='review'||c.status==='PENDING_REVIEW').map(c=>`<div><input style="width:auto" type="checkbox" name="wf-approve-selection" value="${esc(c.id)}"><img width="64" style="display:inline" src="/api/video/${encodeURIComponent(c.id)}/thumbnail" alt=""><button data-review="${esc(c.id)}">${esc(c.filename)}</button> · ${esc(c.status)} ${data.flagged.includes(c.id)?'· Flagged for later':''}</div>`).join('')}</section>`).join('')||'<p>No matching shows. Clips without a show remain in the main review queue.</p>';
        const unassigned=(data.unassigned||[]).filter(c=>(mode!=='review'||c.status==='PENDING_REVIEW')&&c.filename.toLowerCase().includes(query));
        if(unassigned.length)content.innerHTML+='<section><h3>Show not yet selected</h3>'+unassigned.map(c=>`<div><button data-review="${esc(c.id)}">${esc(c.filename)}</button> · ${esc(c.status)}</div>`).join('')+'</section>';
        content.innerHTML+='<button id="wf-bulk-approve">Approve selected confirmed clips</button>';
        button('wf-bulk-approve',async()=>{const ids=[...dialog.querySelectorAll('[name=wf-approve-selection]:checked')].map(el=>el.value);if(!ids.length)throw Error('Select clips first');const response=await api('/api/workflow/bulk',{action:'approve',video_ids:ids});dialog.querySelector('[role=status]').textContent=response.results.map(r=>r.video_id+': '+(r.success?'Approved':r.error)).join(' · ');await loadData();});
      }
      content.querySelectorAll('[data-clip]').forEach(el=>el.onclick=()=>openWorkflow(el.dataset.clip,true));
      content.querySelectorAll('[data-review]').forEach(el=>el.onclick=()=>openWorkflow(el.dataset.review));
    };
    dialog.querySelector('#wf-library-search').oninput=render;render();if(!dialog.open)dialog.showModal();
  });
  const navigation=document.getElementById('nav-btn-catalog')?.parentElement;
  if(navigation){
    for(const [mode,label] of [['review','Ordered Approval'],['library','Library / Shows'],['archive','Archive'],['overview','Workflow Counts']]){
      const b=document.createElement('button');b.className='w-full text-left px-3 py-2 text-xs text-zinc-300 hover:text-white';b.textContent=label;b.onclick=()=>openWorkflowLibrary(mode);navigation.append(b);
    }
  }
  const originalApprove=window.approveVideo;
  window.approveVideo=async id=>{await openWorkflow(id);return false;};
  window.closeWorkflow=()=>{generation++;active=null;dialog.querySelector('video')?.pause();dialog.close();};
  const originalTrigger=window.triggerAction;
  window.triggerAction=async action=>action==='upload'?openPublicationQueue():originalTrigger(action);
  const originalDrawer=window.openDrawer;
  window.openDrawer=id=>{
    originalDrawer(id);
    const approve=document.getElementById('drawer-btn-approve');
    if(approve){approve.textContent='Review Venue → Show → Artist → Song';approve.onclick=()=>openWorkflow(id);}
    const footer=approve?.parentElement;
    document.getElementById('wf-editor-button')?.remove();
    if(footer){const b=document.createElement('button');b.id='wf-editor-button';b.textContent='Publishing editor';b.onclick=()=>openWorkflow(id,true);footer.append(b);}
  };
})();
