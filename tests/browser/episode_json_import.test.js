const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Import = require('../../app/static/episode-json-import.js');
const EditorState = require('../../app/static/episode-editor-state.js');
const fixture = fs.readFileSync(path.join(__dirname, '../fixtures/episode-import.json'), 'utf8');
const host = {id: 'a'.repeat(32), name: 'Wade Mercer', voice: 'wade.wav', tempo: 0.95, reference_audio_path: 'data/voices/wade/reference.wav'};

test('production document maps selected episode and preserves transcript, source metadata, and voice identity', () => {
  const parsed = Import.parse(fixture);
  const episode = Import.toEditor(parsed, 0, [host]);
  assert.equal(episode.title, parsed.episodes[0].title);
  assert.equal(episode.script, parsed.episodes[0].transcript);
  assert.equal(episode.intro_lines, '');
  assert.deepEqual(episode.hosts, [host]);
  assert.notEqual(episode.hosts[0], host);
  assert.equal(episode.target_minutes, 15);
  assert.equal(episode.conversation_tone, parsed.shared.editorial_direction);
  assert.equal(episode.image_prompt, 'Fireflies over a meadow');
  const story = episode.research_packet.stories[0];
  assert.equal(episode.research_packet.episode_angle, 'Rediscovery and conservation');
  assert.equal(story.verified_facts, 'An observation was documented.');
  assert.equal(story.disputed_or_uncertain, 'Population size is unknown.');
  assert.equal(story.angles, 'Avoid overstating recovery.');
  assert.match(story.sources[0], /https:\/\/example.com\/report/);
  const metadata = JSON.parse(episode.story_notes.slice(episode.story_notes.indexOf('{')));
  assert.deepEqual(metadata.production, parsed.shared);
  const {transcript, ...otherFields} = parsed.episodes[0];
  assert.deepEqual(metadata.episode, otherFields);
  assert.equal(metadata.episode.sources[0].date, null);
  assert.equal(episode.performance.enabled, false);
  const second = Import.toEditor(parsed, 1, [host]);
  assert.equal(second.script, 'A second story.');
  assert.equal(second.image_prompt, '');
  assert.equal(second.target_minutes, 8);
  assert.doesNotMatch(second.story_notes, /blinking light/);
});

test('single episode, BOM, string sources, and fractional runtime are supported', () => {
  const episode = JSON.parse(fixture).episodes[0];
  episode.sources = ['https://example.com'];
  episode.estimated_runtime_minutes = 12.4;
  const parsed = Import.parse('\uFEFF' + JSON.stringify(episode));
  const result = Import.toEditor(parsed, 0, [{...host, name: 'WADE MERCER'}]);
  assert.equal(result.target_minutes, 12);
  assert.deepEqual(result.research_packet.stories[0].sources, episode.sources);
});

test('malformed, incomplete, oversized, and incompatible JSON is rejected', () => {
  for (const raw of ['', '{"episodes":[', 'null', '[]', '{"episodes":[]}', '{"schema_version":"2.0"}', 'x'.repeat(2 * 1024 * 1024 + 1)]) {
    assert.throws(() => Import.parse(raw));
  }
  const cases = [
    ['title', ''], ['host', null], ['transcript', 5], ['estimated_runtime_minutes', 61],
    ['estimated_runtime_minutes', '15'], ['estimated_runtime_minutes', 0],
    ['promotional_creative', []], ['promotional_creative', {image_prompt: 'x'.repeat(2001)}],
    ['fact_check', {confirmed: 'not a list'}], ['fact_check', {editorial_cautions: [true]}],
    ['sources', [false]], ['sources', [{url: 5}]], ['sources', {}],
  ];
  for (const [key, value] of cases) {
    const episode = {...JSON.parse(fixture).episodes[0], [key]: value};
    assert.throws(() => Import.parse(JSON.stringify({episodes: [episode]})), /Episode 1:/, key);
  }
});

test('unknown and ambiguous hosts require explicit cast configuration', () => {
  const parsed = Import.parse(fixture);
  assert.throws(() => Import.toEditor(parsed, 0, []), /Add or rename a host in Cast/);
  assert.throws(() => Import.toEditor(parsed, 0, [host, {...host, id: 'b'.repeat(32)}]), /unique names/);
});

function setup(confirm = () => true) {
  const elements = new Map();
  for (const id of ['episode-json-input', 'episode-json-file', 'read-episode-json', 'import-episode-json', 'episode-json-select', 'episode-json-status']) {
    elements.set(id, {
      value: '', files: [], disabled: id.includes('select') || id === 'import-episode-json',
      children: [], listeners: {}, textContent: '',
      addEventListener(event, callback) { this.listeners[event] = callback; },
      replaceChildren() { this.children = []; this.value = ''; },
      appendChild(child) { this.children.push(child); },
      focus() { this.focused = true; },
    });
  }
  let state = {title: 'Manual episode', script: 'Existing manual script', hosts: [host]};
  let replacements = 0;
  const editorState = EditorState.init({
    window: {addEventListener() {}}, form: {addEventListener() {}}, serialize: () => state, confirm,
  });
  Import.init({
    document: {getElementById: (id) => elements.get(id), createElement: () => ({})},
    reportStatus(element, message, error = false) { element.textContent = message; element.error = error; },
    getHosts: () => [host],
    confirmReplacement: (title) => editorState.confirmEpisodeReplacement(title),
    replaceEpisode: (episode) => { state = episode; replacements += 1; },
  });
  return {
    elements, editorState,
    get state() { return state; }, set state(value) { state = value; },
    get replacements() { return replacements; },
    fire: (id, event = 'click') => elements.get(id).listeners[event](),
    read(raw = fixture) { elements.get('episode-json-input').value = raw; return this.fire('read-episode-json'); },
  };
}

