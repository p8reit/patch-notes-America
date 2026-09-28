(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.WorkflowNavigation = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function init({document, window}) {
    const nav = document.querySelector('.workflow-nav');
    if (!nav) return null;
    const links = [...nav.querySelectorAll('a[href^="#"]')];
    const stages = links.map((link) => document.querySelector(link.getAttribute('href'))).filter(Boolean);

    function setCurrent(stage) {
      links.forEach((link) => {
        const current = link.getAttribute('href') === `#${stage.id}`;
        if (current) link.setAttribute('aria-current', 'step');
        else link.removeAttribute('aria-current');
      });
    }

    function availableLinks() {
      return links.filter((link) => {
        const stage = document.querySelector(link.getAttribute('href'));
        return stage && !stage.hidden;
      });
    }

    nav.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
      const available = availableLinks();
      const active = available.indexOf(document.activeElement);
      if (active < 0) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0
        : event.key === 'End' ? available.length - 1
          : (active + (['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1) + available.length) % available.length;
      available[next].focus();
    });

    document.getElementById('episode-form')?.addEventListener('invalid', (event) => {
      const field = event.target;
      field.closest('details')?.setAttribute('open', '');
      const stage = field.closest('[data-workflow-stage]');
      if (stage) setCurrent(stage);
      window.setTimeout(() => field.focus(), 0);
    }, true);

    const observer = typeof window.IntersectionObserver === 'function'
      ? new window.IntersectionObserver((entries) => {
        const visible = entries.filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible) setCurrent(visible.target);
      }, {rootMargin: '-20% 0px -65% 0px', threshold: [0, 0.25, 0.5, 1]})
      : null;
    stages.forEach((stage) => observer?.observe(stage));

    return {setCurrent, availableLinks, observer};
  }

  return {init};
}));
