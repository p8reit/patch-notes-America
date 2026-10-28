const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Description = require('../../app/static/episode-description.js');
const Import = require('../../app/static/episode-json-import.js');

function importedEpisode(index = 0) {
  const fixture = fs.readFileSync(path.join(__dirname, '../fixtures/episode-import.json'), 'utf8');
  return Import.toEditor(Import.parse(fixture), index, [{name: 'Wade Mercer', voice: 'default', tempo: 1}]);
}

function setup(writeClipboard = async () => {}) {
  const elements = new Map(['episode-description', 'copy-episode-description', 'episode-description-status', 'story-notes'].map((id) => [id, {
    value: '', disabled: false, listeners: {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
    focus() { this.focused = true; }, select() { this.selected = true; },
  }]));
  const controller = Description.init({
    document: {getElementById: (id) => elements.get(id)}, writeClipboard,
    reportStatus(element, text, error = false) { element.textContent = text; element.error = error; },
  });
  return {elements, controller, copy: () => elements.get('copy-episode-description').listeners.click()};
}

test('imported description is displayed and copied verbatim for publishing', async () => {
  let copied;
  const ui = setup(async (value) => { copied = value; });
  ui.controller.show(importedEpisode());
  assert.equal(ui.elements.get('episode-description').value, 'A story about careful fieldwork.');
  assert.equal(ui.elements.get('copy-episode-description').disabled, false);
  await ui.copy();
  assert.equal(copied, 'A story about careful fieldwork.');
  assert.match(ui.elements.get('episode-description-status').textContent, /Description copied/);
});

test('switching episodes or starting new work clears the preceding description', () => {
  const ui = setup();
  ui.controller.show(importedEpisode());
  ui.controller.show(importedEpisode(1));
  assert.equal(ui.elements.get('episode-description').value, '');
  assert.equal(ui.elements.get('copy-episode-description').disabled, true);
  ui.controller.show(importedEpisode());
  ui.controller.show({});
  assert.equal(ui.elements.get('episode-description').value, '');
  assert.match(ui.elements.get('episode-description-status').textContent, /No episode description/);
});

test('legacy, manual and damaged notes remain loadable without inventing descriptions', () => {
  for (const episode of [{}, {story_notes: 'Manual notes'}, {story_notes: 'Imported production and episode details:\n{'}, {story_notes: 'Imported production and episode details:\nnull'}, {story_notes: 'Imported production and episode details:\n{"episode":{"description":5}}'}]) {
    assert.equal(Description.descriptionFromEpisode(episode), '');
  }
  const description = 'Line one.\n\n"Quoted text" & <b>literal text</b> — café.';
  assert.equal(Description.descriptionFromEpisode({story_notes: 'Imported production and episode details:\n' + JSON.stringify({episode: {description}})}), description);
});

test('description follows updates to retained source notes without modifying them', () => {
  const ui = setup();
  const notes = ui.elements.get('story-notes');
  notes.value = importedEpisode().story_notes;
  const original = notes.value;
  notes.listeners.input();
  assert.equal(ui.elements.get('episode-description').value, 'A story about careful fieldwork.');
  assert.equal(notes.value, original);
});

test('unavailable or rejected clipboard access selects text for manual copying', async () => {
  for (const clipboard of [null, async () => { throw new Error('Denied'); }]) {
    const ui = setup(clipboard);
    ui.controller.show(importedEpisode());
    await ui.copy();
    assert.equal(ui.elements.get('episode-description').focused, true);
    assert.equal(ui.elements.get('episode-description').selected, true);
    assert.equal(ui.elements.get('copy-episode-description').disabled, false);
    assert.match(ui.elements.get('episode-description-status').textContent, /Ctrl\+C or Cmd\+C/);
  }
});

test('late clipboard completion does not show stale status after switching episodes', async () => {
  let finish;
  const ui = setup(() => new Promise((resolve) => { finish = resolve; }));
  ui.controller.show(importedEpisode());
  const copying = ui.copy();
  assert.match(ui.elements.get('episode-description-status').textContent, /Copying/);
  ui.controller.show(importedEpisode(1));
  finish();
  await copying;
  assert.match(ui.elements.get('episode-description-status').textContent, /No episode description/);
  assert.equal(ui.elements.get('copy-episode-description').disabled, true);
});

test('all editor replacements refresh the labeled read-only description without changing save serialization', () => {
  const template = fs.readFileSync(path.join(__dirname, '../../app/templates/index.html'), 'utf8');
  assert.match(template, /<label for="episode-description">/);
  assert.match(template, /id="episode-description"[^>]*readonly/);
  assert.ok(template.indexOf('id="episode-description"') < template.indexOf('id="stage-2"'));
  const start = template.indexOf('function loadEpisodeIntoEditor(');
  const end = template.indexOf("document.getElementById('new-episode')", start);
  let displayed;
  const control = {value: '', checked: false, replaceChildren() {}, dispatchEvent() {}};
  const context = {
    document: {querySelector: () => control, getElementById: () => control},
    episodeDescription: {show: (episode) => { displayed = episode; }},
    hostsContainer: control, addHost() {}, addPacketStory() {}, renderPerformanceBeats() {}, showSavedIntro() {},
    introMusicVolume: control, Event: class {},
  };
  vm.runInNewContext(`${template.slice(start, end)}; this.load = loadEpisodeIntoEditor;`, context);
  const episode = importedEpisode();
  context.load(episode, 'saved-id');
  assert.equal(displayed, episode);
  const serialize = template.slice(template.indexOf('function serializeEpisode()'), template.indexOf('let removeSavedIntro'));
  assert.doesNotMatch(serialize, /episode-description/);
});
