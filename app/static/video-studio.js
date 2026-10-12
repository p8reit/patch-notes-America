(function(root) {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  if (typeof module === 'object') module.exports = {escape};
  if (!root.document) return;
  const doc = root.document;
  const el = id => doc.getElementById(id);
  let project = null, library = [], dirty = false, jobsTimer = null, lastExport = null, stockSearch = null, stockScene = null;
  async function api(path, options = {}) {
    const response = await fetch('/api/video' + path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Video request failed');
    return data;
  }
  const json = (method, body) => ({method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
  function status(message, failed = false) {
    el('video-status').textContent = message;
    el('video-status').classList.toggle('bad', failed);
    el('video-status').setAttribute('role', failed ? 'alert' : 'status');
  }
  async function operation(button, work) {
    if (button.disabled) return;
    const buttons = [...el('video-panel').querySelectorAll('button')];
    buttons.forEach(b => b.disabled = true);
    status('Working…');
    try { await work(); status('Ready.'); }
    catch(error) { status(error.message, true); }
    finally { buttons.forEach(b => b.disabled = false); }
  }
  function collect() {
    if (!project) throw new Error('Create or load a storyboard first.');
    project.style = el('video-style').value;
    el('video-scenes').querySelectorAll('[data-scene]').forEach(row => {
      const scene = project.scenes.find(s => s.id === row.dataset.scene);
      row.querySelectorAll('[data-field]').forEach(input => {
        scene[input.dataset.field] = input.type === 'checkbox' ? input.checked
          : input.type === 'number' ? Number(input.value) : input.value || (input.dataset.field === 'asset_id' ? null : '');
      });
    });
  }
  function draw() {
    el('video-style').value = project.style;
    el('video-scenes').innerHTML = project.scenes.map((s,i) => `<article class="video-scene" data-scene="${escape(s.id)}">
      <h3>Scene ${i+1} · ${s.start.toFixed(1)}–${s.end.toFixed(1)}s</h3><p>${escape(s.text || 'Music / introduction')}</p>
      <div class="video-grid"><label>Scene start<input type="number" data-field="start" step="0.001" value="${s.start}"></label><label>Scene end<input type="number" data-field="end" step="0.001" value="${s.end}"></label></div>
      <label>Visual purpose<input data-field="intent" value="${escape(s.intent)}"></label>
      <label>Footage search<input data-field="query" maxlength="100" value="${escape(s.query)}"></label>
      <label>Local image prompt<textarea data-field="prompt" maxlength="2000">${escape(s.prompt)}</textarea></label>
      <label>Caption<textarea data-field="caption">${escape(s.caption)}</textarea></label>
      <label>Chosen media<select data-field="asset_id"><option value="">Choose footage or image</option>${library.map(a => `<option value="${escape(a.id)}" ${a.id===s.asset_id?'selected':''}>${escape(a.kind+' · '+(a.source.creator || a.source.model || a.id.slice(0,8)))}</option>`).join('')}</select></label>
      <div class="video-grid"><label>Footage trim start<input type="number" data-field="trim" min="0" step="0.1" value="${s.trim}"></label>
      <label>Horizontal focal point (0–1)<input type="number" data-field="crop_x" min="0" max="1" step="0.05" value="${s.crop_x}"></label><label>Vertical focal point (0–1)<input type="number" data-field="crop_y" min="0" max="1" step="0.05" value="${s.crop_y}"></label>
      <label>Image motion<select data-field="motion"><option value="still">Still</option><option value="zoom" ${s.motion==='zoom'?'selected':''}>Slow zoom</option></select></label></div>
      <label><input type="checkbox" data-field="locked" ${s.locked?'checked':''}> Lock approved scene</label>
      <button type="button" data-image="${escape(s.id)}">Generate local image</button>
      <button type="button" data-search="${escape(s.id)}" class="secondary">Find free B-roll</button>
      ${s.asset_id ? `<details><summary>View selected media and source</summary>${(() => {const a=library.find(a=>a.id===s.asset_id);return a ? (a.kind==='broll'?`<video controls preload="metadata" src="${escape(a.url)}"></video>`:`<img alt="Scene illustration" src="${escape(a.url)}">`)+`<p>${escape(a.source.creator || a.source.model)} · ${escape(a.source.license)}</p>`:'';})()}</details>` : '<p class="hint">Media unresolved</p>'}
      </article>`).join('');
    dirty = false;
  }
  async function lists() {
    const [episodes, projects, assets] = await Promise.all([api('/episodes'), api('/projects'), api('/assets')]);
    library = assets.assets;
    el('video-episode').innerHTML = episodes.episodes.map(e=>`<option value="${escape(e.id)}">${escape(e.title)}</option>`).join('');
    const selected = el('video-project').value || project?.id;
    el('video-project').innerHTML = projects.projects.map(p=>`<option value="${escape(p.id)}">${escape(p.title)} · ${p.id.slice(0,8)}</option>`).join('');
    if (selected) el('video-project').value = selected;
  }
  async function refreshJobs() {
    const resources=await api('/resources');
    el('video-resources').textContent=`GPU stage: ${resources.active} · Model handoffs ${resources.handoff_enabled?'enabled':'disabled'}${resources.free_vram_mb!==null?' · Last observed free VRAM: '+resources.free_vram_mb+' MB':''}`;
    const data = await api('/jobs');
    el('video-jobs').innerHTML = data.jobs.map(j=>`<article class="video-scene"><strong>${escape(j.project_id.slice(0,8))} · ${escape(j.status)}</strong><p>${escape(j.phase)} · ${j.done}/${j.total} scenes prepared</p>${j.error?`<p class="bad">${escape(j.error)}</p>`:''}
      ${['queued','running'].includes(j.status)?`<button type="button" data-cancel-video="${escape(j.id)}">Cancel video</button>`:''}
      ${j.status==='complete'&&j.download_url?`<a class="download" href="${escape(j.download_url)}">Download MP4</a> <a href="/api/video/exports/${escape(j.id)}/sidecar/captions.srt">Captions</a> · <a href="/api/video/exports/${escape(j.id)}/sidecar/credits.txt">Credits</a> · <a href="/api/video/exports/${escape(j.id)}/sidecar/chapters.ffmeta">Chapters</a>`:''}</article>`).join('') || '<p class="hint">No video jobs yet.</p>';
    const completed = data.jobs.find(j=>j.id===lastExport && j.status==='complete');
    if(completed && el('video-preview-player').getAttribute('src')!==completed.download_url){
      el('video-preview-player').src=completed.download_url;el('video-preview-player').hidden=false;
    }
    if(data.jobs.some(j=>['queued','running'].includes(j.status)) && !jobsTimer){
      jobsTimer=root.setTimeout(()=>{jobsTimer=null;refreshJobs().catch(e=>status(e.message,true));},2000);
    }
  }
  el('video-jobs').addEventListener('click',e=>{const b=e.target.closest('[data-cancel-video]');if(!b)return;
    operation(b,async()=>{await api('/jobs/'+b.dataset.cancelVideo+'/cancel',{method:'POST'});await refreshJobs();});
  });
  async function save() {
    collect(); project = await api('/projects/'+project.id, json('PUT', project)); dirty = false;
  }
  const replaceAllowed = () => !dirty || root.confirm('Replace your unsaved video storyboard edits?');
  el('video-create').addEventListener('click', e=>operation(e.currentTarget, async()=>{
    if (!replaceAllowed()) return;
    project=await api('/projects',json('POST',{episode_id:el('video-episode').value}));
    el('video-start').value=0;el('video-end').value=project.duration;await lists();draw();
  }));
  el('video-load').addEventListener('click', e=>operation(e.currentTarget, async()=>{
    if (!replaceAllowed()) return;
    project=await api('/projects/'+el('video-project').value);el('video-start').value=0;el('video-end').value=project.duration;await lists();draw();
  }));
  el('video-save').addEventListener('click', e=>operation(e.currentTarget, async()=>{await save();draw();}));
  el('video-refresh').addEventListener('click', e=>operation(e.currentTarget, async()=>{if(project)collect();const wasDirty=dirty;await lists();if(project)draw();dirty=wasDirty;}));
  el('video-import').addEventListener('submit', e=>{e.preventDefault();operation(e.target.querySelector('button'),async()=>{
    if(project)collect();const wasDirty=dirty;await api('/assets',{method:'POST',body:new FormData(e.target)});await lists();if(project)draw();dirty=wasDirty;
  });});
  el('video-scenes').addEventListener('click', e=>{const button=e.target.closest('[data-image]');if(!button)return;
    operation(button,async()=>{await save();project=await api('/projects/'+project.id+'/images',json('POST',{scene_id:button.dataset.image}));await lists();draw();});
  });
  el('video-plan').addEventListener('click',e=>operation(e.currentTarget,async()=>{await save();project=await api('/projects/'+project.id+'/plan',{method:'POST'});draw();}));
  el('video-batch').addEventListener('click',e=>operation(e.currentTarget,async()=>{await save();await api('/projects/'+project.id+'/batch-images',{method:'POST'});await refreshJobs();}));
  el('video-scenes').addEventListener('click',e=>{const b=e.target.closest('[data-search]');if(!b)return;operation(b,async()=>{
    await save();stockScene=b.dataset.search;const s=project.scenes.find(s=>s.id===stockScene);
    stockSearch=await api('/stock/search',json('POST',{provider:el('video-provider').value,query:s.query}));
    el('video-stock-results').innerHTML=`<h3>${escape(stockSearch.provider)} footage candidates</h3><p>Review source pages before choosing footage.</p>`+stockSearch.candidates.map(c=>`<article class="video-scene"><p>${escape(c.creator)} · ${c.duration}s · ${c.width}×${c.height}</p><a target="_blank" rel="noopener" href="${/^https:\/\//.test(c.source_url)?escape(c.source_url):'#'}">Source and creator on ${escape(c.provider)}</a> <button type="button" data-stock="${escape(c.id)}">Download and use this footage</button></article>`).join('');
    if(!stockSearch.candidates.length)el('video-stock-results').innerHTML+='<p>No matching footage. Try a broader query or import a file.</p>';
  });});
  el('video-stock-results').addEventListener('click',e=>{const b=e.target.closest('[data-stock]');if(!b)return;operation(b,async()=>{
    const item=await api('/stock/import',json('POST',{search_id:stockSearch.id,candidate_id:b.dataset.stock}));
    collect();const scene=project.scenes.find(s=>s.id===stockScene);if(!scene)throw new Error('Project changed; select this footage from the library.');
    scene.asset_id=item.id;project=await api('/projects/'+project.id,json('PUT',project));await lists();draw();
  });});
  for (const id of ['video-preview','video-export']) el(id).addEventListener('click',e=>operation(e.currentTarget,async()=>{
    await save();const result=await api('/projects/'+project.id+'/exports',json('POST',{start:Number(el('video-start').value),end:Number(el('video-end').value),preview:id==='video-preview'}));
    lastExport=result.id;await refreshJobs();
  }));
  el('video-panel').addEventListener('input',()=>{dirty=true;});
  root.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  el('video-tab').addEventListener('click',()=>{Promise.all([lists(),refreshJobs()]).catch(e=>status(e.message,true));});
})(typeof window === 'undefined' ? globalThis : window);
