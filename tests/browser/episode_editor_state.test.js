const test = require('node:test');
const assert = require('node:assert/strict');
const EpisodeEditorState = require('../../app/static/episode-editor-state.js');

function setup(confirm = () => true) {
  const listeners = {};
  const windowListeners = {};
  const form = {addEventListener: (name, callback) => { listeners[name] = callback; }};
  const window = {addEventListener: (name, callback) => { windowListeners[name] = callback; }};
  let state = {title: 'Initial', hosts: [], research_packet: {}, intro_lines: '', image_prompt: '', script: ''};
  const controller = EpisodeEditorState.init({window, form, serialize: () => state, confirm});
  return {controller, listeners, windowListeners, get state() { return state; }, set state(value) { state = value; }};
}

test('edit then load asks what will be replaced and confirmed load can reset dirty state', () => {
  let message = '';
  const ui = setup((value) => { message = value; return true; });
  ui.state = {...ui.state, title: 'Unsaved'};
  assert.equal(ui.controller.confirmEpisodeReplacement('Saved show'), true);
  assert.match(message, /replace your unsaved episode, host, research, intro, image-prompt, and script edits/i);
  ui.state = {...ui.state, title: 'Saved show'};
  ui.controller.markClean();
  assert.equal(ui.controller.isDirty(), false);
});

test('edit then leave installs a beforeunload warning', () => {
  const ui = setup();
  ui.state = {...ui.state, image_prompt: 'New artwork'};
  const event = {preventDefault() { this.prevented = true; }};
  assert.equal(ui.windowListeners.beforeunload(event), '');
  assert.equal(event.prevented, true);
  assert.equal(event.returnValue, '');
});

test('AI draft replacement confirms over a non-empty modified script', () => {
  let calls = 0;
  const ui = setup(() => { calls += 1; return true; });
  ui.state = {...ui.state, script: '[HOST] Existing words'};
  assert.equal(ui.controller.confirmScriptReplacement(), true);
  assert.equal(calls, 1);
});

test('canceled replacement keeps the editor dirty', () => {
  const ui = setup(() => false);
  ui.state = {...ui.state, script: '[HOST] Keep this'};
  assert.equal(ui.controller.confirmScriptReplacement(), false);
  assert.equal(ui.controller.isDirty(), true);
});

test('successful save snapshot resets the unload warning', () => {
  const ui = setup();
  ui.state = {...ui.state, research_packet: {episode_angle: 'Changed'}};
  ui.controller.markClean();
  const event = {preventDefault() { this.prevented = true; }};
  assert.equal(ui.windowListeners.beforeunload(event), undefined);
  assert.equal(event.prevented, undefined);
});