test('read and select only preview JSON; importing the second episode leaves it dirty for saving', () => {
  const ui = setup();
  ui.read();
  assert.equal(ui.replacements, 0);
  const select = ui.elements.get('episode-json-select');
  assert.equal(select.children.length, 2);
  assert.equal(select.disabled, false);
  select.value = '1';
  ui.fire('import-episode-json');
  assert.equal(ui.state.title, 'The second episode');
  assert.equal(ui.editorState.isDirty(), true);
  assert.match(ui.elements.get('episode-json-status').textContent, /unsaved episode/);
});

test('canceled replacement, validation errors, and changed JSON preserve manual work', () => {
  const ui = setup(() => false);
  ui.state = {...ui.state, script: 'Unsaved manual edit'};
  ui.read();
  ui.fire('import-episode-json');
  assert.equal(ui.replacements, 0);
  assert.match(ui.elements.get('episode-json-status').textContent, /canceled/);
  ui.read('{"episodes":[');
  assert.equal(ui.elements.get('import-episode-json').disabled, true);
  assert.equal(ui.elements.get('episode-json-status').error, true);
  assert.equal(ui.state.script, 'Unsaved manual edit');
  ui.read();
  ui.fire('episode-json-input', 'input');
  ui.fire('import-episode-json');
  assert.equal(ui.replacements, 0);
  assert.equal(ui.elements.get('episode-json-select').disabled, true);
});

test('file read has loading, success and error states and ignores stale reads after paste', async () => {
  const ui = setup();
  const file = ui.elements.get('episode-json-file');
  const input = ui.elements.get('episode-json-input');
  const read = ui.elements.get('read-episode-json');
  let resolve;
  file.files = [{size: 100, text: () => new Promise((done) => { resolve = done; })}];
  const pending = ui.fire('episode-json-file', 'change');
  assert.equal(read.disabled, true);
  assert.match(ui.elements.get('episode-json-status').textContent, /Reading/);
  input.value = fixture;
  ui.fire('episode-json-input', 'input');
  resolve('stale file');
  await pending;
  assert.equal(input.value, fixture);
  assert.equal(read.disabled, false);
  file.files = [{size: 10, text: async () => fixture}];
  await ui.fire('episode-json-file', 'change');
  assert.match(ui.elements.get('episode-json-status').textContent, /File ready/);
  ui.fire('read-episode-json');
  assert.equal(ui.elements.get('import-episode-json').disabled, false);
  file.files = [{size: 3 * 1024 * 1024}];
  await ui.fire('episode-json-file', 'change');
  assert.equal(ui.elements.get('episode-json-status').error, true);
  assert.equal(ui.elements.get('import-episode-json').disabled, true);
  assert.equal(input.value, fixture);
  file.files = [{size: 10, text: async () => { throw new Error('Read failed'); }}];
  await ui.fire('episode-json-file', 'change');
  assert.match(ui.elements.get('episode-json-status').textContent, /Read failed/);
  assert.equal(read.disabled, false);
});

test('template integration imports as a new document without marking it saved', () => {
  const template = fs.readFileSync(path.join(__dirname, '../../app/templates/index.html'), 'utf8');
  assert.match(template, /src="\/static\/episode-json-import.js"/);
  const start = template.indexOf('window.EpisodeJsonImport.init({');
  const end = template.indexOf('window.WorkflowNavigation.init', start);
  let options;
  const elements = new Map(['saved-episode-select', 'episode-save-status', 'episode-title'].map((id) => [id, {value: 'old', focus() { this.focused = true; }}]));
  let loaded;
  vm.runInNewContext(template.slice(start, end), {
    window: {EpisodeJsonImport: {init: (value) => { options = value; }}},
    document: {getElementById: (id) => elements.get(id)},
    reportStatus: (element, message) => { element.textContent = message; },
    serializeHosts: () => [{...host, tempo: 1.2}], initialProfiles: [host],
    editorState: {confirmEpisodeReplacement: () => true},
    loadEpisodeIntoEditor: (episode, id) => { loaded = {episode, id}; },
  });
  assert.equal(options.getHosts().length, 1);
  assert.equal(options.getHosts()[0].tempo, 1.2);
  const episode = Import.toEditor(Import.parse(fixture), 0, [host]);
  options.replaceEpisode(episode);
  assert.equal(loaded.id, '');
  assert.equal(loaded.episode, episode);
  assert.equal(elements.get('saved-episode-select').value, '');
  assert.equal(elements.get('episode-title').focused, true);
});
