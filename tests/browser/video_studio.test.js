const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {escape,split,merge,scenePage,buildIssue}=require('../../app/static/video-studio.js');

test('splitting and merging preserves source trims, timing, and original state',()=>{
  const original=[{id:'s',start:0,end:8,trim:3,text:'Story',caption:'Story',locked:false,asset_id:'clip'}];
  const divided=split(original,'s','next');
  assert.deepEqual(divided.map(s=>[s.start,s.end,s.trim]),[[0,4,3],[4,8,7]]);
  assert.equal(original[0].end,8);
  assert.deepEqual(merge(divided,'s'),original);
  assert.throws(()=>split([{...original[0],locked:true}],'s','next'),/unlocked/);
  assert.throws(()=>merge([{...divided[0]}, {...divided[1],trim:1}],'s'),/continuous/);
  assert.equal(escape('<script>"'), '&lt;script&gt;&quot;');
});

function page(failure=false,options={}){
  const config=options;
  const nodes={};
  const node=id=>nodes[id] ||= {value:'',disabled:false,hidden:false,listeners:{},
    addEventListener(event,fn){(this.listeners[event] ||= []).push(fn);},
    setAttribute(name,value){this[name]=value;},getAttribute(name){return this[name];},
    classList:{toggle(){}},querySelectorAll(selector){return id==='video-panel'&&selector==='button'?Object.values(nodes).filter(n=>n.listeners.click):[];}};
  let project={id:'project',revision:1,title:'Episode',duration:60,style:'Style',scenes:[{id:'scene',start:0,end:60,text:'Narration',caption:'Narration',prompt:'Illustration',intent:'Context',query:'houses',asset_id:null,trim:0,crop_x:.5,crop_y:.5,motion:'still',locked:false}]};
  const candidate={id:'clip',provider:'pixabay',creator:'Author',duration:60,width:1920,height:1080,url:'https://cdn.pixabay.com/video/clip.mp4',source_url:'https://pixabay.com/videos/clip/'};
  if(options.ready){project.scenes[0].end=30;project.scenes.push({...project.scenes[0],id:'broll',start:30,end:60,media_type:'broll',stock_id:'selected',stock:candidate});}
  if(options.long){project.duration=160;project.scenes=Array.from({length:20},(_,i)=>({...project.scenes[0],id:'scene'+i,start:i*8,end:(i+1)*8,media_type:i%2?'broll':'image',stock_id:i%2?'selected':null}));}
  const calls=[];
  const timers=[];
  const root={document:{getElementById:node},confirm:()=>true,crypto:{randomUUID:()=> 'new'},addEventListener(){},setTimeout(fn){timers.push(fn);return timers.length;}};
  const context={window:root,fetch:async(url,options={})=>{
    calls.push({url,options});
    if(url.endsWith('/resources')&&config.resourceFailure)return {ok:false,json:async()=>({detail:'GPU unavailable'})};
    if(url.endsWith('/jobs')&&config.jobFetchFailure)throw new Error('Connection lost');
    if(failure&&options.method==='PUT')return {ok:false,json:async()=>({detail:'Project changed. Reload.'})};
    if(options.method==='PUT')project={...JSON.parse(options.body),revision:project.revision+1};
    if(url.endsWith('/scenes/scene/stock'))project={...project,revision:project.revision+1,scenes:project.scenes.map(s=>s.id==='scene'?{...s,media_type:'broll',stock_id:'selection',stock:candidate}:s)};
    let value=url.endsWith('/resources')?{active:'idle',handoff_enabled:false,free_vram_mb:null}
      :url.endsWith('/episodes')?{episodes:[{id:'episode',title:'Episode'}]}
      :url.endsWith('/projects')&&options.method!=='POST'?{projects:[project]}
      :url.endsWith('/assets')?{assets:[]}:url.endsWith('/jobs')?{jobs:config.job?[config.job]:[]}
      :url.endsWith('/stock/search')?{id:'search',provider:'pixabay',candidates:[candidate]}
      :url.endsWith('/exports')||url.endsWith('/build')?{id:'export',status:'queued'}:JSON.parse(JSON.stringify(project));
    return {ok:true,json:async()=>value};
  }};
  vm.runInNewContext(fs.readFileSync('app/static/video-studio.js','utf8'),context);
  node('video-episode').value='episode';node('video-format').value='vertical';
  node('video-image-preset').value='draft';node('video-encoder').value='libx264';node('video-caption-style').value='large';
  node('video-provider').value='pixabay';
  node('video-style').value='';
  return {nodes,calls,timers,click:async id=>{for(const fn of node(id).listeners.click || [])await fn({currentTarget:node(id)});},
    change:async(id,value)=>{node(id).value=value;for(const fn of node(id).listeners.change || [])await fn({target:node(id)});},
    fields:(id,values)=>{
      const inputs=Object.entries(values).map(([field,value])=>({dataset:{field},value,type:typeof value==='number'?'number':'text'}));
      node('video-scenes').querySelectorAll=()=>[{dataset:{scene:id},querySelectorAll:selector=>selector==='[data-field]'?inputs:[]}];
    },
    sceneClick:async(attr,value)=>{
      const key=attr.replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
      const button={disabled:false,dataset:{[key]:value}};
      const target={closest:selector=>selector===`[data-${attr}]`?button:null};
      for(const fn of node(attr==='stock'?'video-stock-results':'video-scenes').listeners.click || [])await fn({target});
      await new Promise(resolve=>setImmediate(resolve));
    },
    chooseSource:async value=>{
      const input={dataset:{field:'media_type'},value};
      const row={dataset:{scene:'scene'},querySelectorAll:selector=>selector==='[data-field]'?[input]:[]};
      node('video-scenes').querySelectorAll=()=>[row];
      input.closest=()=>row;
      for(const fn of node('video-scenes').listeners.change || [])await fn({target:input});
    }};
}

