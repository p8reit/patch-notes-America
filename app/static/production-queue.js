(() => {
  const status = document.getElementById('production-queue-status');
  const labels = {audio: 'Audio', speech: 'Speech preview', image: 'Image', video: 'Video', planner: 'Local planning', encoder: 'Video encoding'};
  const describe = job => `${labels[job.kind] || 'Production'}${job.id === 'request' ? '' : ` · ${job.id}`}`;
  let lastText = '';

  async function refresh() {
    try {
      if (document.hidden) return;
      const response = await fetch('/api/production-queue');
      if (!response.ok) throw new Error('Queue status unavailable. Retrying…');
      const queue = await response.json();
      const lines = [queue.active ? `Running: ${describe(queue.active)}` : 'No job running.'];
      for (const job of queue.waiting) lines.push(`Waiting #${job.position}: ${describe(job)}`);
      if (!queue.active && !queue.waiting.length) lines[0] = 'Queue empty. Ready for your next job.';
      const text = lines.join('\n');
      if (text !== lastText) {
        const rows = lines.map(line => {
          const row = document.createElement('p');
          row.textContent = line;
          return row;
        });
        status.replaceChildren(...rows);
        lastText = text;
      }
    } catch (error) {
      status.textContent = 'Queue status unavailable. Retrying…';
      lastText = '';
    } finally {
      window.setTimeout(refresh, 2000);
    }
  }
  refresh();
})();
