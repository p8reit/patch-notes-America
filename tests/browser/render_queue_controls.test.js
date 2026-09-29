const test = require('node:test');
const assert = require('node:assert/strict');
const QueueControls = require('../../app/static/render-queue-controls.js');

const jobs = [
  {title: 'Morning update', status: 'complete', created_at: '2026-09-27T12:00:00Z'},
  {title: 'Election desk', status: 'running', created_at: '2026-09-28T09:00:00Z'},
  {title: 'Morning follow-up', status: 'failed', created_at: '2026-09-28T12:00:00Z'},
  {title: 'Late bulletin', status: 'queued', created_at: '2026-09-28T14:00:00Z'},
];

test('filters queue by search and grouped active status', () => {
  assert.deepEqual(
    QueueControls.filterJobs(jobs, {status: 'active'}).map((job) => job.title),
    ['Late bulletin', 'Election desk'],
  );
  assert.deepEqual(
    QueueControls.filterJobs(jobs, {query: 'morning'}).map((job) => job.title),
    ['Morning follow-up', 'Morning update'],
  );
});

test('sorts queue chronologically without mutating API results', () => {
  const original = jobs.map((job) => job.title);
  const oldest = QueueControls.filterJobs(jobs, {order: 'oldest'});
  assert.equal(oldest[0].title, 'Morning update');
  assert.deepEqual(jobs.map((job) => job.title), original);
});

test('summarizes queue states for the live status', () => {
  const counts = QueueControls.summarizeJobs(jobs);
  assert.deepEqual(counts, {total: 4, active: 2, complete: 1, failed: 1});
  assert.equal(QueueControls.summaryText(counts, 2, 1), '2 of 4 shown · 2 active · 1 completed · 1 failed · 1 worker');
});

test('persists the queue view and migrates the former hide-completed preference', () => {
  const values = new Map([['podcast-builder-hide-completed', 'true']]);
  const storage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  };
  assert.deepEqual(QueueControls.readView(storage), {status: 'active', order: 'newest'});
  QueueControls.saveView(storage, {status: 'failed', order: 'oldest'});
  assert.deepEqual(QueueControls.readView(storage), {status: 'failed', order: 'oldest'});
});
