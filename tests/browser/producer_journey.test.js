const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const template = fs.readFileSync(
  path.join(__dirname, '../../app/templates/index.html'), 'utf8',
);

function handler(start, end) {
  const from = template.indexOf(start);
  assert.notEqual(from, -1, `missing journey step: ${start}`);
  const to = end ? template.indexOf(end, from + start.length) : template.length;
  assert.notEqual(to, -1, `missing journey boundary: ${end}`);
  return template.slice(from, to);
}

test('primary producer journey remains wired to mocked route boundaries', () => {
  assert.match(template, /id="episode-title"/);
  assert.match(template, /id="episode-script"/);

  const save = handler("document.getElementById('save-episode').addEventListener", "document.getElementById('load-episode')");
  assert.match(save, /serializeEpisode\(\)/);
  assert.match(save, /fetch\('\/api\/saved-episodes', \{method: 'POST'/);

  const draft = handler("document.getElementById('draft-conversation').addEventListener", "document.getElementById('episode-form')");
  assert.match(draft, /fetch\('\/api\/conversation-draft'/);
  assert.match(draft, /getElementById\('episode-script'\)\.value = data\.script/);

  const submit = handler("document.getElementById('episode-form').addEventListener", 'function useClipSuggestion');
  assert.match(submit, /fetch\('\/api\/generation-jobs'/);
  assert.match(submit, /monitorJob\(data\.job_id\)/);

  const monitor = handler('function monitorJob(jobId)', "document.getElementById('refresh-jobs')");
  assert.match(monitor, /`\/api\/generation-jobs\/\$\{encodeURIComponent\(jobId\)\}`/);
  assert.match(monitor, /job\.status === 'complete'/);
  assert.match(monitor, /setActiveCompletedEpisode\(job\)/);
  assert.match(template, /href="\$\{encodeURI\(job\.download_url\)\}">Download MP3<\/a>/);

  const activeEpisode = handler('function setActiveCompletedEpisode(job)', 'async function refreshJobs()');
  assert.match(activeEpisode, /getElementById\('clip-studio'\)\.hidden = !currentEpisode/);

  const suggestions = handler("document.getElementById('suggest-clips').addEventListener", "document.getElementById('render-clip')");
  assert.match(suggestions, /`\/api\/episodes\/\$\{currentEpisode\}\/clip-suggestions`/);
  assert.match(suggestions, /useClipSuggestion\(item, index\)/);

  const render = handler("document.getElementById('render-clip').addEventListener");
  assert.match(render, /`\/api\/episodes\/\$\{currentEpisode\}\/clips`/);
  assert.match(render, /href="\$\{data\.download_url\}">Download \$\{data\.aspect\} MP4<\/a>/);
});
