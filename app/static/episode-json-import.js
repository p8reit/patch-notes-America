(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.EpisodeJsonImport = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const MAX_SIZE = 2 * 1024 * 1024;
  const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

  function text(object, key, required = false) {
    const value = object[key];
    if (value === undefined && !required) return '';
    if (typeof value !== 'string' || (required && !value.trim())) {
      throw new Error(`${key} must be ${required ? 'a non-empty string' : 'a string'}.`);
    }
    return value;
  }

  function stringList(object, key) {
    const value = object[key];
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
      throw new Error(`${key} must be an array of strings.`);
    }
    return value;
  }

  function validateEpisode(episode) {
    if (!isObject(episode)) throw new Error('Each episode must be an object.');
    ['title', 'host', 'transcript'].forEach((key) => text(episode, key, true));
    ['format', 'theme', 'cold_open', 'description', 'closing'].forEach((key) => text(episode, key));
    if (episode.estimated_runtime_minutes !== undefined && (
      typeof episode.estimated_runtime_minutes !== 'number' || !Number.isFinite(episode.estimated_runtime_minutes)
      || episode.estimated_runtime_minutes < 2 || episode.estimated_runtime_minutes > 60
    )) throw new Error('estimated_runtime_minutes must be a number between 2 and 60.');
    if (episode.promotional_creative !== undefined && !isObject(episode.promotional_creative)) {
      throw new Error('promotional_creative must be an object.');
    }
    const creative = episode.promotional_creative || {};
    if (text(creative, 'image_prompt').length > 2000) throw new Error('image_prompt must be 2000 characters or fewer.');
    text(creative, 'clip_prompt');
    if (episode.fact_check !== undefined && !isObject(episode.fact_check)) throw new Error('fact_check must be an object.');
    ['confirmed', 'uncertain_or_developing', 'editorial_cautions'].forEach((key) => stringList(episode.fact_check || {}, key));
    if (episode.sources !== undefined) {
      if (!Array.isArray(episode.sources)) throw new Error('sources must be an array.');
      episode.sources.forEach((source) => {
        if (typeof source === 'string') return;
        if (!isObject(source)) throw new Error('Each source must be a string or an object.');
        ['title', 'publisher', 'date', 'url', 'type'].forEach((key) => {
          if (source[key] !== null) text(source, key);
        });
      });
    }
  }

  function parse(raw) {
    if (raw.length > MAX_SIZE) throw new Error('JSON must be 2 MB or smaller.');
    let document;
    try { document = JSON.parse(raw.replace(/^\uFEFF/, '')); }
    catch (error) { throw new Error(`Invalid or incomplete JSON. Check commas, quotes, and closing brackets. ${error.message}`); }
    if (!isObject(document) && !Array.isArray(document)) throw new Error('JSON must be an episode object, an episodes array, or a production document with an episodes array.');
    if (!Array.isArray(document) && document.schema_version !== undefined && document.schema_version !== '1.0') {
      throw new Error('Unsupported schema_version. Use "1.0".');
    }
    // Resolve the collection before validating individual titles. A production
    // document does not need an episode title of its own.
    const episodes = Array.isArray(document) ? document : document.episodes === undefined ? [document] : document.episodes;
    if (!Array.isArray(episodes) || !episodes.length) throw new Error('episodes must be a non-empty array.');
    episodes.forEach((episode, index) => {
      try { validateEpisode(episode); }
      catch (error) { throw new Error(`Episode ${index + 1}: ${error.message}`); }
    });
    const shared = Array.isArray(document) || document.episodes === undefined ? {} : Object.fromEntries(
      Object.entries(document).filter(([key]) => key !== 'episodes'),
    );
    text(shared, 'editorial_direction');
    return {shared, episodes};
  }

  function toEditor(parsed, index, hosts) {
    if (!Number.isInteger(index) || index < 0) throw new Error('Choose an episode to import.');
    const episode = parsed.episodes[index];
    if (!episode) throw new Error('Choose an episode to import.');
    const name = episode.host.trim().toLowerCase();
    const matches = hosts.filter((host) => host.name.trim().toLowerCase() === name);
    if (matches.length !== 1) {
      throw new Error(matches.length
        ? `More than one host is named “${episode.host}”. Give the profiles unique names in Cast, then import again.`
        : `No host profile matches “${episode.host}”. Add or rename a host in Cast and configure its voice, then import again.`);
    }
    const facts = episode.fact_check || {};
    const metadata = Object.fromEntries(Object.entries(episode).filter(([key]) => key !== 'transcript'));
    const coldOpen = (episode.cold_open || '').trim();
    const transcript = episode.transcript;
    const spokenText = transcript.trimStart();
    // Only remove an exact, complete opening passage. Do not remove similar
    // words or an occurrence later in the script.
    const openingIsRepeated = Boolean(coldOpen && spokenText.startsWith(coldOpen)
      && (spokenText.length === coldOpen.length || /^\s/.test(spokenText.slice(coldOpen.length))));
    const script = openingIsRepeated ? spokenText.slice(coldOpen.length).trimStart() : transcript;
    // Keep the original source available when splitting it into editor fields.
    if (openingIsRepeated) metadata.transcript = transcript;
    return {
      title: episode.title.trim(),
      hosts: [JSON.parse(JSON.stringify(matches[0]))],
      script,
      research_packet: {
        episode_angle: episode.theme || episode.description || '',
        stories: [{
          headline: episode.title.trim(), importance: 'medium',
          verified_facts: (facts.confirmed || []).join('\n'),
          disputed_or_uncertain: (facts.uncertain_or_developing || []).join('\n'),
          angles: (facts.editorial_cautions || []).join('\n'),
          sources: (episode.sources || []).map((source) => typeof source === 'string' ? source
            : ['title', 'publisher', 'date', 'url', 'type'].map((key) => source[key]).filter(Boolean).join(' | ')),
        }],
      },
      // Retain all production/episode metadata in an existing editable field.
      // Preserve the original transcript in notes when its opening was split out.
      story_notes: `Imported production and episode details:\n${JSON.stringify({production: parsed.shared, episode: metadata}, null, 2)}`,
      target_minutes: Math.round(episode.estimated_runtime_minutes ?? 8),
      conversation_tone: parsed.shared.editorial_direction || 'smart, funny, conversational, politically non-tribal',
      image_prompt: (episode.promotional_creative || {}).image_prompt || '',
      intro_lines: coldOpen,
      intro_overlap: true, intro_music_volume: 0.25,
      performance: {version: 1, enabled: false, beats: []},
    };
  }

  function init({document, reportStatus, getHosts, confirmReplacement, replaceEpisode}) {
    const input = document.getElementById('episode-json-input');
    const fileInput = document.getElementById('episode-json-file');
    const readButton = document.getElementById('read-episode-json');
    const importButton = document.getElementById('import-episode-json');
    const select = document.getElementById('episode-json-select');
    const status = document.getElementById('episode-json-status');
    let parsed = null;
    let parsedRaw = '';
    let revision = 0;

    function clearSelection(message) {
      parsed = null;
      parsedRaw = '';
      select.replaceChildren();
      select.disabled = true;
      importButton.disabled = true;
      reportStatus(status, message);
    }

    input.addEventListener('input', () => {
      revision += 1;
      readButton.disabled = false;
      clearSelection('JSON changed. Choose Read JSON to update the episode list.');
    });
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files[0];
      const currentRevision = ++revision;
      clearSelection(file ? 'Reading JSON file…' : 'Paste JSON or choose a file, then choose Read JSON.');
      readButton.disabled = Boolean(file);
      if (!file) return;
      try {
        if (file.size > MAX_SIZE) throw new Error('JSON file must be 2 MB or smaller.');
        const content = await file.text();
        if (currentRevision !== revision) return;
        input.value = content;
        reportStatus(status, 'File ready. Choose Read JSON to list its episodes.');
      } catch (error) {
        if (currentRevision === revision) reportStatus(status, `Could not read file: ${error.message}`, true);
      } finally {
        // An older read must not enable the button during a newer file read.
        if (currentRevision === revision || !fileInput.files.length) readButton.disabled = false;
      }
    });
    function readJson() {
      clearSelection('Checking JSON…');
      try {
        parsed = parse(input.value);
        parsedRaw = input.value;
        if (parsed.episodes.length > 1) {
          const placeholder = document.createElement('option');
          placeholder.value = '';
          placeholder.textContent = `Choose one of ${parsed.episodes.length} episodes…`;
          select.appendChild(placeholder);
        }
        parsed.episodes.forEach((episode, index) => {
          const option = document.createElement('option');
          option.value = String(index);
          option.textContent = `${index + 1}. ${episode.title} · ${episode.host}${episode.format ? ` · ${episode.format}` : ''}`;
          select.appendChild(option);
        });
        select.value = parsed.episodes.length === 1 ? '0' : '';
        select.disabled = false;
        importButton.disabled = !select.value;
        reportStatus(status, `Found ${parsed.episodes.length} episode${parsed.episodes.length === 1 ? '' : 's'}. Choose one, then import it for review.`);
      } catch (error) {
        reportStatus(status, error.message, true);
        input.focus();
      }
    }
    readButton.addEventListener('click', readJson);
    select.addEventListener('change', () => {
      importButton.disabled = !parsed || select.value === '';
      if (!importButton.disabled) {
        const episode = parsed.episodes[Number(select.value)];
        reportStatus(status, `Selected “${episode.title}”. Import this episode to edit its script and cold open. Other episodes remain available in the list.`);
      }
    });
    importButton.addEventListener('click', () => {
      if (!parsed) return;
      if (input.value !== parsedRaw) { readJson(); return; }
      if (select.value === '') {
        reportStatus(status, 'Choose an episode from the list before importing.', true);
        select.focus();
        return;
      }
      try {
        const episode = toEditor(parsed, Number(select.value), getHosts());
        if (!confirmReplacement(episode.title)) {
          reportStatus(status, 'Import canceled; your current episode was kept.');
          return;
        }
        replaceEpisode(episode);
        reportStatus(status, `Imported “${episode.title}” as a new, unsaved episode. Review the cast, research, script, and cold open, then save. Other episodes remain available in the list.`);
      } catch (error) { reportStatus(status, error.message, true); }
    });
  }

  return {parse, toEditor, init};
}));
