const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../app/static/image-creation-progress.js');

function fixture() {
  let time = 0;
  let tick;
  let canceled = 0;
  const panel = {hidden: true};
  const elapsed = {};
  const log = {
    entries: [],
    ownerDocument: {createElement: () => ({})},
    appendChild(entry) { this.entries.push(entry.textContent); },
    replaceChildren() { this.entries = []; },
  };
  const progress = create({panel, elapsed, log, now: () => time,
    schedule: (callback) => { tick = callback; return 0; },
    cancel: () => { canceled++; },
  });
  return {panel, elapsed, log, progress, advance(seconds) { time += seconds * 1000; tick(); },
    get canceled() { return canceled; }};
}

test('pending requests show elapsed time and one honest long-wait notice', () => {
  const f = fixture();
  f.progress.start({aspect: 'square', promptLength: 25});
  assert.equal(f.panel.hidden, false);
  assert.match(f.log.entries[0], /square.*25 prompt characters/);
  f.advance(61);
  assert.equal(f.elapsed.textContent, '61s elapsed');
  assert.match(f.log.entries.at(-1), /Still waiting.*does not indicate completion percentage/);
  const count = f.log.entries.length;
  f.advance(10);
  assert.equal(f.log.entries.length, count);
  f.progress.finish('Complete.');
  assert.equal(f.canceled, 1);
  assert.match(f.log.entries.at(-1), /\+71s.*Complete/);
});

test('retry resets old activity and writes error strings as text', () => {
  const f = fixture();
  f.progress.start({aspect: 'square', promptLength: 3});
  f.progress.finish('Failed: <script>untrusted</script>');
  assert.equal(f.log.entries.at(-1), '+0s — Failed: <script>untrusted</script>');
  f.advance(5);
  f.progress.start({aspect: 'portrait', promptLength: 8});
  assert.equal(f.elapsed.textContent, '0s elapsed');
  assert.equal(f.log.entries.length, 2);
  assert.match(f.log.entries[0], /portrait/);
});

test('a slow image download does not claim the server has not responded', () => {
  const f = fixture();
  f.progress.start({aspect: 'square', promptLength: 25});
  f.progress.response(200);
  f.advance(65);
  assert.equal(f.elapsed.textContent, '65s elapsed');
  assert.match(f.log.entries.at(-1), /HTTP 200/);
  assert.equal(f.log.entries.some((entry) => entry.includes('Still waiting')), false);
  f.progress.finish('Complete.');
});

function page(fetch) {
  const f = fixture();
  let submit;
  const nodes = {
    'image-progress': f.panel, 'image-elapsed': f.elapsed, 'image-activity': f.log,
    'create-image': {disabled: false}, 'image-status': {}, 'image-result': {hidden: true},
    'generated-image': {}, 'download-image': {},
    'image-form': {addEventListener: (_name, handler) => { submit = handler; }},
  };
  const source = fs.readFileSync('app/templates/index.html', 'utf8');
  const script = source.slice(source.indexOf('    const imageProgress ='), source.indexOf('    function fieldError'));
  const context = {
    window: {ImageCreationProgress: {create: () => f.progress}},
    document: {getElementById: (id) => nodes[id]},
    FormData: class { get(key) { return key === 'aspect' ? 'square' : 'Test prompt'; } },
    fetch,
    URL: {createObjectURL: () => 'blob:generated', revokeObjectURL: () => {}},
    generatedImageUrl: null,
    reportStatus: (element, message, error) => { element.message = message; element.error = error; },
  };
  vm.runInNewContext(script, context);
  return {...f, nodes, submit: () => submit({preventDefault() {}, target: {}})};
}

test('page tracks a pending request through HTTP response, PNG receipt, and completion', async () => {
  let resolve;
  let calls = 0;
  const p = page(() => { calls++; return new Promise((done) => { resolve = done; }); });
  const pending = p.submit();
  assert.equal(p.nodes['create-image'].disabled, true);
  await p.submit();
  assert.equal(calls, 1);
  resolve({ok: true, status: 200, blob: async () => ({size: 2048, type: 'image/png'})});
  await pending;
  assert.match(p.log.entries.join('\n'), /HTTP 200.*\n.*Receiving.*\n.*2 KB.*\n.*Complete/);
  assert.equal(p.nodes['image-result'].hidden, false);
  assert.equal(p.nodes['download-image'].href, 'blob:generated');
  assert.equal(p.nodes['create-image'].disabled, false);
});

test('HTTP failure stays visible and permits retry', async () => {
  const p = page(async () => ({ok: false, status: 503, json: async () => ({detail: 'Provider unavailable'})}));
  await p.submit();
  assert.match(p.log.entries.join('\n'), /HTTP 503.*\n.*Failed: Provider unavailable/);
  assert.equal(p.nodes['image-status'].error, true);
  assert.equal(p.nodes['create-image'].disabled, false);
  assert.equal(p.nodes['image-result'].hidden, true);
});

test('network and invalid PNG failures end the activity and restore the button', async () => {
  for (const fetch of [async () => { throw new Error('Network disconnected'); },
    async () => ({ok: true, status: 200, blob: async () => ({size: 0, type: 'text/html'})})]) {
    const p = page(fetch);
    await p.submit();
    assert.match(p.log.entries.at(-1), /Failed:/);
    assert.equal(p.nodes['create-image'].disabled, false);
    assert.equal(p.nodes['image-result'].hidden, true);
  }
});
