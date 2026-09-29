(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.Accessibility = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  function setStatus(element, message, options = {}) {
    const isError = Boolean(options.error);
    element.setAttribute('role', isError ? 'alert' : 'status');
    element.setAttribute('aria-live', isError ? 'assertive' : 'polite');
    element.textContent = message;
  }

  function describedBy(control) {
    return (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
  }

  function showFieldError(control, errorElement, message) {
    const ids = describedBy(control).filter((id) => id !== errorElement.id);
    if (message) ids.push(errorElement.id);
    if (ids.length) control.setAttribute('aria-describedby', ids.join(' '));
    else control.removeAttribute('aria-describedby');
    control.setAttribute('aria-invalid', message ? 'true' : 'false');
    errorElement.textContent = message || '';
    errorElement.hidden = !message;
  }

  function focusFirstInvalid(controls) {
    const first = Array.from(controls).find((control) =>
      control.getAttribute('aria-invalid') === 'true' || (control.checkValidity && !control.checkValidity()));
    if (!first) return null;
    first.focus();
    return first;
  }

  function setDisabledReason(button, reasonElement, reason) {
    button.disabled = Boolean(reason);
    reasonElement.textContent = reason || '';
    reasonElement.hidden = !reason;
    if (reason) button.setAttribute('aria-describedby', reasonElement.id);
    else button.removeAttribute('aria-describedby');
  }

  return {setStatus, showFieldError, focusFirstInvalid, setDisabledReason};
});