test('create, save and export sends full-length format settings through durable API',async()=>{
  const p=page();await p.click('video-create');
  assert.equal(p.nodes['video-end'].value,60);
  assert.match(p.nodes['video-scenes'].innerHTML,/Narration/);
  await p.click('video-export');
  const request=p.calls.find(c=>c.url.endsWith('/exports'));
  const body=JSON.parse(request.options.body);
  assert.equal(body.end,60);assert.equal(body.aspect,'vertical');assert.equal(body.captions,'large');
  assert.equal(p.nodes['video-export'].disabled,false);
});

test('failed save keeps editing state and restores controls without exporting',async()=>{
  const p=page(true);await p.click('video-create');await p.click('video-export');
  assert.match(p.nodes['video-status'].textContent,/Project changed/);
  assert.equal(p.nodes['video-status'].role,'alert');
  assert.equal(p.calls.some(c=>c.url.endsWith('/exports')),false);
  assert.equal(p.nodes['video-export'].disabled,false);
});

test('one build button saves the storyboard and queues the asset-to-video workflow',async()=>{
  const p=page(false,{ready:true});await p.click('video-create');await p.click('video-build');
  const request=p.calls.find(c=>c.url.endsWith('/build'));
  const body=JSON.parse(request.options.body);
  assert.deepEqual(body,{start:0,end:60,preview:false,aspect:'vertical',encoder:'libx264',captions:'large'});
  assert.ok(p.calls.findIndex(c=>c.options.method==='PUT')<p.calls.indexOf(request));
  assert.match(p.nodes['video-status'].textContent,/Queued: download selected B-roll/);
  assert.equal(p.nodes['video-build'].disabled,false);
  assert.equal(p.calls.some(c=>c.url.endsWith('/images')||c.url.endsWith('/stock/import')||c.url.endsWith('/exports')),false);
});

