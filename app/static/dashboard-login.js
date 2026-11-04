'use strict';
const setup = window.location.pathname === '/setup';
let setupToken = setup ? window.location.hash.slice(1) : '';
if (setup) window.history.replaceState(null, '', window.location.pathname);
const form = document.getElementById('login-form');
const message = document.getElementById('message');
const submit = document.getElementById('submit');
if (setup) {
  document.title = 'Set up · DDoJA';
  document.getElementById('heading').textContent = 'Make it yours.';
  document.getElementById('intro').textContent = 'Choose the username and password for your dashboard. You only need to do this once.';
  document.getElementById('password').autocomplete = 'new-password';
  document.getElementById('password').minLength = 15;
  document.getElementById('password-help').hidden = false;
  submit.textContent = 'Create account';
  if (!/^[A-Za-z0-9_-]{43}$/.test(setupToken)) {
    form.hidden = true;
    message.textContent = 'Open the private setup link to create your account.';
  }
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  submit.disabled = true;
  message.textContent = '';
  try {
    const data = {username: document.getElementById('username').value,
      password: document.getElementById('password').value};
    if (setup) data.setup_token = setupToken;
    const response = await fetch(setup ? '/auth/setup' : '/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Sign-in failed. Please try again.');
    document.getElementById('password').value = '';
    setupToken = '';
    const status = await fetch('/auth/status', {cache: 'no-store', credentials: 'same-origin'}).then(r => r.json());
    if (!status.authenticated) throw new Error('This browser did not save the login. Open this address directly in Safari or Chrome.');
    const next = new URLSearchParams(window.location.search).get('next') || '/';
    window.location.replace(next.startsWith('/') && !next.startsWith('//') && !/[\\\r\n]/.test(next) ? next : '/');
  } catch (error) {
    message.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
