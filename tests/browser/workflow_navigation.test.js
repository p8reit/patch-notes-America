const test = require('node:test');
const assert = require('node:assert/strict');
const WorkflowNavigation = require('../../app/static/workflow-navigation.js');

function fixture() {
  const listeners = {};
  const attributes = new Map();
  const stages = Array.from({length: 6}, (_, index) => ({id: `stage-${index + 1}`, hidden: index === 5}));
  const links = stages.map((stage, index) => ({
    stage,
    focused: false,
    getAttribute(name) { return name === 'href' ? `#${stage.id}` : attributes.get(`${index}:${name}`); },
    setAttribute(name, value) { attributes.set(`${index}:${name}`, value); },
    removeAttribute(name) { attributes.delete(`${index}:${name}`); },
    focus() { links.forEach((link) => { link.focused = false; }); this.focused = true; document.activeElement = this; },
  }));
  const formListeners = {};
  const nav = {
    querySelectorAll: () => links,
    addEventListener: (name, callback) => { listeners[name] = callback; },
  };
  const form = {addEventListener: (name, callback) => { formListeners[name] = callback; }};
  const document = {
    activeElement: null,
    querySelector(selector) {
      if (selector === '.workflow-nav') return nav;
      return stages.find((stage) => `#${stage.id}` === selector);
    },
    getElementById: (id) => id === 'episode-form' ? form : null,
  };
  const observed = [];
  class IntersectionObserver {
    constructor(callback) { this.callback = callback; }
    observe(stage) { observed.push(stage); }
  }
  const window = {IntersectionObserver, setTimeout: (callback) => callback()};
  const controller = WorkflowNavigation.init({document, window});
  return {attributes, controller, document, formListeners, links, listeners, observed, stages};
}

test('arrow keys move focus through available workflow stages', () => {
  const ui = fixture();
  ui.links[0].focus();
  let prevented = false;
  ui.listeners.keydown({key: 'ArrowRight', preventDefault: () => { prevented = true; }});
  assert.equal(ui.document.activeElement, ui.links[1]);
  assert.equal(prevented, true);

  ui.links[4].focus();
  ui.listeners.keydown({key: 'ArrowRight', preventDefault() {}});
  assert.equal(ui.document.activeElement, ui.links[0], 'hidden distribution is skipped');
});

test('intersection updates the current-stage indication', () => {
  const ui = fixture();
  ui.controller.observer.callback([{target: ui.stages[2], isIntersecting: true, intersectionRatio: .8}]);
  assert.equal(ui.attributes.get('2:aria-current'), 'step');
  assert.equal(ui.attributes.has('0:aria-current'), false);
  assert.equal(ui.observed.length, 6);
});

test('invalid controls reveal their disclosure, select their stage, and receive focus', () => {
  const ui = fixture();
  let opened = false;
  const field = {
    closest(selector) {
      if (selector === 'details') return {setAttribute(name) { opened = name === 'open'; }};
      if (selector === '[data-workflow-stage]') return ui.stages[3];
      return null;
    },
    focus() { this.focused = true; },
  };
  ui.formListeners.invalid({target: field});
  assert.equal(opened, true);
  assert.equal(field.focused, true);
  assert.equal(ui.attributes.get('3:aria-current'), 'step');
});

test('collapsing a disclosure does not replace or clear its controls', () => {
  const control = {value: 'Keep my settings'};
  const details = {open: true, control};
  details.open = false;
  details.open = true;
  assert.equal(details.control, control);
  assert.equal(details.control.value, 'Keep my settings');
});
