(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.EpisodeEditorState = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function init({window, form, serialize, confirm}) {
    let cleanSnapshot = JSON.stringify(serialize());
    let cleanScript = String(serialize().script || '');

    function isDirty() {
      return JSON.stringify(serialize()) !== cleanSnapshot;
    }

    function markClean() {
      const state = serialize();
      cleanSnapshot = JSON.stringify(state);
      cleanScript = String(state.script || '');
    }

    function confirmEpisodeReplacement(title) {
      if (!isDirty()) return true;
      return confirm(`Load “${title || 'the selected episode'}” and replace your unsaved episode, host, research, intro, image-prompt, and script edits?`);
    }

    function confirmScriptReplacement() {
      const state = serialize();
      const script = String(state.script || '').trim();
      if (!script || String(state.script || '') === cleanScript) return true;
      return confirm('Replace the non-empty, modified episode script with a new AI draft? Other editor fields will be kept.');
    }

    function warnBeforeUnload(event) {
      if (!isDirty()) return undefined;
      event.preventDefault();
      event.returnValue = '';
      return '';
    }

    form.addEventListener('input', isDirty);
    form.addEventListener('change', isDirty);
    window.addEventListener('beforeunload', warnBeforeUnload);

    return {isDirty, markClean, confirmEpisodeReplacement, confirmScriptReplacement, warnBeforeUnload};
  }

  return {init};
}));
