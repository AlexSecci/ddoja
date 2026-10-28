// Settings are loaded on navigation, never by the dashboard's polling loop.
let workerSettings = null;
let settingsDirty = false;
let settingsBusy = false;
const settingsNode = id => document.getElementById(id);

function settingsMessage(message) { settingsNode('settings-status').textContent = message; }
function settingsSelect(label, values, value) {
  const field = document.createElement('label'); field.className = 'field';
  const text = document.createElement('span'); text.textContent = label;
  const select = document.createElement('select');
  select.setAttribute('aria-label', label);
  values.forEach(v => { const option = document.createElement('option'); option.value = v.id; option.textContent = v.name || v.id; select.append(option); });
  select.value = values.some(v => v.id === value) ? value : values[0]?.id || '';
  field.append(text, select); return { field, select };
}
function renderWorkerSettings() {
  const container = settingsNode('worker-settings-roles'); container.replaceChildren();
  Object.entries(workerSettings.roles).forEach(([role, value]) => {
    const section = document.createElement('fieldset'); section.className = 'worker-settings-row'; section.dataset.role = role;
    const legend = document.createElement('legend'); legend.textContent = workerSettings.role_labels[role]; section.append(legend);
    const grid = document.createElement('div'); grid.className = 'form-grid'; section.append(grid);
    const provider = settingsSelect('Provider', workerSettings.providers, value.provider); provider.select.name = 'provider'; grid.append(provider.field);
    function dependentFields(modelId, effortId, backendId) {
      while (grid.children.length > 1) grid.lastChild.remove();
      const p = workerSettings.providers.find(p => p.id === provider.select.value);
      const model = settingsSelect('Model', p.models, modelId); model.select.name = 'model'; grid.append(model.field);
      const effort = settingsSelect('Reasoning effort', p.models.find(m => m.id === model.select.value).efforts.map(id => ({ id })), effortId);
      effort.select.name = 'effort'; grid.append(effort.field);
      const backends = role === 'materials' ? ['none'] : p.browser_backends;
      const browser = settingsSelect('Browser', backends.map(id => ({ id, name: ({ none: 'No browser', cua: 'Chrome / native CUA', playwright: 'Playwright (separate profile)' })[id] })), backendId || p.default_browser_backend);
      browser.select.name = 'browser_backend'; grid.append(browser.field);
      model.select.addEventListener('change', () => {
        const levels = p.models.find(m => m.id === model.select.value).efforts;
        const previous = effort.select.value; effort.select.replaceChildren();
        levels.forEach(id => { const o = document.createElement('option'); o.value = id; o.textContent = id; effort.select.append(o); });
        effort.select.value = levels.includes(previous) ? previous : levels.includes('high') ? 'high' : levels[0];
      });
    }
    dependentFields(value.model, value.effort, value.browser_backend);
    provider.select.addEventListener('change', () => dependentFields(null, 'high', null));
    container.append(section);
  });
  const credentials = workerSettings.credentials.deepseek;
  settingsNode('deepseek-key-status').textContent = credentials.configured ? 'Configured · ' + (credentials.source === 'environment' ? 'service environment' : 'encrypted local storage') : credentials.error || 'No DeepSeek API key is configured.';
  settingsNode('deepseek-key-remove').disabled = credentials.source !== 'encrypted_local';
  const pw = workerSettings.playwright;
  settingsNode('playwright-readiness').textContent = pw.ready ? 'Installed and ready. No model call was made.' : 'Not ready. Install from the project folder: ' + pw.setup_command;
  settingsNode('settings-save').disabled = false;
}
async function loadWorkerSettings({ discardDraft = false } = {}) {
  if (settingsBusy || (settingsDirty && !discardDraft)) return;
  settingsBusy = true;
  try {
    workerSettings = await api('settings/workers'); settingsDirty = false; renderWorkerSettings();
    try {
      const broker = await api('settings/gmail-broker');
      settingsNode('gmail-broker-status').textContent = broker.configured ? 'Configured · Gmail read-only OAuth. Matching email tokens are retrieved in the background.' : 'Not configured · scoped retrieval through the Codex supervisor remains available.';
    } catch { settingsNode('gmail-broker-status').textContent = 'Connection status unavailable. Worker settings remain available.'; }
    settingsMessage('Saved revision ' + workerSettings.revision + '.');
  } catch (error) { settingsMessage(error.message); }
  finally { settingsBusy = false; }
}
settingsNode('worker-settings-form').addEventListener('change', () => { settingsDirty = true; settingsMessage('Unsaved changes. Running workers keep their current settings.'); });
settingsNode('settings-reload').addEventListener('click', () => loadWorkerSettings({ discardDraft: true }));
settingsNode('worker-settings-form').addEventListener('submit', async event => {
  event.preventDefault(); if (settingsBusy || !workerSettings) return;
  const roles = {};
  settingsNode('worker-settings-roles').querySelectorAll('fieldset[data-role]').forEach(row => {
    roles[row.dataset.role] = Object.fromEntries(Array.from(row.querySelectorAll('select')).map(s => [s.name, s.value]));
  });
  settingsBusy = true; settingsNode('settings-save').disabled = true;
  settingsNode('worker-settings-roles').querySelectorAll('fieldset').forEach(row => { row.disabled = true; });
  try {
    workerSettings = await api('settings/workers', { method: 'PUT', body: JSON.stringify({ revision: workerSettings.revision, roles }) });
    settingsDirty = false; renderWorkerSettings(); settingsMessage('Saved. New invocations use these choices; campaigns remain in their current state.');
  } catch (error) { settingsMessage(error.message); }
  finally {
    settingsBusy = false; settingsNode('settings-save').disabled = false;
    settingsNode('worker-settings-roles').querySelectorAll('fieldset').forEach(row => { row.disabled = false; });
  }
});
settingsNode('deepseek-key-form').addEventListener('submit', async event => {
  event.preventDefault(); const input = settingsNode('deepseek-api-key'); const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const status = await api('settings/providers/deepseek/key', { method: 'PUT', body: JSON.stringify({ api_key: input.value }) });
    settingsNode('deepseek-key-status').textContent = status.configured ? 'Configured · encrypted local storage' : 'Not configured';
    settingsNode('deepseek-key-remove').disabled = !status.configured;
  } catch (error) { settingsNode('deepseek-key-status').textContent = error.message; }
  finally { input.value = ''; button.disabled = false; }
});
settingsNode('deepseek-key-remove').addEventListener('click', async () => {
  try {
    const status = await api('settings/providers/deepseek/key', { method: 'DELETE' });
    settingsNode('deepseek-key-status').textContent = status.configured ? 'Saved key removed. A service environment key remains configured.' : 'Saved key removed.';
    settingsNode('deepseek-key-remove').disabled = true;
  } catch (error) { settingsNode('deepseek-key-status').textContent = error.message; }
});
