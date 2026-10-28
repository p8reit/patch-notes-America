const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Import = require('../../app/static/episode-json-import.js');
const EditorState = require('../../app/static/episode-editor-state.js');
const fixture = fs.readFileSync(path.join(__dirname, '../fixtures/episode-import.json'), 'utf8');
const host = {id: 'a'.repeat(32), name: 'Wade Mercer', voice: 'wade.wav', tempo: 0.95, reference_audio_path: 'data/voices/wade/reference.wav'};

test('production document maps selected episode, separates its cold open, and preserves source metadata and voice identity', () => {
  const parsed = Import.parse(fixture);
  const episode = Import.toEditor(parsed, 0, [host]);
  assert.equal(episode.title, parsed.episodes[0].title);
  assert.equal(episode.script, "I'm Wade Mercer.\n\nThanks for listening.");
  assert.equal(episode.intro_lines, 'A little light is back.');
  assert.equal(`${episode.intro_lines}\n\n${episode.script}`, parsed.episodes[0].transcript);
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
  assert.deepEqual(metadata.episode, parsed.episodes[0]);
  assert.equal(metadata.episode.sources[0].date, null);
  assert.equal(episode.performance.enabled, false);
  const second = Import.toEditor(parsed, 1, [host]);
  assert.equal(second.script, 'A second story.');
  assert.equal(second.image_prompt, '');
  assert.equal(second.target_minutes, 8);
  assert.equal(second.intro_lines, '');
  assert.doesNotMatch(second.story_notes, /blinking light/);
});

test('wrapped production document and bare array both resolve two episodes without a collection title', () => {
  const original = JSON.parse(fixture);
  assert.equal(original.title, undefined);
  for (const raw of [fixture, JSON.stringify(original.episodes)]) {
    const parsed = Import.parse(raw);
    assert.equal(parsed.episodes.length, 2);
    for (let index = 0; index < 2; index += 1) {
      const editor = Import.toEditor(parsed, index, [host]);
      assert.equal(editor.title, original.episodes[index].title);
      assert.equal(editor.intro_lines, original.episodes[index].cold_open || '');
    }
  }
});

test('cold open is editable when separate, repeated, absent, or the entire transcript', () => {
  const base = {title: 'Cold open cases', host: host.name};
  const cases = [
    {cold_open: 'Opening.', transcript: 'Opening.\n\nMain script.', intro: 'Opening.', script: 'Main script.'},
    {cold_open: 'Opening.', transcript: 'Main script.', intro: 'Opening.', script: 'Main script.'},
    {cold_open: 'Open', transcript: 'Opening words.', intro: 'Open', script: 'Opening words.'},
    {cold_open: 'Opening.', transcript: 'Opening.', intro: 'Opening.', script: ''},
    {transcript: 'Main script.', intro: '', script: 'Main script.'},
  ];
  for (const {intro, script, ...input} of cases) {
    const editor = Import.toEditor(Import.parse(JSON.stringify({...base, ...input})), 0, [host]);
    assert.equal(editor.intro_lines, intro);
    assert.equal(editor.script, script);
    if (script !== input.transcript) assert.match(editor.story_notes, /"transcript":/);
  }
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
  for (const index of [-1, 0.5, NaN, undefined, '']) {
    assert.throws(() => Import.toEditor(parsed, index, [host]), /Choose an episode/);
  }
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
    choose(index) { elements.get('episode-json-select').value = String(index); this.fire('episode-json-select', 'change'); },
  };
}

test('read and select only preview JSON; importing the second episode leaves it dirty for saving', () => {
  const ui = setup();
  ui.read();
  assert.equal(ui.replacements, 0);
  const select = ui.elements.get('episode-json-select');
  assert.equal(select.children.length, 3);
  assert.equal(select.disabled, false);
  assert.equal(select.value, '');
  assert.equal(ui.elements.get('import-episode-json').disabled, true);
  ui.fire('import-episode-json');
  assert.equal(ui.replacements, 0);
  ui.choose(1);
  ui.fire('import-episode-json');
  assert.equal(ui.state.title, 'The second episode');
  assert.equal(ui.editorState.isDirty(), true);
  assert.match(ui.elements.get('episode-json-status').textContent, /unsaved episode/);
});

test('both episodes can be imported in sequence without mixing titles, scripts, or cold opens', () => {
  for (const raw of [fixture, JSON.stringify(JSON.parse(fixture).episodes)]) {
    const ui = setup();
    ui.read(raw);
    ui.choose(0);
    ui.fire('import-episode-json');
    assert.equal(ui.state.title, 'A little blinking light');
    assert.equal(ui.state.intro_lines, 'A little light is back.');
    assert.equal(ui.state.script, "I'm Wade Mercer.\n\nThanks for listening.");
    ui.choose(1);
    ui.fire('import-episode-json');
    assert.equal(ui.state.title, 'The second episode');
    assert.equal(ui.state.script, 'A second story.');
    assert.equal(ui.state.intro_lines, '');
    assert.equal(ui.replacements, 2);
    assert.equal(ui.elements.get('episode-json-select').children.length, 3);
  }
});

test('import reparses changed text rather than using an obsolete episode collection', () => {
  const ui = setup();
  ui.read();
  ui.choose(0);
  ui.elements.get('episode-json-input').value = JSON.stringify({title: 'New input', host: host.name, transcript: 'New words.'});
  ui.fire('import-episode-json');
  assert.equal(ui.replacements, 0);
  ui.fire('import-episode-json');
  assert.equal(ui.state.title, 'New input');
  assert.equal(ui.state.script, 'New words.');
});

test('canceled replacement, validation errors, and changed JSON preserve manual work', () => {
  const ui = setup(() => false);
  ui.state = {...ui.state, script: 'Unsaved manual edit'};
  ui.read();
  ui.choose(0);
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
  ui.choose(0);
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
  assert.match(template, /src="\/static\/episode-json-import.js\?v=2"/);
  const start = template.indexOf('window.EpisodeJsonImport.init({');
  const end = template.indexOf('window.WorkflowNavigation.init', start);
  let options;
  const elements = new Map(['saved-episode-select', 'episode-save-status', 'episode-title'].map((id) => [id, {value: 'old', focus() { this.focused = true; }}]));
  let loaded;
  const introSection = {open: false};
  vm.runInNewContext(template.slice(start, end), {
    window: {EpisodeJsonImport: {init: (value) => { options = value; }}},
    document: {getElementById: (id) => elements.get(id), querySelector: () => introSection},
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
  assert.equal(introSection.open, true);
  options.replaceEpisode(Import.toEditor(Import.parse(fixture), 1, [host]));
  assert.equal(introSection.open, false);
});
