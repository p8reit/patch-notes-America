(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ResearchPackets = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function init(options) {
    const {
      document, fetch, confirm, FormData, serializePacket, readJsonResponse, replacePacket,
    } = options;
    const select = document.getElementById('saved-packet-select');
    const loadButton = document.getElementById('load-packet');
    const saveButton = document.getElementById('save-packet');
    const status = document.getElementById('packet-status');
    let savedSnapshot = JSON.stringify(serializePacket());

    function setOptions(packets, selectedId) {
      select.replaceChildren();
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = packets.length ? 'Choose a saved packet…' : 'No saved packets';
      select.appendChild(placeholder);
      packets.forEach((packet) => {
        const option = document.createElement('option');
        option.value = packet.id;
        option.textContent = `${packet.title} · ${packet.stories} ${packet.stories === 1 ? 'story' : 'stories'}`;
        select.appendChild(option);
      });
      select.value = packets.some((packet) => packet.id === selectedId) ? selectedId : '';
      select.disabled = !packets.length;
      loadButton.disabled = !select.value;
    }

    async function refresh(selectedId = select.value, announce = true) {
      select.disabled = true;
      loadButton.disabled = true;
      if (announce) status.textContent = 'Loading saved packets…';
      try {
        const response = await fetch('/api/research-packets');
        const data = await readJsonResponse(response, 'Could not list saved packets');
        const packets = Array.isArray(data.packets) ? data.packets : [];
        setOptions(packets, selectedId);
        if (announce) {
          status.textContent = packets.length
            ? `Found ${packets.length} saved packet${packets.length === 1 ? '' : 's'}.`
            : 'No saved research packets yet.';
        }
        return packets;
      } catch (error) {
        select.disabled = !select.children.length || select.children.length === 1;
        loadButton.disabled = !select.value;
        status.textContent = error.message;
        throw error;
      }
    }

    select.addEventListener('change', () => {
      loadButton.disabled = !select.value;
      if (select.value) status.textContent = 'Saved packet selected. Choose Load to replace the editor fields.';
    });

    loadButton.addEventListener('click', async () => {
      const packetId = select.value;
      if (!packetId) {
        status.textContent = 'Choose a saved research packet first.';
        return;
      }
      const hasUnsavedEdits = JSON.stringify(serializePacket()) !== savedSnapshot;
      if (hasUnsavedEdits && !confirm('Replace the unsaved research-packet edits with the selected saved packet?')) {
        status.textContent = 'Load canceled; your unsaved packet edits were kept.';
        return;
      }
      loadButton.disabled = true;
      status.textContent = 'Loading saved research packet…';
      try {
        const response = await fetch(`/api/research-packets/${encodeURIComponent(packetId)}`);
        const data = await readJsonResponse(response, 'Could not load saved packet');
        replacePacket(data.packet);
        savedSnapshot = JSON.stringify(serializePacket());
        select.value = data.id;
        status.textContent = `Loaded “${data.packet.title || data.id}”.`;
      } catch (error) {
        status.textContent = error.message;
      } finally {
        loadButton.disabled = !select.value;
      }
    });

    saveButton.addEventListener('click', async () => {
      saveButton.disabled = true;
      status.textContent = 'Saving research packet…';
      try {
        const form = new FormData();
        form.append('packet_json', JSON.stringify(serializePacket()));
        const response = await fetch('/api/research-packets', {method: 'POST', body: form});
        const data = await readJsonResponse(response, 'Could not save packet');
        savedSnapshot = JSON.stringify(data.packet);
        const savedOption = document.createElement('option');
        savedOption.value = data.id;
        savedOption.textContent = `${data.packet.title || data.id} · ${data.packet.stories.length} ${data.packet.stories.length === 1 ? 'story' : 'stories'}`;
        select.appendChild(savedOption);
        select.value = data.id;
        await refresh(data.id, false);
        status.textContent = `Saved ${data.packet.stories.length} stories as ${data.id}.`;
      } catch (error) {
        status.textContent = error.message;
      } finally {
        saveButton.disabled = false;
      }
    });

    const ready = refresh().catch(() => {});
    return {refresh, ready};
  }

  return {init};
}));