test('scene B-roll choice supports preview and deferred selection without downloading',async()=>{
  const p=page();await p.click('video-create');
  assert.match(p.nodes['video-scenes'].innerHTML,/Create an image/);
  await p.chooseSource('broll');
  assert.match(p.nodes['video-scenes'].innerHTML,/Quick footage search/);
  assert.doesNotMatch(p.nodes['video-scenes'].innerHTML,/Image prompt/);
  await p.sceneClick('search','scene');
  assert.match(p.nodes['video-stock-results'].innerHTML,/<video controls playsinline preload="none"/);
  assert.match(p.nodes['video-stock-results'].innerHTML,/Use this B-roll/);
  await p.sceneClick('stock','clip');
  const request=p.calls.find(c=>c.url.endsWith('/scenes/scene/stock'));
  const body=JSON.parse(request.options.body);
  assert.equal(body.search_id,'search');assert.equal(body.candidate_id,'clip');assert.ok(body.revision>1);
  assert.equal(p.calls.some(c=>c.url.endsWith('/stock/import')),false);
  assert.match(p.nodes['video-scenes'].innerHTML,/B-roll selected: Author/);
  assert.match(p.nodes['video-status'].textContent,/B-roll prepared/);
});

test('failed save prevents asset generation and restores build controls',async()=>{
  const p=page(true,{ready:true});await p.click('video-create');await p.click('video-build');
  assert.equal(p.calls.some(c=>c.url.endsWith('/build')),false);
  assert.match(p.nodes['video-status'].textContent,/Project changed/);
  assert.equal(p.nodes['video-build'].disabled,false);
});

test('unresolved scenes with different sources or prepared footage cannot merge',()=>{
  const scenes=[{id:'a',start:0,end:8,trim:0,asset_id:null,media_type:'image'},
    {id:'b',start:8,end:16,trim:0,asset_id:null,media_type:'broll',stock_id:'prepared'}];
  assert.throws(()=>merge(scenes,'a','image'),/same media/);
  assert.throws(()=>merge([{...scenes[0],media_type:'broll',stock_id:'other'},scenes[1]],'a'),/same media/);
});

test('new storyboards explain missing B-roll and never silently attempt an invalid build',async()=>{
  const p=page();await p.click('video-create');await p.click('video-build');
  assert.equal(p.calls.some(c=>c.url.endsWith('/build')),false);
  assert.match(p.nodes['video-status'].textContent,/New storyboards default to images/);
  assert.equal(p.nodes['video-status'].role,'alert');
  assert.match(p.nodes['video-readiness'].textContent,/Choose both/);
});

test('paging, filters and jumping preserve edits and save every scene',async()=>{
  const p=page(false,{long:true});await p.click('video-create');
  assert.equal((p.nodes['video-scenes'].innerHTML.match(/data-scene=/g)||[]).length,5);
  assert.match(p.nodes['video-page-label'].textContent,/Page 1 of 4/);
  p.fields('scene0',{prompt:'Edited before paging'});await p.click('video-page-next');
  assert.match(p.nodes['video-scenes'].innerHTML,/Scene 6/);
  assert.doesNotMatch(p.nodes['video-scenes'].innerHTML,/data-scene="scene0"/);
  await p.change('video-scene-filter','broll');
  assert.match(p.nodes['video-page-label'].textContent,/10 matching scenes/);
  p.nodes['video-scene-jump'].value=20;await p.click('video-jump');
  assert.match(p.nodes['video-scenes'].innerHTML,/Scene 20/);
  await p.click('video-save');
  const body=JSON.parse(p.calls.find(c=>c.options.method==='PUT').options.body);
  assert.equal(body.scenes.length,20);assert.equal(body.scenes[0].prompt,'Edited before paging');
});

test('GPU diagnostics failure does not hide accepted jobs or prevent polling',async()=>{
  const job={id:'export',project_id:'project',status:'running',phase:'generating images',done:1,total:2};
  const p=page(false,{ready:true,resourceFailure:true,job});await p.click('video-create');await p.click('video-build');
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(p.nodes['video-jobs'].innerHTML,/generating images/);
  assert.match(p.nodes['video-status'].textContent,/Video running/);
  assert.match(p.nodes['video-resources'].textContent,/diagnostics unavailable/);
  assert.equal(p.timers.length,1);
});

