(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ImageCreationProgress = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function create({panel, elapsed, log, now = () => performance.now(),
    schedule = setInterval, cancel = clearInterval}) {
    let started = 0;
    let timer = null;
    let waiting = false;
    function seconds() { return Math.max(0, Math.floor((now() - started) / 1000)); }
    function tick() {
      elapsed.textContent = `${seconds()}s elapsed`;
    }
    function record(message) {
      const entry = log.ownerDocument.createElement('li');
      entry.textContent = `+${seconds()}s — ${message}`;
      log.appendChild(entry);
    }
    function finish(message) {
      if (timer !== null) cancel(timer);
      timer = null;
      tick();
      record(message);
    }
    return {
      start({aspect, promptLength}) {
        if (timer !== null) cancel(timer);
        started = now();
        waiting = true;
        log.replaceChildren();
        panel.hidden = false;
        tick();
        record(`Submitting ${aspect} image request (${promptLength} prompt characters).`);
        record('Waiting for the server and image provider. Internal generation progress is unavailable.');
        let slowNotice = false;
        timer = schedule(() => {
          tick();
          if (waiting && !slowNotice && seconds() >= 60) {
            slowNotice = true;
            record('Still waiting for a response. Generation can take several minutes; elapsed time does not indicate completion percentage.');
          }
        }, 1000);
      },
      response(status) {
        waiting = false;
        record(`Server responded: HTTP ${status}.`);
      },
      record,
      finish,
    };
  }
  return {create};
}));
