const test = require('node:test');
const assert = require('node:assert/strict');
const Accessibility = require('../../app/static/accessibility.js');

class Element {
  constructor(id = '') { this.id = id; this.attrs = {}; this.hidden = false; this.textContent = ''; this.disabled = false; this.valid = true; this.focused = false; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] || null; }
  removeAttribute(name) { delete this.attrs[name]; }
  checkValidity() { return this.valid; }
  focus() { this.focused = true; }
}

for (const journey of ['save', 'draft', 'voice preview', 'render submission', 'job failure', 'clip rendering']) {
  test(`${journey} progress is polite and its actionable error is assertive`, () => {
    const status = new Element(`${journey}-status`);
    Accessibility.setStatus(status, `${journey} in progress`);
    assert.equal(status.getAttribute('role'), 'status');
    assert.equal(status.getAttribute('aria-live'), 'polite');
    Accessibility.setStatus(status, `${journey} failed`, {error: true});
    assert.equal(status.getAttribute('role'), 'alert');
    assert.equal(status.getAttribute('aria-live'), 'assertive');
  });
}

test('field errors have stable descriptions and keyboard focus moves to the first invalid control', () => {
  const first = new Element('episode-title');
  const second = new Element('episode-script');
  const error = new Element('episode-title-error');
  Accessibility.showFieldError(first, error, 'Enter an episode title.');
  second.valid = false;
  assert.equal(first.getAttribute('aria-describedby'), 'episode-title-error');
  assert.equal(first.getAttribute('aria-invalid'), 'true');
  assert.equal(Accessibility.focusFirstInvalid([first, second]), first);
  assert.equal(first.focused, true);
});

test('a disabled action exposes its reason as adjacent described text', () => {
  const button = new Element('generate-episode');
  const reason = new Element('progress');
  Accessibility.setDisabledReason(button, reason, 'Audio service unavailable.');
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute('aria-describedby'), 'progress');
  assert.equal(reason.hidden, false);
});
