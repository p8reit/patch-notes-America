const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const QueueControls = require('../../app/static/render-queue-controls.js');

const source = fs.readFileSync('app/templates/index.html', 'utf8');
function section(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

function page(fetch, jobs = [{id: 'stuck-job', title: 'Stuck artwork', status: 'running'}]) {
  let click;
  const timers = [];
  const cleared = [];
  const nodes = {
    'job-list': {addEventListener: (_type, handler) => { click = handler; }},
    'job-action-status': {}, 'job-summary': {}, 'progress': {},
    'job-search': {value: ''}, 'job-status-filter': {value: 'all'}, 'job-sort-order': {value: 'newest'},
  };
  const context = {
    window: {RenderQueueControls: QueueControls,
      clearTimeout: id => cleared.push(id), setTimeout: callback => { timers.push(callback); return timers.length; }},
    document: {getElementById: id => nodes[id]},
    latestJobs: jobs, configuredWorkers: 1, activeJobPollers: new Map([['stuck-job', 99]]),
    fetch, formatElapsed: () => '1m',
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
    reportStatus: (element, message, error = false) => Object.assign(element, {message, error}),
    readJsonResponse: async (response) => {
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail);
      return body;
    },
    refreshJobs: async () => {},
  };
  vm.runInNewContext(section('    function renderJobs(', '    function setActiveCompletedEpisode(')
    + section('    const cancellingJobs =', "    document.getElementById('refresh-jobs')"), context);
  const button = {dataset: {cancelJob: 'stuck-job'}, disabled: false};
  return {context, nodes, button, timers, cleared,
    render: () => vm.runInNewContext('renderJobs(latestJobs, configuredWorkers)', context),
    cancel: () => click({target: {closest: () => button}}),
  };
}

test('running render cancels through API, stops polling, and retains assembled MP3 download', async () => {
  let finish;
  const calls = [];
  const p = page((url, options) => { calls.push({url, options}); return new Promise(resolve => { finish = resolve; }); });
  p.render();
  assert.match(p.nodes['job-list'].innerHTML, /aria-label="Cancel render: Stuck artwork"/);
  const pending = p.cancel();
  assert.equal(p.button.disabled, true);
  assert.equal(p.button.textContent, 'Cancelling…');
  await p.cancel();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/generation-jobs/stuck-job/cancel');
  assert.equal(calls[0].options.method, 'POST');
  finish({ok: true, json: async () => ({id: 'stuck-job', title: 'Stuck artwork', status: 'cancelled',
    download_url: '/api/episodes/stuck-job/download'})});
  await pending;
  assert.deepEqual(p.cleared, [99]);
  assert.equal(p.context.activeJobPollers.has('stuck-job'), false);
  assert.match(p.nodes['job-action-status'].message, /cancelled.*audio retained/);
  assert.match(p.nodes['job-list'].innerHTML, /Download MP3/);
  assert.doesNotMatch(p.nodes['job-list'].innerHTML, /data-cancel-job/);
});

test('cancellation failures stay visible and permit retry', async () => {
  for (const fetch of [async () => { throw new Error('Network disconnected'); },
    async () => ({ok: false, json: async () => ({detail: 'Already completed'})})]) {
    const p = page(fetch);
    await p.cancel();
    assert.equal(p.nodes['job-action-status'].error, true);
    assert.match(p.nodes['job-action-status'].message, /Could not confirm cancellation/);
    assert.match(p.nodes['job-list'].innerHTML, /data-cancel-job/);
    assert.doesNotMatch(p.nodes['job-list'].innerHTML, /Cancelling…/);
    assert.equal(p.context.activeJobPollers.has('stuck-job'), true);
  }
});

test('only active jobs get a keyboard-operable cancel button with escaped title and ID', () => {
  const p = page(null, [
    {id: 'queued', title: 'Queued', status: 'queued'},
    {id: 'odd"id', title: '<untrusted>', status: 'running'},
    {id: 'cancelled', title: 'Cancelled', status: 'cancelled'},
    {id: 'failed', title: 'Failed', status: 'failed'},
    {id: 'complete', title: 'Completed', status: 'complete', download_url: '/done'},
  ]);
  p.render();
  const markup = p.nodes['job-list'].innerHTML;
  assert.equal((markup.match(/data-cancel-job=/g) || []).length, 2);
  assert.match(markup, /data-cancel-job="odd&quot;id"/);
  assert.match(markup, /Cancel render: &lt;untrusted>/);
  assert.doesNotMatch(markup, /<untrusted>/);
  assert.match(p.nodes['job-summary'].textContent, /1 cancelled/);
});

test('a poll refresh sees durable cancellation and never schedules more polls', async () => {
  const p = page(async () => ({ok: true, json: async () => ({id: 'stuck-job', status: 'running'})}));
  p.context.activeJobPollers.clear();
  p.context.refreshJobs = async () => { p.context.latestJobs = [{id: 'stuck-job', status: 'cancelled'}]; };
  vm.runInNewContext('monitorJob("stuck-job")', p.context);
  await p.timers[0]();
  assert.equal(p.timers.length, 1);
  assert.equal(p.context.activeJobPollers.has('stuck-job'), false);
  assert.match(p.nodes.progress.message, /cancelled/);
});

test('cancelled queue filtering and saved view survive reload', () => {
  const jobs = [{status: 'queued'}, {status: 'cancelled'}];
  assert.deepEqual(QueueControls.filterJobs(jobs, {status: 'cancelled'}), [jobs[1]]);
  assert.deepEqual(QueueControls.filterJobs(jobs, {status: 'active'}), [jobs[0]]);
  const storage = {getItem: key => key.includes('render-queue') ? '{"status":"cancelled"}' : null};
  assert.equal(QueueControls.readView(storage).status, 'cancelled');
});

test('a late poll failure cannot restart polling after cancellation succeeds', async () => {
  let rejectPoll;
  const p = page(url => url.endsWith('/cancel')
    ? Promise.resolve({ok: true, json: async () => ({id: 'stuck-job', status: 'cancelled'})})
    : new Promise((_resolve, reject) => { rejectPoll = reject; }));
  p.context.activeJobPollers.clear();
  vm.runInNewContext('monitorJob("stuck-job")', p.context);
  const pending = p.timers[0]();
  await p.cancel();
  rejectPoll(new Error('Late connection failure'));
  await pending;
  assert.equal(p.context.activeJobPollers.has('stuck-job'), false);
  assert.equal(p.timers.length, 1);
  assert.equal(p.nodes.progress.message, undefined);
});
