(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.EpisodeDescription = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const IMPORT_NOTES_PREFIX = 'Imported production and episode details:\n';

  function descriptionFromEpisode(episode) {
    const notes = episode.story_notes;
    if (typeof notes !== 'string' || !notes.startsWith(IMPORT_NOTES_PREFIX)) return '';
    try {
      const imported = JSON.parse(notes.slice(IMPORT_NOTES_PREFIX.length));
      return typeof imported?.episode?.description === 'string' ? imported.episode.description : '';
    } catch {
      return '';
    }
  }

  function init({document, reportStatus, writeClipboard}) {
    const description = document.getElementById('episode-description');
    const copyButton = document.getElementById('copy-episode-description');
    const status = document.getElementById('episode-description-status');
    const notes = document.getElementById('story-notes');
    let revision = 0;

    function show(episode) {
      revision += 1;
      description.value = descriptionFromEpisode(episode);
      copyButton.disabled = !description.value.trim();
      reportStatus(status, copyButton.disabled ? 'No episode description available.' : 'Description ready to copy for publishing.');
    }

    copyButton.addEventListener('click', async () => {
      const value = description.value;
      if (!value.trim()) return;
      const currentRevision = revision;
      copyButton.disabled = true;
      reportStatus(status, 'Copying description…');
      try {
        if (!writeClipboard) throw new Error('Automatic clipboard access is unavailable.');
        await writeClipboard(value);
        if (currentRevision === revision) reportStatus(status, 'Description copied. Paste it into your publishing platform.');
      } catch {
        if (currentRevision === revision) {
          description.focus();
          description.select();
          reportStatus(status, 'Select and copy the highlighted description with Ctrl+C or Cmd+C.', true);
        }
      } finally {
        if (currentRevision === revision) copyButton.disabled = !description.value.trim();
      }
    });

    notes.addEventListener('input', () => show({story_notes: notes.value}));
    show({story_notes: notes.value});
    return {show};
  }

  return {descriptionFromEpisode, init};
}));
