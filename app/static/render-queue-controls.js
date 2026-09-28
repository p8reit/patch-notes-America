(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RenderQueueControls = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const STORAGE_KEY = 'podcast-builder-render-queue-view';
  const VALID_STATUSES = new Set(['all', 'active', 'complete', 'failed']);
  const VALID_ORDERS = new Set(['newest', 'oldest']);

  function filterJobs(jobs, {query = '', status = 'all', order = 'newest'} = {}) {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const matchesStatus = (job) => status === 'all'
      || (status === 'active' && ['queued', 'running'].includes(job.status))
      || job.status === status;
    return jobs.filter((job) => matchesStatus(job)
      && (!normalizedQuery || String(job.title || '').toLocaleLowerCase().includes(normalizedQuery)))
      .slice()
      .sort((left, right) => {
        const comparison = String(left.created_at || '').localeCompare(String(right.created_at || ''));
        return order === 'oldest' ? comparison : -comparison;
      });
  }

  function summarizeJobs(jobs) {
    return jobs.reduce((counts, job) => {
      counts.total += 1;
      if (['queued', 'running'].includes(job.status)) counts.active += 1;
      else if (job.status === 'complete') counts.complete += 1;
      else if (job.status === 'failed') counts.failed += 1;
      return counts;
    }, {total: 0, active: 0, complete: 0, failed: 0});
  }

  function summaryText(counts, visible, workers) {
    const parts = [`${visible} of ${counts.total} shown`, `${counts.active} active`, `${counts.complete} completed`];
    if (counts.failed) parts.push(`${counts.failed} failed`);
    parts.push(`${workers} worker${workers === 1 ? '' : 's'}`);
    return parts.join(' · ');
  }

  function readView(storage) {
    let saved = {};
    try { saved = JSON.parse(storage.getItem(STORAGE_KEY) || '{}'); } catch (_error) { saved = {}; }
    const legacyHideCompleted = storage.getItem('podcast-builder-hide-completed') === 'true';
    return {
      status: VALID_STATUSES.has(saved.status) ? saved.status : legacyHideCompleted ? 'active' : 'all',
      order: VALID_ORDERS.has(saved.order) ? saved.order : 'newest',
    };
  }

  function saveView(storage, view) {
    storage.setItem(STORAGE_KEY, JSON.stringify({status: view.status, order: view.order}));
  }

  return {filterJobs, summarizeJobs, summaryText, readView, saveView};
}));
