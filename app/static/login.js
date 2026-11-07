(() => {
  'use strict';
  try { document.documentElement.dataset.theme = localStorage.getItem('kiwm-ui-theme') === 'dark' ? 'dark' : 'light'; } catch {}
  const toggle = document.querySelector('[data-reveal-password]');
  toggle?.addEventListener('click', () => {
    const field = document.querySelector('#password');
    const visible = field.type === 'password';
    field.type = visible ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(visible));
    toggle.setAttribute('aria-label', visible ? 'Passwort verbergen' : 'Passwort anzeigen');
  });
  document.querySelector('[data-login-form]')?.addEventListener('submit', event => {
    if (!event.target.checkValidity()) return;
    const button = event.target.querySelector('[type=submit]');
    button.disabled = true;
    button.querySelector('span').textContent = 'Anmelden …';
  });
})();
