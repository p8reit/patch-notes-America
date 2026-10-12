const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {escape,split,merge}=require('../../app/static/video-studio.js');

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

function page(failure=false){
  const nodes={};
  const node=id=>nodes[id] ||= {value:'',disabled:false,hidden:false,listeners:{},
    addEventListener(event,fn){(this.listeners[event] ||= []).push(fn);},
    setAttribute(name,value){this[name]=value;},getAttribute(name){return this[name];},
    classList:{toggle(){}},querySelectorAll(selector){return id==='video-panel'&&selector==='button'?Object.values(nodes).filter(n=>n.listeners.click):[];}};
  let project={id:'project',revision:1,title:'Episode',duration:60,style:'Style',scenes:[{id:'scene',start:0,end:60,text:'Narration',caption:'Narration',prompt:'Illustration',intent:'Context',query:'houses',asset_id:null,trim:0,crop_x:.5,crop_y:.5,motion:'still',locked:false}]};
  const candidate={id:'clip',provider:'pixabay',creator:'Author',duration:60,width:1920,height:1080,url:'https://cdn.pixabay.com/video/clip.mp4',source_url:'https://pixabay.com/videos/clip/'};
  const calls=[];
  const root={document:{getElementById:node},confirm:()=>true,crypto:{randomUUID:()=> 'new'},addEventListener(){},setTimeout(){return 1;}};
  const context={window:root,fetch:async(url,options={})=>{
    calls.push({url,options});
    if(failure&&options.method==='PUT')return {ok:false,json:async()=>({detail:'Project changed. Reload.'})};
    if(options.method==='PUT')project={...JSON.parse(options.body),revision:project.revision+1};
    if(url.endsWith('/scenes/scene/stock'))project={...project,revision:project.revision+1,scenes:project.scenes.map(s=>({...s,media_type:'broll',stock_id:'selection',stock:candidate}))};
    let value=url.endsWith('/resources')?{active:'idle',handoff_enabled:false,free_vram_mb:null}
      :url.endsWith('/episodes')?{episodes:[{id:'episode',title:'Episode'}]}
      :url.endsWith('/projects')&&options.method!=='POST'?{projects:[project]}
      :url.endsWith('/assets')?{assets:[]}:url.endsWith('/jobs')?{jobs:[]}
      :url.endsWith('/stock/search')?{id:'search',provider:'pixabay',candidates:[candidate]}
      :url.endsWith('/exports')||url.endsWith('/build')?{id:'export',status:'queued'}:JSON.parse(JSON.stringify(project));
    return {ok:true,json:async()=>value};
  }};
  vm.runInNewContext(fs.readFileSync('app/static/video-studio.js','utf8'),context);
  node('video-episode').value='episode';node('video-format').value='vertical';
  node('video-image-preset').value='draft';node('video-encoder').value='libx264';node('video-caption-style').value='large';
  return {nodes,calls,click:async id=>{for(const fn of node(id).listeners.click || [])await fn({currentTarget:node(id)});},
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
  const p=page();await p.click('video-create');await p.click('video-build');
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
  assert.match(p.nodes['video-scenes'].innerHTML,/Create a local image/);
  await p.chooseSource('broll');
  assert.match(p.nodes['video-scenes'].innerHTML,/Quick footage search/);
  assert.doesNotMatch(p.nodes['video-scenes'].innerHTML,/Local image prompt/);
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
  const p=page(true);await p.click('video-create');await p.click('video-build');
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
