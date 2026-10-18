const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function panel(responses) {
  const timers = [];
  const status = {rows: [], replacements: 0, textContent: '', replaceChildren(...rows) {
    this.rows = rows; this.replacements++;
  }};
  const document = {hidden: false, getElementById: () => status, createElement: () => ({textContent: ''})};
  const calls = [];
  vm.runInNewContext(fs.readFileSync('app/static/production-queue.js', 'utf8'), {
    document, window: {setTimeout: callback => timers.push(callback)},
    fetch: async url => {
      calls.push(url);
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return {ok: true, json: async () => response};
    },
  });
  return {status, document, calls, timers};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('shows active work and FIFO positions as text, without repeatedly announcing unchanged state', async () => {
  const queue = {active: {kind: 'audio', id: 'episode'}, waiting: [
    {kind: 'image', id: 'request', position: 1}, {kind: 'video', id: '<script>', position: 2},
  ]};
  const p = panel([queue, queue]);
  await settle();
  assert.deepEqual(p.status.rows.map(row => row.textContent), [
    'Running: Audio · episode', 'Waiting #1: Image', 'Waiting #2: Video · <script>',
  ]);
  assert.equal(p.calls[0], '/api/production-queue');
  await p.timers.shift()();
  assert.equal(p.status.replacements, 1);
});

test('retries after a failed poll and reports an empty recovered queue', async () => {
  const p = panel([new Error('offline'), {active: null, waiting: []}]);
  await settle();
  assert.match(p.status.textContent, /unavailable.*Retrying/);
  await p.timers.shift()();
  assert.equal(p.status.rows[0].textContent, 'Queue empty. Ready for your next job.');
});

test('pauses polling while the document is hidden and resumes on the next tick', async () => {
  const p = panel([{active: null, waiting: []}, {active: {kind: 'image', id: 'request'}, waiting: []}]);
  await settle();
  p.document.hidden = true;
  await p.timers.shift()();
  assert.equal(p.calls.length, 1);
  p.document.hidden = false;
  await p.timers.shift()();
  assert.equal(p.status.rows[0].textContent, 'Running: Image');
});