test('an accepted build remains confirmed if refreshing its status fails',async()=>{
  const p=page(false,{ready:true,jobFetchFailure:true});await p.click('video-create');await p.click('video-build');
  assert.equal(p.calls.filter(c=>c.url.endsWith('/build')).length,1);
  assert.match(p.nodes['video-status'].textContent,/Video queued.*Status refresh failed/);
  assert.equal(p.timers.length,1);
});

test('an immediate worker failure stays visible beside the build button',async()=>{
  const job={id:'export',project_id:'project',status:'failed',phase:'failed',done:0,total:2,error:'Local image service unavailable'};
  const p=page(false,{ready:true,job});await p.click('video-create');await p.click('video-build');
  assert.match(p.nodes['video-status'].textContent,/Video failed: Local image service unavailable/);
  assert.equal(p.nodes['video-status'].role,'alert');
});

test('navigation filters identify blockers while ungenerated image plans are build-ready',()=>{
  const scenes=[{id:'image',start:0,end:8,media_type:'image',asset_id:null,locked:false},
    {id:'broll',start:8,end:16,media_type:'broll',asset_id:null,stock_id:null}];
  assert.equal(scenePage(scenes,'attention',0).count,1);
  assert.equal(buildIssue({duration:16,scenes},0,16).scene_id,'broll');
  scenes[1].stock_id='chosen';
  assert.equal(buildIssue({duration:16,scenes},0,16),null);
  scenes[0].locked=true;
  assert.equal(buildIssue({duration:16,scenes},0,16).scene_id,'image');
  assert.equal(scenePage(scenes,'image',99).page,0);
});

test('one automatic click creates a storyboard and queues AI selection without manual readiness checks',async()=>{
  const p=page();p.nodes['video-style'].value='Custom direction';p.nodes['video-image-preset'].value='detail';await p.click('video-auto-build');
  const created=p.calls.find(c=>c.url.endsWith('/projects')&&c.options.method==='POST');
  assert.equal(JSON.parse(created.options.body).episode_id,'episode');
  const build=p.calls.find(c=>c.url.endsWith('/build'));
  const body=JSON.parse(build.options.body);
  assert.equal(body.auto_broll,true);assert.equal(body.provider,'pixabay');assert.equal(body.end,60);
  const saved=JSON.parse(p.calls.find(c=>c.options.method==='PUT').options.body);
  assert.equal(saved.style,'Custom direction');assert.equal(saved.image_preset,'detail');
  assert.match(p.nodes['video-status'].textContent,/local AI will plan visuals/);
  assert.equal(p.nodes['video-auto-build'].disabled,false);
});

test('automatic mode uses the loaded storyboard and preserves its selected media',async()=>{
  const p=page(false,{ready:true});await p.click('video-create');await p.click('video-auto-build');
  assert.equal(p.calls.filter(c=>c.url.endsWith('/projects')&&c.options.method==='POST').length,1);
  const saved=JSON.parse(p.calls.find(c=>c.options.method==='PUT').options.body);
  assert.equal(saved.scenes[1].stock_id,'selected');
  assert.equal(JSON.parse(p.calls.find(c=>c.url.endsWith('/build')).options.body).auto_broll,true);
});


test('provider and both quality controls survive storyboard save and reload',async()=>{
  const p=page();await p.click('video-create');
  p.nodes['video-image-provider'].value='openai';
  p.nodes['video-image-quality'].value='medium';
  p.nodes['video-image-preset'].value='detail';
  await p.click('video-save');
  const saved=JSON.parse(p.calls.find(c=>c.options.method==='PUT').options.body);
  assert.equal(saved.image_provider,'openai');
  assert.equal(saved.image_quality,'medium');
  assert.equal(saved.image_preset,'detail');
});


test('legacy storyboard uses the configured hosted quality when the field is absent',async()=>{
  const p=page();
  await p.change('video-image-quality','medium');
  p.nodes['video-image-quality'].dataset={default:'medium'};
  await p.click('video-create');
  assert.equal(p.nodes['video-image-quality'].value,'medium');
});
