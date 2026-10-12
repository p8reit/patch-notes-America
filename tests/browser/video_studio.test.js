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
  const project={id:'project',revision:1,title:'Episode',duration:60,style:'Style',scenes:[{id:'scene',start:0,end:60,text:'Narration',caption:'Narration',prompt:'Illustration',intent:'Context',query:'houses',asset_id:null,trim:0,crop_x:.5,crop_y:.5,motion:'still',locked:false}]};
  const calls=[];
  const root={document:{getElementById:node},confirm:()=>true,crypto:{randomUUID:()=> 'new'},addEventListener(){},setTimeout(){return 1;}};
  const context={window:root,fetch:async(url,options={})=>{
    calls.push({url,options});
    if(failure&&options.method==='PUT')return {ok:false,json:async()=>({detail:'Project changed. Reload.'})};
    let value=url.endsWith('/resources')?{active:'idle',handoff_enabled:false,free_vram_mb:null}
      :url.endsWith('/episodes')?{episodes:[{id:'episode',title:'Episode'}]}
      :url.endsWith('/projects')&&options.method!=='POST'?{projects:[project]}
      :url.endsWith('/assets')?{assets:[]}:url.endsWith('/jobs')?{jobs:[]}
      :url.endsWith('/exports')?{id:'export',status:'queued'}:JSON.parse(JSON.stringify(project));
    return {ok:true,json:async()=>value};
  }};
  vm.runInNewContext(fs.readFileSync('app/static/video-studio.js','utf8'),context);
  node('video-episode').value='episode';node('video-format').value='vertical';
  node('video-image-preset').value='draft';node('video-encoder').value='libx264';node('video-caption-style').value='large';
  return {nodes,calls,click:async id=>{for(const fn of node(id).listeners.click || [])await fn({currentTarget:node(id)});}};
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
