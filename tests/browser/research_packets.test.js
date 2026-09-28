const test = require('node:test');
const assert = require('node:assert/strict');
const ResearchPackets = require('../../app/static/research-packets.js');

class Element {
  constructor() {
    this.value = '';
    this.textContent = '';
    this.disabled = false;
    this.children = [];
    this.listeners = {};
  }

  addEventListener(name, callback) { this.listeners[name] = callback; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  async trigger(name) { return this.listeners[name]?.({currentTarget: this}); }
}

function response(body, ok = true, status = 200) {
  return {ok, status, text: async () => JSON.stringify(body)};
}

async function readJsonResponse(res, fallback) {
  const data = JSON.parse(await res.text());
  if (!res.ok) throw new Error(data.detail || fallback);
  return data;
}

function setup(fetch, confirm = () => true) {
  const elements = Object.fromEntries([
    'saved-packet-select', 'load-packet', 'save-packet', 'packet-status',
  ].map((id) => [id, new Element()]));
  const document = {
    getElementById: (id) => elements[id],
    createElement: () => new Element(),
  };
  let editor = {title: 'Working title', episode_angle: '', stories: [{headline: '', verified_facts: ''}]};
  const controller = ResearchPackets.init({
    document,
    fetch,
    confirm,
    FormData,
    serializePacket: () => structuredClone(editor),
    readJsonResponse,
    replacePacket: (packet) => { editor = {...structuredClone(packet), title: editor.title}; },
  });
  return {elements, controller, get editor() { return editor; }, set editor(value) { editor = value; }};
}

test('lists saved packets and enables loading after selection', async () => {
  const ui = setup(async () => response({packets: [{id: 'packet-1', title: 'Packet One', stories: 2}]}));
  await ui.controller.ready;
  assert.equal(ui.elements['saved-packet-select'].children[1].textContent, 'Packet One · 2 stories');
  ui.elements['saved-packet-select'].value = 'packet-1';
  await ui.elements['saved-packet-select'].trigger('change');
  assert.equal(ui.elements['load-packet'].disabled, false);
  assert.match(ui.elements['packet-status'].textContent, /selected/i);
});

test('reports an empty saved-packet list', async () => {
  const ui = setup(async () => response({packets: []}));
  await ui.controller.ready;
  assert.equal(ui.elements['saved-packet-select'].disabled, true);
  assert.equal(ui.elements['packet-status'].textContent, 'No saved research packets yet.');
});

test('reports list request failures in the live status', async () => {
  const ui = setup(async () => response({detail: 'Packet storage unavailable'}, false, 503));
  await ui.controller.ready;
  assert.equal(ui.elements['packet-status'].textContent, 'Packet storage unavailable');
});

test('saves, refreshes, and retains the newly selected packet', async () => {
  let call = 0;
  const ui = setup(async (_url, options) => {
    call += 1;
    if (options?.method === 'POST') {
      return response({id: 'new-packet', packet: {title: 'Working title', episode_angle: '', stories: [{headline: 'News', verified_facts: 'Fact'}]}});
    }
    return response({packets: call > 1 ? [{id: 'new-packet', title: 'Working title', stories: 1}] : []});
  });
  ui.editor = {title: 'Working title', episode_angle: '', stories: [{headline: 'News', verified_facts: 'Fact'}]};
  await ui.elements['save-packet'].trigger('click');
  assert.equal(ui.elements['saved-packet-select'].value, 'new-packet');
  assert.match(ui.elements['packet-status'].textContent, /^Saved 1 stories/);
});

test('warns before loading over edits and restores angle and stories when confirmed', async () => {
  let confirmations = 0;
  const packet = {title: 'Saved title', episode_angle: 'Saved angle', stories: [{headline: 'Saved story', verified_facts: 'Saved fact'}]};
  const ui = setup(async (url) => url.endsWith('/saved-packet')
    ? response({id: 'saved-packet', packet})
    : response({packets: [{id: 'saved-packet', title: 'Saved title', stories: 1}]}), () => { confirmations += 1; return true; });
  await ui.controller.ready;
  ui.elements['saved-packet-select'].value = 'saved-packet';
  await ui.elements['saved-packet-select'].trigger('change');
  ui.editor.episode_angle = 'Unsaved angle';
  await ui.elements['load-packet'].trigger('click');
  assert.equal(confirmations, 1);
  assert.equal(ui.editor.episode_angle, 'Saved angle');
  assert.equal(ui.editor.stories[0].headline, 'Saved story');
  assert.equal(ui.elements['saved-packet-select'].value, 'saved-packet');
  assert.match(ui.elements['packet-status'].textContent, /^Loaded/);
});

test('keeps unsaved edits when replacement is canceled', async () => {
  const ui = setup(async () => response({packets: [{id: 'saved-packet', title: 'Saved title', stories: 1}]}), () => false);
  await ui.controller.ready;
  ui.elements['saved-packet-select'].value = 'saved-packet';
  ui.editor.episode_angle = 'Keep this';
  await ui.elements['load-packet'].trigger('click');
  assert.equal(ui.editor.episode_angle, 'Keep this');
  assert.match(ui.elements['packet-status'].textContent, /canceled/i);
});

test('reports a packet load request failure without clearing the selection', async () => {
  const ui = setup(async (url) => url.endsWith('/broken')
    ? response({detail: 'Stored research packet is malformed'}, false, 422)
    : response({packets: [{id: 'broken', title: 'Broken', stories: 1}]}));
  await ui.controller.ready;
  ui.elements['saved-packet-select'].value = 'broken';
  await ui.elements['load-packet'].trigger('click');
  assert.equal(ui.elements['saved-packet-select'].value, 'broken');
  assert.equal(ui.elements['packet-status'].textContent, 'Stored research packet is malformed');
});
