let token = '';
let pipelines = [];
let jobs = [];
let jobPage = 0;
const jobsPerPage = 20;
let technologies = [];
let companies = [];
let legacyQuestions = [];
let summaries = {};
let pipelineJobs = new Map();
let pipelineInboxes = new Map();
let workerRecords = new Map();
let expandedPipelines = new Set();
let expandedLegacy = new Set();
let expandedWorkers = new Set();
let expandedFinishedWorkers = new Set();
let showHiddenCampaigns = false;
let inboxDrafts = new Map();
const batchSelections = new Map();
let batchSelectionNotice = '';
const inFlightActions = new Set();
let currentJobDetail = null;
let serviceHealth = null;
let pipelineCurrent = false;
let refreshPromise = null;
let dataRevision = 0;
let refreshSequence = 0;
let pollTimer = null;
let browserPollTimer = null;
let browserRefreshPromise = null;
let currentView = 'pipelines';
let efficiencyMetrics = null;
let efficiencyMetricsError = null;
const runtimeStatuses = new Map();
const runtimeStatusErrors = new Map();
const expandedRuntime = new Set();
const panelRequestIds = new Map();

const $ = id => document.getElementById(id);
const element = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
};
const withRenderKey = (node, key) => {
  node.dataset.renderKey = String(key);
  return node;
};
const bind = (node, event, handler) => {
  // Setting `open` while building an unattached replacement <details> queues
  // a toggle event too. It must not fetch or render the live tree recursively.
  node['on' + event] = event === 'toggle'
    ? currentEvent => { if (currentEvent.currentTarget.isConnected) return handler(currentEvent); }
    : handler;
  return node;
};
const button = (label, cls, action) => {
  const node = element('button', label, cls);
  node.type = 'button';
  if (action) bind(node, 'click', action);
  return node;
};
const externalLink = (label, rawUrl) => {
  const node = element('a', label);
  try {
    const url = new URL(String(rawUrl || ''));
    if (url.protocol === 'https:') {
      node.href = url.href;
      node.target = '_blank';
      node.rel = 'noreferrer';
    } else node.removeAttribute('href');
  } catch (_) { node.removeAttribute('href'); }
  return node;
};
const count = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const humanize = value => String(value || 'unknown').replaceAll('_', ' ').replaceAll('-', ' ');
const segment = value => encodeURIComponent(String(value));
const dateText = value => {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
};
const objectValue = value => {
  if (value && typeof value === 'object') return value;
  if (typeof value === 'string') { try { return JSON.parse(value); } catch (_) {} }
  return {};
};

function nodeKey(node) {
  return node && node.nodeType === 1 && node.dataset && node.dataset.renderKey
    ? node.dataset.renderKey : null;
}
function compatibleNode(left, right) {
  return left && right && left.nodeType === right.nodeType
    && (left.nodeType !== 1 || left.nodeName === right.nodeName);
}
function captureControlState(node) {
  if (!node || node.nodeType !== 1) return null;
  const tag = node.nodeName;
  if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') return null;
  const focused = node === document.activeElement;
  if (tag === 'INPUT' && node.type === 'file') {
    return { preserve: true, file: true, focused };
  }
  const dirty = tag === 'SELECT'
    ? Array.from(node.options || []).some(option => option.selected !== option.defaultSelected)
    : node.value !== node.defaultValue || (tag === 'INPUT' && node.checked !== node.defaultChecked);
  const selected = tag === 'SELECT'
    ? Array.from(node.options || []).filter(option => option.selected).map(option => option.value)
    : null;
  return {
    preserve: node.dataset.batchSelection !== 'true' && (focused || dirty),
    focused,
    composing: Boolean(node.__pipelineComposing),
    value: node.value,
    checked: tag === 'INPUT' ? node.checked : undefined,
    selected,
    selectionStart: typeof node.selectionStart === 'number' ? node.selectionStart : null,
    selectionEnd: typeof node.selectionEnd === 'number' ? node.selectionEnd : null,
    selectionDirection: node.selectionDirection || 'none',
  };
}
function syncAttributes(current, next) {
  const tag = current.nodeName;
  const preserveOpen = tag === 'DETAILS' ? current.open : null;
  const skipped = new Set(tag === 'INPUT' ? ['value', 'checked']
    : tag === 'OPTION' ? ['selected']
      : tag === 'DETAILS' ? ['open'] : []);
  Array.from(current.attributes || []).forEach(attribute => {
    if (!next.hasAttribute(attribute.name) && !skipped.has(attribute.name)) current.removeAttribute(attribute.name);
  });
  Array.from(next.attributes || []).forEach(attribute => {
    if (!skipped.has(attribute.name) && current.getAttribute(attribute.name) !== attribute.value) {
      current.setAttribute(attribute.name, attribute.value);
    }
  });
  if (tag === 'DETAILS') current.open = preserveOpen;
  for (const event of ['click', 'submit', 'input', 'change', 'keydown', 'toggle']) {
    current['on' + event] = next['on' + event] || null;
  }
}
function patchNode(current, next) {
  if (!compatibleNode(current, next)) return next;
  if (current.nodeType === 3) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return current;
  }
  const control = captureControlState(current);
  syncAttributes(current, next);
  reconcileChildren(current, Array.from(next.childNodes || []));
  if (control && !control.file) {
    const tag = current.nodeName;
    if (!control.preserve) {
      if (tag !== 'SELECT') current.defaultValue = next.defaultValue;
      if (tag === 'INPUT') current.defaultChecked = next.defaultChecked;
      if (tag === 'SELECT') {
        const selected = new Set(Array.from(next.options || []).filter(option => option.selected).map(option => option.value));
        Array.from(current.options || []).forEach(option => { option.selected = selected.has(option.value); });
      } else {
        current.value = next.value;
        if (tag === 'INPUT') current.checked = next.checked;
      }
    } else if (tag === 'SELECT') {
      const selected = new Set(control.selected || []);
      Array.from(current.options || []).forEach(option => { option.selected = selected.has(option.value); });
    } else {
      if (current.value !== control.value) current.value = control.value;
      if (tag === 'INPUT' && current.checked !== control.checked) current.checked = control.checked;
    }
    if (control.focused && !control.composing && control.selectionStart !== null
        && typeof current.setSelectionRange === 'function') {
      try { current.setSelectionRange(control.selectionStart, control.selectionEnd, control.selectionDirection); } catch (_) {}
    }
  }
  return current;
}
function reconcileChildren(parent, desiredChildren) {
  const previous = Array.from(parent.childNodes || []);
  const desired = Array.from(desiredChildren || []);
  const previousByKey = new Map(previous.map(node => [nodeKey(node), node]).filter(([key]) => key));
  const desiredKeys = new Set(desired.map(nodeKey).filter(Boolean));
  const focusedInside = document.activeElement && parent.contains(document.activeElement);
  let order = desired;
  if (focusedInside) {
    // Keep existing keyed cards in place while someone types or composes text.
    // New cards wait at the end until focus leaves the live panel.
    const retained = previous.map(node => nodeKey(node)).filter(key => key && desiredKeys.has(key));
    const byKey = new Map(desired.filter(nodeKey).map(node => [nodeKey(node), node]));
    const retainedSet = new Set(retained);
    order = retained.map(key => byKey.get(key)).concat(desired.filter(node => !nodeKey(node) || !retainedSet.has(nodeKey(node))));
  }
  const used = new Set();
  const unkeyed = previous.filter(node => !nodeKey(node));
  let unkeyedCursor = 0;
  order.forEach((next, index) => {
    const key = nodeKey(next);
    let current = key ? previousByKey.get(key) : null;
    if (!current && !key) {
      while (unkeyedCursor < unkeyed.length && (used.has(unkeyed[unkeyedCursor])
          || !compatibleNode(unkeyed[unkeyedCursor], next))) unkeyedCursor += 1;
      current = unkeyed[unkeyedCursor];
      unkeyedCursor += 1;
    }
    if (current && !used.has(current) && compatibleNode(current, next)) {
      current = patchNode(current, next);
      used.add(current);
    } else current = next;
    const reference = parent.childNodes[index] || null;
    if (current !== reference) parent.insertBefore(current, reference);
  });
  previous.forEach(node => { if (!used.has(node) && node.parentNode === parent) parent.removeChild(node); });
  return parent;
}

function preserveViewport(parent, action) {
  const x = window.scrollX, y = window.scrollY;
  const active = document.activeElement;
  const activeInside = active && parent && parent.contains(active);
  const visibleAnchor = activeInside ? active : Array.from(parent?.children || []).find(node => {
    if (typeof node.getBoundingClientRect !== 'function') return false;
    const rect = node.getBoundingClientRect();
    return rect.bottom >= 0 && rect.top <= window.innerHeight;
  });
  const anchorTop = visibleAnchor && typeof visibleAnchor.getBoundingClientRect === 'function'
    ? visibleAnchor.getBoundingClientRect().top : null;
  action();
  if (visibleAnchor && visibleAnchor.isConnected && anchorTop !== null
      && typeof visibleAnchor.getBoundingClientRect === 'function') {
    const shift = visibleAnchor.getBoundingClientRect().top - anchorTop;
    if (Math.abs(shift) > 1) window.scrollTo(x, y + shift);
  } else if (Math.abs(window.scrollY - y) > 1 || Math.abs(window.scrollX - x) > 1) {
    window.scrollTo(x, y);
  }
}

function actionKey(kind, id) { return String(kind) + ':' + String(id); }
function actionInFlight(key) { return inFlightActions.has(String(key)); }
function beginAction(key) {
  const normalized = String(key);
  if (inFlightActions.has(normalized)) return false;
  inFlightActions.add(normalized);
  return true;
}
function endAction(key) { inFlightActions.delete(String(key)); }
function setFormButtonsBusy(form, busy) {
  if (!form || typeof form.querySelectorAll !== 'function') return;
  form.querySelectorAll('button').forEach(control => { control.disabled = Boolean(busy); });
}
function refreshActionPanel(panel) {
  if (panel === 'inbox' && currentView === 'inbox') renderInbox();
  else if (panel === 'pipelines' && currentView === 'pipelines') renderPipelines();
}

document.addEventListener('compositionstart', event => { if (event.target) event.target.__pipelineComposing = true; }, true);
document.addEventListener('compositionend', event => { if (event.target) event.target.__pipelineComposing = false; }, true);
const pipelineOf = view => view && view.pipeline ? view.pipeline : (view || {});
const countsOf = view => view && view.counts ? view.counts : (pipelineOf(view).counts || {});
async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') dataRevision += 1;
  const headers = {
    Accept: 'application/json',
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    'X-Pipeline-Token': token,
    ...options.headers,
  };
  const response = await fetch('/api/' + path, { ...options, method, headers });
  if (options.expectBlob) {
    if (!response.ok) throw new Error((await response.text()) || response.statusText);
    return response.blob();
  }
  const raw = await response.text();
  let result = {};
  if (raw) { try { result = JSON.parse(raw); } catch (_) { result = { detail: raw }; } }
  if (!response.ok) throw new Error(result.detail || response.statusText || 'The request failed.');
  return result;
}
function setStatus(message) { $('status').textContent = message; }
function completionOf(view) {
  const p = pipelineOf(view), c = countsOf(view), discovery = p.discovery || {};
  const selectedPresent = c.selected !== undefined && c.selected !== null;
  const resolved = count(c.submitted) + count(c.discarded);
  return {
    selected: count(c.selected),
    resolved,
    discoveryDone: discovery.status === 'complete',
    complete: p.status === 'completed' && discovery.status === 'complete' && selectedPresent && resolved === count(c.selected),
  };
}
function statusLabel(view) {
  const p = pipelineOf(view), completion = completionOf(view);
  if (p.status === 'completed') return completion.complete ? 'Complete' : 'Completion needs reconciliation';
  return ({
    queued: 'Queued',
    running: 'Running',
    stopping: 'Stopping',
    stopped: 'Stopped',
    blocked: 'Blocked',
    awaiting_user: 'Waiting at a job checkpoint',
    quota_paused: 'Paused by account quota',
    interrupted: 'Interrupted',
    failed: 'Needs attention',
  })[p.status] || humanize(p.status);
}
function stageLabel(value) {
  return ({
    discovery: 'Company discovery',
    evaluation: 'Job evaluation',
    evaluator: 'Job evaluation',
    submission: 'Application preparation',
    document: 'Requested document child',
    waiting_child: 'Waiting for requested document work',
    waiting_question: 'Waiting for a job-specific answer',
    waiting_approval: 'Waiting for a job-specific decision',
    quota_paused: 'Paused by account quota',
    uncertain: 'Submission outcome needs reconciliation',
    interrupted: 'Saved checkpoint needs recovery',
    completed: 'Work finished',
  })[value] || humanize(value || 'No active stage');
}
function discoveryCoverageSummary(coverage) {
  if (typeof coverage === 'string') return coverage.trim();
  if (!coverage || typeof coverage !== 'object' || Array.isArray(coverage)) return '';
  return Object.entries(coverage).map(([key, value]) => {
    if (value === null || value === undefined || value === '') return '';
    let rendered;
    if (Array.isArray(value)) {
      rendered = value.length
        ? value.map(item => typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item)).join(', ')
        : '';
    } else if (typeof value === 'object') {
      rendered = Object.keys(value).length ? JSON.stringify(value) : '';
    } else {
      rendered = String(value);
    }
    if (!rendered) return '';
    const label = humanize(key);
    return label.charAt(0).toUpperCase() + label.slice(1) + ': ' + rendered;
  }).filter(Boolean).join(' · ');
}
function discoveryContinuationSummary(continuation, pipelineStatus) {
  if (!continuation || !['queued', 'interrupted'].includes(continuation.state)) return '';
  let summary;
  if (continuation.state === 'interrupted') {
    summary = 'Discovery continuation is interrupted and requires explicit resume.';
  } else if (pipelineStatus === 'running') {
    summary = 'Discovery is continuing in the same session.';
  } else if (pipelineStatus === 'queued') {
    summary = 'Discovery continuation is queued in the same session.';
  } else {
    summary = 'A same-session discovery continuation is saved while the pipeline is '
      + humanize(pipelineStatus || 'not running') + '.';
  }
  const parts = [summary];
  if (continuation.resume_mode) parts.push('Resume mode: ' + humanize(continuation.resume_mode) + '.');
  if (continuation.reason) parts.push(String(continuation.reason));
  return parts.join(' ');
}
function limitsText(limits = {}) {
  return (limits.discovery_per_company ?? 1) + ' discovery per company · '
    + (limits.evaluators_per_pipeline ?? 3) + ' evaluators and '
    + (limits.submissions_per_pipeline ?? 3) + ' submission agents per pipeline · up to '
    + (limits.document_children_per_parent ?? limits.documents_per_parent ?? 1) + ' document child per parent (outside the submission cap)';
}

function renderHealth() {
  pipelineCurrent = serviceHealth && serviceHealth.pipeline_release === 'agentic_pipeline_v1';
  $('service-state').classList.toggle('connected', Boolean(pipelineCurrent));
  $('service-label').textContent = pipelineCurrent ? 'Service online' : 'Check connection';
  $('integration-notice').textContent = pipelineCurrent
    ? 'Agent pipeline controls are connected. Agents interpret each listing and form; review their job-specific questions and decisions in the inbox.'
    : 'This local service is an older dashboard release. Refresh or restart it to use the agentic pipeline controls.';
  $('pipeline-submit').disabled = !pipelineCurrent;
  if ($('upload-policy-summary')) $('upload-policy-summary').textContent = serviceHealth?.routine_document_uploads?.enabled
    ? 'Routine résumé and cover-letter uploads to this company’s approved job/application sites are preauthorized.'
    : 'Document uploads require approval unless standing authorization is enabled.';
  $('pipeline-refresh').disabled = !pipelineCurrent;
  $('inbox-refresh').disabled = !pipelineCurrent;
  $('agent-limits').textContent = pipelineCurrent
    ? limitsText(serviceHealth.agent_limits || {})
    : 'Agent worker limits are unavailable on this service release.';
}
function renderBrowserStatus(report) {
  $('connection-label').textContent = report?.verified ? 'Local practice verified' : 'Local practice not verified';
  const scopeNote = 'The fictional local practice check is separate from employer workflows; employer access and submission evidence are reported per pipeline.';
  if (!report || typeof report !== 'object') {
    $('browser-check-status').textContent = 'Local practice status is unavailable. ' + scopeNote;
    return;
  }
  const check = report.check || {};
  const detail = String(report.detail || check.error || '').replace(/Employer workflows remain untested\.?/gi, '').trim();
  let text = report.verified ? 'The fictional local browser practice is verified.'
    : 'Local browser control is not verified (' + humanize(report.state || 'unknown') + ').';
  if (check.status === 'running') text = 'The fictional local browser practice check is running.';
  if (check.status === 'failed') text = 'The fictional local browser practice check did not verify browser control.';
  if (check.status === 'completed') text = report.verified
    ? 'The fictional local browser practice completed successfully.'
    : 'The local check finished, but browser control is not verified.';
  $('browser-check-status').textContent = (text + ' ' + detail + ' ' + scopeNote).trim();
}
async function refreshBrowserStatus() {
  if (browserRefreshPromise) return browserRefreshPromise;
  browserRefreshPromise = (async () => {
    try {
      const report = await api('browser/status');
      renderBrowserStatus(report);
      if (report.check && report.check.status === 'running' && !browserPollTimer) {
        browserPollTimer = setInterval(() => refreshBrowserStatus(), 3500);
      } else if ((!report.check || report.check.status !== 'running') && browserPollTimer) {
        clearInterval(browserPollTimer);
        browserPollTimer = null;
      }
    } catch (_) {
      renderBrowserStatus(null);
      if (browserPollTimer) clearInterval(browserPollTimer);
      browserPollTimer = null;
    }
  })();
  try { await browserRefreshPromise; }
  finally { browserRefreshPromise = null; }
}
function metric(label, value) {
  const box = element('div', undefined, 'pipeline-metric');
  box.append(element('strong', count(value)), element('span', label));
  return box;
}
function collectArtifacts(value, found = [], depth = 0) {
  if (depth > 5 || value === null || value === undefined) return found;
  if (Array.isArray(value)) { value.forEach(item => collectArtifacts(item, found, depth + 1)); return found; }
  if (typeof value !== 'object') return found;
  ['artifacts', 'ready_artifacts', 'review_artifacts'].forEach(key => {
    if (Array.isArray(value[key])) value[key].forEach(item => {
      if (item && item.id && !found.some(old => old.id === item.id)) found.push(item);
    });
  });
  Object.entries(value).forEach(([key, child]) => {
    if (!['artifacts', 'ready_artifacts', 'review_artifacts'].includes(key)) collectArtifacts(child, found, depth + 1);
  });
  return found;
}
function artifactDownloadButton(artifactId, variant, filename, sha256, label, index = null) {
  const row = withRenderKey(element('div', undefined, 'artifact-row'), 'artifact:' + artifactId + ':' + variant + ':' + (index ?? ''));
  const digest = sha256 ? ' · SHA-256 ' + sha256 : '';
  row.append(element('span', (filename || 'Document') + digest));
  row.append(button(label, 'small-button', async event => {
    event.currentTarget.disabled = true;
    try {
      const query = 'variant=' + encodeURIComponent(variant)
        + (index === null ? '' : '&index=' + encodeURIComponent(index));
      const blob = await api('agent-artifacts/' + segment(artifactId) + '?' + query, { expectBlob: true });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename || 'agent-artifact';
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) { setStatus(error.message); }
    finally { event.currentTarget.disabled = false; }
  }));
  return row;
}
function artifactButton(artifact) {
  return artifactDownloadButton(artifact.id, 'markdown', artifact.filename, artifact.sha256,
    'Download Markdown source');
}
function renderReviewArtifact(artifact) {
  const section = withRenderKey(element('section', undefined, 'review-artifact'), 'review-artifact:' + artifact.id);
  section.append(element('h4', humanize(artifact.kind) + ' · ' + (artifact.upload?.filename || artifact.filename || 'Document')));
  section.append(element('p', 'Prepared source SHA-256: ' + (artifact.sha256 || 'not recorded'), 'sub'));
  if (artifact.upload) {
    section.append(element('p', 'Rendered PDF SHA-256: ' + (artifact.upload.sha256 || 'not recorded'), 'sub'));
    section.append(artifactDownloadButton(artifact.id, 'pdf', artifact.upload.filename,
      artifact.upload.sha256, 'Download exact reviewed PDF'));
  }
  section.append(artifactDownloadButton(artifact.id, 'markdown', artifact.filename,
    artifact.sha256, 'Download Markdown source'));
  (artifact.previews || []).forEach(preview => {
    section.append(artifactDownloadButton(artifact.id, 'preview', preview.filename,
      preview.sha256, 'Download rendered page preview', preview.index));
  });
  if (artifact.kind === 'cover_letter' && typeof artifact.markdown === 'string') {
    const exact = withRenderKey(element('details', undefined, 'review-document-text'), 'prepared-cover-text');
    exact.append(element('summary', 'Prepared cover-letter document text'), element('pre', artifact.markdown));
    section.append(exact);
  }
  return section;
}
function renderFormFieldSummary(context) {
  const section = withRenderKey(element('section', undefined, 'review-form-fields'), 'form-fields');
  section.append(element('h4', 'Application fields observed by the agent'));
  const fields = Array.isArray(context.form_fields) ? context.form_fields : [];
  if (!fields.length) {
    section.append(element('p', 'No structured form-field summary was included with this checkpoint.', 'worker-error'));
    return section;
  }
  const table = document.createElement('table');
  table.className = 'review-fields-table';
  const head = document.createElement('thead');
  const header = document.createElement('tr');
  ['Field', 'Value observed', 'Evidence/source'].forEach(label => header.append(element('th', label)));
  head.append(header); table.append(head);
  const body = document.createElement('tbody');
  fields.forEach(field => {
    const entry = field && typeof field === 'object' ? field : { value: field };
    const row = document.createElement('tr');
    row.append(element('th', entry.label || 'Unlabeled field'));
    row.append(element('td', typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value ?? '')));
    row.append(element('td', typeof entry.evidence === 'string' ? entry.evidence : JSON.stringify(entry.evidence ?? '')));
    body.append(row);
  });
  table.append(body); section.append(table);
  return section;
}
function workerActivityText(worker, pipeline) {
  if (pipeline.stop_requested || ['stopped', 'stopping'].includes(pipeline.status)) return 'Campaign stopped; this worker will not start.';
  if (worker.context?.browser_freshness?.pending && ['queued', 'running'].includes(worker.state)) {
    return worker.state === 'queued' ? 'Queued to refresh the employer session. No action needed from you.'
      : 'Refreshing the employer session before continuing.';
  }
  return ({
    running: 'Working now.',
    queued: 'Queued for automatic continuation. No action needed from you.',
    waiting_approval: 'Waiting for your decision in Agent inbox.',
    waiting_question: 'Waiting for your answer in Agent inbox.',
    waiting_browser: 'Waiting for shared browser or sign-in recovery.',
    waiting_child: 'Waiting for its document worker.',
    quota_paused: 'Paused by the account quota.',
    uncertain: 'Application outcome needs reconciliation before any retry.',
  })[worker.state] || stageLabel(worker.state);
}

function renderWorker(worker, pipeline) {
  const card = withRenderKey(element('article', undefined, 'worker-card'), 'worker:' + worker.id);
  const heading = withRenderKey(element('div', undefined, 'worker-card-title'), 'heading');
  heading.append(element('strong', humanize(worker.role)));
  heading.append(element('span', humanize(worker.state), 'badge status-' + String(worker.state || 'unknown').replace(/[^a-z_]/g, '')));
  card.append(heading);
  card.append(withRenderKey(element('p', workerActivityText(worker, pipeline), 'worker-activity'), 'activity'));
  if (worker.job_title || worker.job_id) card.append(element('p', worker.job_title || ('Job ' + worker.job_id), 'sub'));
  if (worker.resume_mode) card.append(element('p', 'Resume mode: ' + humanize(worker.resume_mode), 'sub'));
  const execution = worker.context?.execution;
  if (execution) card.append(element('p', `${execution.provider} · ${execution.model} · ${execution.effort} · ${execution.browser_backend}`, 'sub'));
  if (worker.context?.execution_change_deferred) card.append(element('p', 'Model change waits for reconciliation of the possibly sent application.', 'hint'));
  if (worker.updated_at) card.append(element('p', 'Updated ' + dateText(worker.updated_at), 'sub'));
  if (worker.id) card.append(element('p', 'Worker ID ' + worker.id, 'sub'));
  if (worker.session_id) {
    const session = element('p', undefined, 'sub');
    session.append(document.createTextNode('Saved session '), element('code', worker.session_id));
    card.append(session);
  }
  if (worker.checkpoint) {
    const history = withRenderKey(element('details', undefined, 'worker-checkpoint'), 'saved-checkpoint');
    history.append(element('summary', 'Saved resume point (history)'), element('code', worker.checkpoint),
      element('p', 'This is the last saved progress marker. Current activity is shown above.', 'hint'));
    card.append(history);
  }
  if (worker.error) card.append(element('p', worker.error, 'worker-error'));
  collectArtifacts(worker.context || {}).forEach(file => card.append(artifactButton(file)));
  if (worker.state === 'session_missing' && worker.resume_mode === 'not_resumable') {
    const key = actionKey('replacement', worker.id);
    const form = withRenderKey(element('form', undefined, 'replacement-session-form'), key);
    const reason = document.createElement('textarea');
    reason.name = 'replacement_reason';
    reason.required = true;
    reason.maxLength = 1000;
    reason.rows = 2;
    reason.placeholder = 'Explain why the saved agent session is definitively unavailable.';
    const provenance = document.createElement('textarea');
    provenance.name = 'provenance';
    provenance.required = true;
    provenance.maxLength = 1000;
    provenance.rows = 2;
    provenance.placeholder = 'Confirm you reviewed this session recovery request.';
    const submit = element('button', 'Start a replacement session', 'small-button');
    submit.type = 'submit';
    submit.disabled = actionInFlight(key);
    form.append(element('p', 'The runtime reported that the saved session cannot be resumed. This action records that confirmation and creates a replacement session; uncertain final sends cannot use it.', 'sub'), reason, provenance, submit);
    bind(form, 'submit', async event => {
      event.preventDefault();
      const currentForm = event.currentTarget;
      if (!beginAction(key)) return;
      const controls = currentForm.elements;
      setFormButtonsBusy(currentForm, true);
      try {
        await api('pipelines/' + segment(pipeline.id) + '/workers/' + segment(worker.id) + '/resume-replacement', {
          method: 'POST',
          body: JSON.stringify({ replacement_reason: controls.namedItem('replacement_reason').value.trim(), provenance: controls.namedItem('provenance').value.trim() }),
        });
        setStatus('Replacement session request recorded for this saved worker.');
        await refreshDashboard();
      } catch (error) { setStatus(error.message); }
      finally {
        endAction(key);
        if (currentForm.isConnected) setFormButtonsBusy(currentForm, false);
        refreshActionPanel('pipelines');
      }
    });
    card.append(form);
  }
  return card;
}

function renderPipeline(view) {
  const p = pipelineOf(view), c = countsOf(view);
  const workerPreviews = Array.isArray(view.workers) ? view.workers : [];
  const workers = expandedWorkers.has(p.id) ? (workerRecords.get(p.id) || []) : workerPreviews;
  const needsWorkerAttention = worker => worker.turn_active || !['completed', 'discarded', 'superseded', 'stopped'].includes(worker.state);
  const currentWorkers = workers.filter(needsWorkerAttention);
  const finishedWorkers = workers.filter(worker => !needsWorkerAttention(worker));
  const card = withRenderKey(element('article', undefined, 'pipeline-card'), 'pipeline:' + p.id);
  const heading = withRenderKey(element('div', undefined, 'pipeline-card-heading'), 'heading');
  const title = element('div', undefined, 'company-copy');
  title.append(element('h3', p.company_name || p.company_id || 'Company'));
  const filters = p.filters || {};
  const narrowing = [];
  if (filters.title_contains) narrowing.push('Role focus: ' + filters.title_contains);
  if (filters.location_contains) narrowing.push('Location: ' + filters.location_contains);
  const scopeSummary = p.mode === 'agentic'
    ? ['Computer-science-related roles', ...narrowing].join(' · ')
    : (narrowing.join(' · ') || 'No optional role or location narrowing');
  title.append(element('p', scopeSummary, 'sub'));
  if (p.careers_url) {
    title.append(externalLink(p.careers_url, p.careers_url));
  }
  const identity = element('div', undefined, 'company-identity');
  const companyName = String(p.company_name || p.company_id || 'Company');
  const tone = Array.from(companyName).reduce((sum, char) => sum + char.codePointAt(0), 0) % 4;
  const avatar = element('span', companyName.trim().charAt(0).toUpperCase(), 'company-avatar avatar-tone-' + tone);
  avatar.setAttribute('aria-hidden', 'true');
  identity.append(avatar, title);
  heading.append(identity, element('span', statusLabel(view), 'pipeline-status state-' + (p.status || 'unknown')));
  card.append(heading);
  const task = withRenderKey(element('div', undefined, 'pipeline-task'), 'task');
  task.append(element('strong', 'Current stage'), element('span', stageLabel(p.stage)));
  const discovery = p.discovery || {};
  if (discovery.status) task.append(element('span', 'Discovery: ' + humanize(discovery.status), 'sub'));
  card.append(task);
  const stages = withRenderKey(element('div', undefined, 'pipeline-stages'), 'stage-overview');
  stages.setAttribute('aria-label', 'Recorded stage activity');
  [
    ['01', 'Discover', count(c.discovered), 'listing found', 'listings found'],
    ['02', 'Evaluate', count(c.evaluated), 'decision recorded', 'decisions recorded'],
    ['03', 'Submit', count(c.submitted), 'verified submission', 'verified submissions'],
  ].forEach(([number, label, value, singular, plural]) => {
    const stage = element('div', undefined, 'pipeline-stage' + (value > 0 ? ' has-activity' : ''));
    const copy = element('div', undefined, 'stage-copy');
    copy.append(element('strong', label), element('small', value + ' ' + (value === 1 ? singular : plural)));
    stage.append(element('span', number, 'stage-number'), copy); stages.append(stage);
  });
  card.append(stages);
  const continuationState = discovery.continuation && discovery.continuation.state;
  if (['incomplete', 'blocked', 'failed'].includes(discovery.status)) {
    let label = 'Discovery ' + humanize(discovery.status);
    if (discovery.status === 'incomplete' && continuationState === 'queued') {
      label += p.status === 'running' ? ' — continuing'
        : p.status === 'queued' ? ' — queued to continue' : ' — continuation saved';
    }
    else if (discovery.status === 'incomplete' && continuationState === 'interrupted') label += ' — interrupted';
    const needsAttention = ['blocked', 'failed'].includes(discovery.status) || continuationState === 'interrupted';
    const note = withRenderKey(element('details', undefined, needsAttention ? 'blocker-note discovery-attention' : 'discovery-detail'), 'discovery-detail');
    note.append(element('summary', label));
    if (discovery.detail) note.append(element('p', discovery.detail));
    const coverage = discoveryCoverageSummary(discovery.coverage);
    if (coverage) note.append(element('p', 'Reported coverage: ' + coverage, 'sub'));
    const continuation = discoveryContinuationSummary(discovery.continuation, p.status);
    if (continuation) note.append(element('p', continuation, 'sub'));
    card.append(note);
  } else if (discovery.detail) {
    if (discovery.detail.length > 180) {
      const report = withRenderKey(element('details', undefined, 'discovery-detail'), 'discovery-detail');
      report.append(element('summary', 'Discovery report · ' + humanize(discovery.status)), element('p', discovery.detail));
      card.append(report);
    } else card.append(withRenderKey(element('p', discovery.detail, 'discovery-detail'), 'discovery-detail'));
  }
  if (p.adopted_from) card.append(withRenderKey(element('p', 'New agentic evaluation of historical job ' + p.adopted_from + '; previous run remains unchanged.', 'sub'), 'adopted-from'));
  const metrics = withRenderKey(element('div', undefined, 'pipeline-metrics'), 'metrics');
  [
    ['Discovered', c.discovered],
    ['Selected for evaluation', c.selected],
    ['Evaluated', c.evaluated],
    ['Agent-approved', c.accepted ?? c.approved],
    ['Discarded', c.discarded],
    ['Submitted with evidence', c.submitted],
    ['Excluded by filters', c.excluded],
  ].forEach(([label, value]) => metrics.append(metric(label, value)));
  card.append(metrics);
  const outcomes = withRenderKey(element('div', undefined, 'outcome-counts'), 'outcomes');
  [
    ['Recruiter pending', c.recruiter_pending], ['Rejected', c.recruiter_rejected],
    ['Accepted', c.recruiter_accepted], ['Unknown', c.recruiter_unknown],
    ['At a job checkpoint', c.awaiting_user], ['Uncertain', c.uncertain],
  ].forEach(([label, value]) => outcomes.append(element('span', label + ' ' + count(value))));
  card.append(outcomes);
  const completion = completionOf(view);
  if (completion.discoveryDone && completion.selected) {
    const progress = withRenderKey(element('div', undefined, 'listing-progress'), 'completion-progress');
    const label = element('div', undefined, 'listing-progress-label');
    label.append(element('span', 'Selected listings with a terminal disposition'), element('span', completion.resolved + ' of ' + completion.selected));
    const track = element('div', undefined, 'progress-track');
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-label', 'Selected listings with a terminal disposition');
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', String(completion.selected));
    track.setAttribute('aria-valuenow', String(Math.min(completion.selected, completion.resolved)));
    track.setAttribute('aria-valuetext', completion.resolved + ' of ' + completion.selected + ' selected listings resolved');
    const fill = element('span', undefined, 'progress-fill');
    fill.style.width = Math.min(100, Math.round(completion.resolved / completion.selected * 100)) + '%';
    track.append(fill); progress.append(label, track); card.append(progress);
  } else if (completion.discoveryDone && completion.selected === 0) {
    card.append(withRenderKey(element('p', 'Discovery is complete; no listings were selected for evaluation.', 'completion-empty'), 'completion-empty'));
  }
  const quota = view.quota || p.quota || {};
  if (quota.paused && p.status === 'quota_paused') {
    const note = withRenderKey(element('div', undefined, 'blocker-note quota-warning'), 'quota-warning');
    note.append(element('strong', 'Account-wide quota pause'));
    note.append(element('p', 'Confirm the account reset in Execution & capacity before resuming. Your saved session is preserved.'));
    card.append(note);
  }
  if (Array.isArray(p.blockers) && p.blockers.length) {
    const note = withRenderKey(element('details', undefined, 'blocker-note'), 'blockers');
    note.append(element('summary', (p.status === 'blocked' ? 'Blocked' : 'Needs attention') + ' · ' + p.blockers.length + ' saved ' + (p.blockers.length === 1 ? 'note' : 'notes')));
    const list = element('ul');
    p.blockers.forEach(blocker => list.append(element('li', blocker)));
    note.append(list); card.append(note);
  }
  const runtime = withRenderKey(element('details', undefined, 'pipeline-runtime'), 'runtime-status');
  runtime.open = expandedRuntime.has(p.id);
  const runtimeSummary = withRenderKey(element('summary', 'Runtime, account, and company health'), 'summary');
  runtime.append(runtimeSummary);
  if (runtime.open) runtime.append(renderRuntimeStatus(p.id));
  bind(runtime, 'toggle', event => {
    const opened = event.currentTarget.open;
    if (opened) {
      expandedRuntime.add(p.id);
      renderPipelines();
      if (!runtimeStatuses.has(p.id) && !runtimeStatusErrors.has(p.id)) loadRuntimeStatus(p.id);
    } else {
      expandedRuntime.delete(p.id);
      runtimeStatuses.delete(p.id);
      runtimeStatusErrors.delete(p.id);
      renderPipelines();
    }
    updatePolling();
  });
  card.append(runtime);
  const detail = withRenderKey(element('details', undefined, 'pipeline-workers'), 'workers');
  detail.open = expandedWorkers.has(p.id);
  const workerCount = p.worker_count ?? workerPreviews.length;
  detail.append(element('summary', 'Worker sessions (' + currentWorkers.length + ' active or needing attention)'));
  if (detail.open) {
    const workerList = element('div', undefined, 'worker-list');
    if (!workerRecords.has(p.id)) {
      workerList.append(element('p', workerCount ? 'Loading saved worker details…' : 'No worker sessions are recorded yet.', 'empty-state'));
    } else if (!currentWorkers.length) workerList.append(element('p', 'No active workers or workers needing attention.', 'empty-state'));
    else currentWorkers.forEach(worker => workerList.append(renderWorker(worker, p)));
    if (workerRecords.has(p.id) && finishedWorkers.length) {
      const history = withRenderKey(element('details', undefined, 'finished-workers'), 'finished-workers');
      history.open = expandedFinishedWorkers.has(p.id);
      history.append(element('summary', 'Finished worker history (' + finishedWorkers.length + ')'));
      if (history.open) finishedWorkers.forEach(worker => history.append(renderWorker(worker, p)));
      bind(history, 'toggle', event => {
        if (event.currentTarget.open) expandedFinishedWorkers.add(p.id);
        else expandedFinishedWorkers.delete(p.id);
        renderPipelines();
      });
      workerList.append(history);
    }
    detail.append(workerList);
  }
  bind(detail, 'toggle', event => {
    if (event.currentTarget.open) {
      expandedWorkers.add(p.id);
      renderPipelines();
      if (!workerRecords.has(p.id)) loadPipelineWorkers(p.id);
    } else {
      expandedWorkers.delete(p.id);
      workerRecords.delete(p.id);
      renderPipelines();
    }
    updatePolling();
  });
  card.append(detail);

  const footer = withRenderKey(element('div', undefined, 'pipeline-card-footer'), 'footer');
  footer.append(element('span', p.created_at ? 'Created ' + dateText(p.created_at) : '', 'sub'));
  const controls = element('div', undefined, 'pipeline-actions');
  if (['queued', 'running', 'stopping', 'awaiting_user', 'quota_paused'].includes(p.status)) {
    const stop = button(p.status === 'stopping' ? 'Stopping…' : 'Stop this company', 'quiet-button',
      () => pipelineAction(p.id, 'stop'));
    stop.disabled = !pipelineCurrent || p.status === 'stopping'; controls.append(stop);
  } else if (['stopped', 'interrupted', 'failed', 'blocked'].includes(p.status)) {
    const resume = button('Resume this company', 'quiet-button', () => pipelineAction(p.id, 'resume'));
    resume.disabled = !pipelineCurrent || Boolean(quota.paused); controls.append(resume);
  }
  controls.append(button(expandedPipelines.has(p.id) ? 'Hide job records' : 'View job records',
    'outline-button', () => togglePipelineJobs(p.id)));
  if (p.dashboard_hidden || ['completed', 'stopped'].includes(p.status)) {
    const visibility = button(p.dashboard_hidden ? 'Restore campaign' : 'Hide campaign', 'quiet-button', () => setCampaignVisibility(p.id, !p.dashboard_hidden));
    visibility.disabled = actionInFlight(actionKey('visibility', p.id));
    controls.append(visibility);
  }
  footer.append(controls); card.append(footer);
  if (expandedPipelines.has(p.id)) card.append(renderPipelineJobs(p, view));
  return card;
}
function renderPipelineJobs(p, view) {
  const section = withRenderKey(element('section', undefined, 'pipeline-jobs'), 'pipeline-jobs:' + p.id);
  section.append(element('h4', 'Listing and per-job records'));
  const saved = pipelineJobs.get(p.id);
  if (!saved) { section.append(element('p', 'Loading saved job records…', 'empty-state')); return section; }
  if (saved.error) section.append(element('p', 'Some saved listing data could not refresh: ' + saved.error, 'worker-error'));
  const rows = saved.jobs || [];
  if (!rows.length) {
    section.append(element('p', 'No listing records yet. Discovery may still be exploring this company site.', 'empty-state'));
    return section;
  }
  const wrap = element('div', undefined, 'table-wrap'), table = element('table');
  const head = element('thead'), header = element('tr');
  ['Role and location', 'Evaluation-time decision', 'Application state', 'Checkpoint or adoption'].forEach(label => header.append(element('th', label)));
  head.append(header); table.append(head);
  const body = element('tbody');
  rows.forEach(job => {
    const row = withRenderKey(element('tr'), 'pipeline-job:' + job.id), role = element('td');
    role.append(element('strong', job.title || job.job_title || 'Untitled listing'));
    if (job.url) {
      role.append(document.createTextNode(' '), externalLink('Open listing', job.url));
    }
    role.append(element('p', [job.location, job.country, job.employment, job.arrangement].filter(Boolean).join(' · '), 'sub'));
    if (job.selected === false) role.append(element('p', 'Excluded by this pipeline’s filters: ' + (job.excluded_reason || 'not selected'), 'sub'));
    const decision = element('td');
    decision.append(element('span', humanize(job.agent_decision || job.evaluation_status || job.phase || 'pending'), 'badge'));
    if (job.agent_reason || job.reason) {
      decision.append(element('p', 'Evaluation-time rationale only; this is not a current submission authorization. ' + (job.agent_reason || job.reason), 'sub'));
    }
    const application = element('td');
    application.append(element('span', humanize(job.application_status || job.run_status || 'not submitted'), 'badge'));
    if (job.application_provenance) application.append(element('p', job.application_provenance, 'sub'));
    const action = element('td');
    if (job.run_status === 'submission_uncertain' || job.submission_uncertain) {
      action.append(element('p', 'The final action may have happened. Reconcile saved session evidence; do not retry it.', 'worker-error'));
    }
    const profileRefresh = job.profile_refresh || {};
    if (p.mode === 'agentic' && job.selected === true && profileRefresh.eligible === true) {
      const key = actionKey('profile-refresh', job.id);
      const form = withRenderKey(element('form', undefined, 'inline-provenance-form'), key);
      const provenance = document.createElement('textarea');
      provenance.name = 'provenance';
      provenance.required = true; provenance.rows = 2; provenance.maxLength = 1000;
      provenance.dataset.renderKey = 'provenance';
      provenance.dataset.focusKey = 'profile-refresh:' + job.id;
      provenance.placeholder = 'Confirm the factual profile update that should be used for this one-job re-evaluation.';
      const submit = element('button', 'Re-evaluate with updated profile', 'small-button');
      submit.type = 'submit';
      submit.disabled = actionInFlight(key);
      form.append(provenance, submit);
      bind(form, 'submit', async event => {
        event.preventDefault();
        const currentForm = event.currentTarget;
        const note = currentForm.elements.namedItem('provenance').value.trim();
        if (!note) return;
      if (!beginAction(key)) return;
        setFormButtonsBusy(currentForm, true);
        try {
          const result = await api('pipelines/' + segment(p.id) + '/jobs/' + segment(job.id) + '/refresh-profile', {
            method: 'POST', body: JSON.stringify({ provenance: note }),
          });
          setStatus(result.profile_refresh?.already_current
            ? 'This job is already evaluated against the current profile version.'
            : 'A fresh evaluation was queued for this job using the current profile version.');
          await loadPipelineJobs(p.id);
          await refreshDashboard();
          renderPipelines();
        } catch (error) {
          setStatus(error.message);
        } finally {
          endAction(key);
          if (currentForm.isConnected) setFormButtonsBusy(currentForm, false);
          refreshActionPanel('pipelines');
        }
      });
      action.append(form);
    }
    if (p.mode === 'legacy' && job.adoption && job.adoption.eligible) {
      const key = actionKey('adopt', job.id);
      const form = withRenderKey(element('form', undefined, 'inline-provenance-form'), key);
      const provenance = document.createElement('textarea'); provenance.required = true; provenance.rows = 2;
      provenance.name = 'provenance';
      provenance.dataset.renderKey = 'provenance';
      provenance.dataset.focusKey = 'adopt:' + job.id;
      provenance.placeholder = 'Confirm you want a new agent to re-evaluate this saved job.';
      const submit = element('button', 'Adopt and re-evaluate', 'small-button'); submit.type = 'submit';
      submit.disabled = actionInFlight(key);
      form.append(provenance, submit);
      bind(form, 'submit', async event => {
        event.preventDefault();
        const currentForm = event.currentTarget;
        if (!beginAction(key)) return;
        const currentProvenance = currentForm.elements.namedItem('provenance').value.trim();
        setFormButtonsBusy(currentForm, true);
        try {
          await api('pipelines/' + segment(p.id) + '/jobs/' + segment(job.id) + '/adopt', {
            method: 'POST', body: JSON.stringify({ provenance: currentProvenance }),
          });
          setStatus('A new agentic run was created. The legacy record remains unchanged.');
          await refreshDashboard();
        } catch (error) { setStatus(error.message); }
        finally {
          endAction(key);
          if (currentForm.isConnected) setFormButtonsBusy(currentForm, false);
          refreshActionPanel('pipelines');
        }
      });
      action.append(form);
    } else if (p.mode === 'legacy' && job.adoption && job.adoption.reason) {
      action.append(element('p', 'Adoption unavailable: ' + job.adoption.reason, 'sub'));
    }
    row.append(role, decision, application, action); body.append(row);
  });
  table.append(body); wrap.append(table); section.append(wrap); return section;
}
function displayMetric(value, formatter = value => String(value)) {
  return value === undefined || value === null ? 'Unavailable' : formatter(value);
}
function renderRuntimeStatus(pipelineId) {
  const section = withRenderKey(element('div', undefined, 'runtime-status-content'), 'runtime-content');
  const report = runtimeStatuses.get(pipelineId);
  const error = runtimeStatusErrors.get(pipelineId);
  if (error) section.append(element('p', 'Runtime status could not be loaded: ' + error, 'worker-error'));
  if (!report) {
    section.append(element('p', error ? 'Refresh this panel to try again.' : 'Loading runtime status…', 'sub'));
    return section;
  }
  const host = report.execution_host;
  const hostText = host && typeof host === 'object'
    ? [host.mode, host.status, host.ready === true ? 'ready' : host.ready === false ? 'not ready' : null, host.detail]
      .filter(value => value !== undefined && value !== null && value !== '').join(' · ') || 'Host details unavailable'
    : 'Execution-host readiness unavailable';
  section.append(element('p', 'Execution host: ' + hostText, 'sub'));
  const quota = report.quota;
  section.append(element('p', 'Account quota: ' + (quota && typeof quota.paused === 'boolean'
    ? (quota.paused ? 'paused' : 'not paused') : 'unavailable')
    + (quota?.reason ? ' · ' + quota.reason : ''), 'sub'));
  const gate = report.company_gate;
  const gateText = !gate ? 'unavailable' : typeof gate === 'string' ? gate
    : gate.open ? 'sign-in in progress; protected applications are waiting' : 'no sign-in wait';
  section.append(element('p', 'Company account: ' + gateText + (gate?.reason ? ' · ' + gate.reason : ''), 'sub'));
  const openCircuits = Array.isArray(report.circuits) ? report.circuits.filter(value => value.open) : null;
  const circuitText = openCircuits === null ? 'unavailable' : openCircuits.length
    ? 'paused for recovery' + openCircuits.map(value => value.reason ? ' · ' + value.reason : '').join('')
    : 'no shared browser failure reported';
  section.append(element('p', 'Browser connection: ' + circuitText, 'sub'));
  const workers = report.workers || {};
  const workerCounts = ['active', 'waiting', 'queued', 'failed', 'uncertain']
    .map(key => humanize(key) + ' ' + displayMetric(workers[key])).join(' · ');
  section.append(element('p', 'Workers: ' + workerCounts, 'sub'));
  const caps = report.caps || {};
  section.append(element('p', 'Per-pipeline caps: ' + ['discovery', 'evaluator', 'submission']
    .map(key => humanize(key) + ' ' + displayMetric(caps[key])).join(' · '), 'sub'));
  const actions = element('div', undefined, 'runtime-status-actions');
  actions.append(button('Refresh runtime status', 'small-button', () => loadRuntimeStatus(pipelineId)));
  section.append(actions);
  return section;
}
async function loadRuntimeStatus(pipelineId) {
  const key = 'runtime:' + pipelineId;
  const requestId = (panelRequestIds.get(key) || 0) + 1;
  panelRequestIds.set(key, requestId);
  const revision = dataRevision;
  runtimeStatusErrors.delete(pipelineId);
  try {
    const report = await api('pipelines/' + segment(pipelineId) + '/runtime-status');
    if (panelRequestIds.get(key) !== requestId || dataRevision !== revision
        || currentView !== 'pipelines' || !expandedRuntime.has(pipelineId)) return;
    runtimeStatuses.set(pipelineId, report);
    runtimeStatusErrors.delete(pipelineId);
  } catch (error) {
    if (panelRequestIds.get(key) !== requestId || dataRevision !== revision
        || currentView !== 'pipelines' || !expandedRuntime.has(pipelineId)) return;
    runtimeStatusErrors.set(pipelineId, error.message);
  }
  if (currentView === 'pipelines') renderPipelines();
}
async function loadPipelineJobs(id) {
  const key = 'jobs:' + id, requestId = (panelRequestIds.get(key) || 0) + 1;
  panelRequestIds.set(key, requestId);
  const revision = dataRevision;
  try {
    const result = await api('pipelines/' + segment(id) + '/jobs');
    if (panelRequestIds.get(key) !== requestId || revision !== dataRevision
        || (!expandedPipelines.has(id) && !expandedLegacy.has(id))) return;
    pipelineJobs.set(id, result);
  } catch (error) {
    if (panelRequestIds.get(key) === requestId && revision === dataRevision
        && (expandedPipelines.has(id) || expandedLegacy.has(id))) {
      const prior = pipelineJobs.get(id) || { jobs: [] };
      pipelineJobs.set(id, { ...prior, error: error.message });
    }
  }
}
async function loadPipelineWorkers(id) {
  const key = 'workers:' + id, requestId = (panelRequestIds.get(key) || 0) + 1;
  panelRequestIds.set(key, requestId);
  const revision = dataRevision;
  try {
    const result = await api('pipelines/' + segment(id) + '/workers');
    if (panelRequestIds.get(key) !== requestId || revision !== dataRevision || !expandedWorkers.has(id)) return;
    workerRecords.set(id, result.workers || []);
  } catch (error) {
    if (panelRequestIds.get(key) === requestId && revision === dataRevision) setStatus(error.message);
  }
  if (currentView === 'pipelines') renderPipelines();
}
async function togglePipelineJobs(id) {
  if (expandedPipelines.has(id)) { expandedPipelines.delete(id); pipelineJobs.delete(id); }
  else { expandedPipelines.add(id); if (!pipelineJobs.has(id)) await loadPipelineJobs(id); }
  renderPipelines();
}
async function pipelineAction(id, action) {
  try {
    await api('pipelines/' + segment(id) + '/' + action, { method: 'POST', body: '{}' });
    setStatus(action === 'stop' ? 'Stop requested for this company pipeline.' : 'Resume requested for this company pipeline.');
    await refreshDashboard();
  } catch (error) { setStatus(error.message); }
}
async function postJobAction(pipelineId, jobId, action, body) {
  try {
    await api('pipelines/' + segment(pipelineId) + '/jobs/' + segment(jobId) + '/' + action, {
      method: 'POST', body: JSON.stringify(body),
    });
    setStatus(action === 'discard' ? 'Listing discarded for this pipeline.' : 'Saved checkpoint acknowledgment recorded.');
    await loadPipelineJobs(pipelineId); await refreshDashboard(); renderPipelines();
  } catch (error) { setStatus(error.message); }
}

function isOpen(item) {
  return !['answered', 'approved', 'rejected', 'resolved', 'superseded', 'completed', 'discarded', 'stale'].includes(String(item.state || '').toLowerCase());
}
function renderInboxItem(item, pipeline) {
  const card = withRenderKey(element('article', undefined, 'inbox-card'), 'inbox:' + item.id);
  card.id = 'inbox-request-' + item.id;
  card.dataset.inboxId = item.id;
  const heading = withRenderKey(element('div', undefined, 'inbox-card-heading'), 'heading');
  heading.append(element('strong', item.job_title || 'Job checkpoint'));
  heading.append(element('span', humanize(item.kind) + ' · ' + humanize(item.state), 'badge'));
  card.append(heading);
  if (item.url || item.job_url) {
    const listingUrl = item.url || item.job_url;
    card.append(withRenderKey(externalLink('Open job listing', listingUrl), 'job-link'));
  }
  if (item.action) card.append(withRenderKey(element('p', 'Requested action: ' + humanize(item.action), 'action-label'), 'action'));
  if (item.summary) card.append(withRenderKey(element('p', item.summary, 'inbox-summary'), 'summary'));
  if (item.question) card.append(withRenderKey(element('p', item.question, 'inbox-question'), 'question'));
  if (item.is_current === false) {
    card.append(withRenderKey(element('p', item.stale_reason || 'This checkpoint is stale and cannot be acted on.', 'worker-error'), 'stale-reason'));
  }
  const context = objectValue(item.context);
  if (context.owned_tab === true) {
    card.append(withRenderKey(element('p', 'Agent-reported worker-owned browser tab'
      + (context.tab_id ? ': ' + context.tab_id : ''), 'sub'), 'owned-tab'));
  }
  if (item.kind === 'first_application_review' || Array.isArray(context.form_fields)) {
    card.append(renderFormFieldSummary(context));
  }
  if (typeof context.cover_letter_text === 'string' && context.cover_letter_text) {
    const entered = withRenderKey(element('details', undefined, 'review-document-text'), 'cover-text');
    entered.append(element('summary', 'Cover-letter text entered in the application form'),
      element('pre', context.cover_letter_text));
    card.append(entered);
  }
  if (item.is_current !== false && Array.isArray(context.review_artifacts)) {
    const packageSection = withRenderKey(element('section', undefined, 'review-artifact-package'), 'review-artifact-package');
    packageSection.append(element('h3', 'Current prepared documents'));
    if (!context.review_artifacts.length) {
      packageSection.append(element('p', 'No generated documents were attached to this checkpoint.', 'sub'));
    } else context.review_artifacts.forEach(artifact => packageSection.append(renderReviewArtifact(artifact)));
    card.append(packageSection);
  }
  const fields = [];
  if (item.terms_text) fields.push(['Exact action terms', item.terms_text]);
  if (item.question_text) fields.push(['Question', item.question_text]);
  if (item.context_text) fields.push(['Agent context', item.context_text]);
  Object.entries(context).forEach(([key, value]) => {
    if (['artifacts', 'ready_artifacts', 'review_artifacts', 'form_fields', 'cover_letter_text',
      'session_id', 'tab_id', 'capability', 'token'].includes(key) || value == null) return;
    if (!fields.some(row => row[0] === key)) fields.push([humanize(key), typeof value === 'string' ? value : JSON.stringify(value, null, 2)]);
  });
  fields.forEach(([label, value]) => {
    const detail = withRenderKey(element('details', undefined, 'inbox-context'), 'context:' + label);
    detail.append(element('summary', label), element('pre', value)); card.append(detail);
  });
  if (item.is_current !== false) {
    collectArtifacts(context)
      .filter(file => !(file.upload && typeof file.markdown === 'string'))
      .forEach(file => card.append(artifactButton(file)));
  }
  if (item.is_current === false) {
    if (!isOpen(item)) {
      if (item.provenance) card.append(withRenderKey(element('p', 'Recorded provenance: ' + item.provenance, 'sub'), 'provenance'));
      if (item.response) card.append(withRenderKey(element('p', 'Recorded response: ' + item.response, 'sub'), 'response'));
      return card;
    }
    if (item.kind === 'approval' || item.kind === 'first_application_review') {
      const choices = element('div', undefined, 'decision-buttons');
      const approve = button(item.kind === 'first_application_review' ? 'Approve first application review' : 'Approve this exact action',
        'primary-button', () => {});
      const reject = button(item.kind === 'first_application_review' ? 'Reject first application review' : 'Reject this exact action',
        'quiet-button', () => {});
      approve.disabled = true; reject.disabled = true;
      choices.append(approve, reject); card.append(choices);
    } else {
      card.append(element('p', 'No response controls are available for a stale checkpoint.', 'sub'));
    }
    return card;
  }
  if (!isOpen(item)) {
    if (item.provenance) card.append(withRenderKey(element('p', 'Recorded provenance: ' + item.provenance, 'sub'), 'provenance'));
    if (item.response) card.append(withRenderKey(element('p', 'Recorded response: ' + item.response, 'sub'), 'response'));
    return card;
  }
  const draft = inboxDrafts.get(item.id) || { response: '', provenance: '' };
  inboxDrafts.set(item.id, draft);
  if (item.kind === 'question') {
    const key = actionKey('answer', item.id);
    const form = withRenderKey(element('form', undefined, 'inbox-response-form'), key);
    const response = document.createElement('textarea');
    response.name = 'response';
    response.setAttribute('aria-label', 'Your answer to this job question');
    response.dataset.focusKey = item.id + ':response';
    response.dataset.renderKey = 'response';
    response.dataset.draftField = 'response';
    response.required = true; response.rows = 3; response.maxLength = 5000;
    response.placeholder = item.response_hint || 'Answer this agent’s question. The agent will decide how it affects this job.';
    response.value = draft.response;
    bind(response, 'input', event => { draft.response = event.currentTarget.value; });
    const submit = element('button', 'Send answer for this job', 'primary-button'); submit.type = 'submit';
    submit.disabled = actionInFlight(key);
    form.append(response, submit);
    bind(form, 'submit', async event => {
      event.preventDefault();
      const currentForm = event.currentTarget;
      if (!beginAction(key)) return;
      const controls = currentForm.elements;
      const submittedResponse = controls.namedItem('response').value.trim();
      inboxDrafts.set(item.id, {
        response: controls.namedItem('response').value,
        provenance: draft.provenance || '',
      });
      setFormButtonsBusy(currentForm, true);
      try {
        await api('pipelines/' + segment(pipeline.id) + '/inbox/' + segment(item.id) + '/answer', {
          method: 'POST', body: JSON.stringify({ response: submittedResponse, ...(draft.provenance ? { provenance: draft.provenance } : {}) }),
        });
        const currentResponse = controls.namedItem('response').value;
        if (currentResponse.trim() === submittedResponse) inboxDrafts.delete(item.id);
        else inboxDrafts.set(item.id, { response: currentResponse, provenance: draft.provenance || '' });
        setStatus('Answer saved; the same affected job session is queued to resume.');
        await refreshDashboard();
      } catch (error) {
        inboxDrafts.set(item.id, {
          response: controls.namedItem('response').value,
          provenance: draft.provenance || '',
        });
        setStatus(error.message);
      } finally {
        endAction(key);
        if (currentForm.isConnected) setFormButtonsBusy(currentForm, false);
        refreshActionPanel('inbox');
      }
    });
    card.append(form);
  } else if (item.kind === 'approval' || item.kind === 'first_application_review') {
    const key = actionKey('decision', item.id);
    const form = withRenderKey(element('form', undefined, 'inbox-decision-form'), key);
    const provenance = document.createElement('textarea');
    provenance.name = 'provenance';
    provenance.setAttribute('aria-label', 'Optional note for this approval or rejection');
    provenance.dataset.focusKey = item.id + ':decision-provenance';
    provenance.dataset.renderKey = 'provenance';
    provenance.dataset.draftField = 'provenance';
    provenance.rows = 2; provenance.maxLength = 500;
    provenance.placeholder = 'Optional note';
    provenance.value = draft.provenance;
    bind(provenance, 'input', event => { draft.provenance = event.currentTarget.value; });
    const choices = element('div', undefined, 'decision-buttons');
    const approve = button(item.kind === 'first_application_review' ? 'Approve first application review' : 'Approve this exact action',
      'primary-button', event => sendDecision('approve', event.currentTarget.form));
    const reject = button(item.kind === 'first_application_review' ? 'Reject first application review' : 'Reject this exact action',
      'quiet-button', event => sendDecision('reject', event.currentTarget.form));
    approve.disabled = actionInFlight(key) || actionInFlight(actionKey('batch-decision', pipeline.id));
    reject.disabled = approve.disabled;
    choices.append(approve, reject);
    async function sendDecision(decision, currentForm) {
      if (!currentForm || !beginAction(key)) return;
      const currentProvenance = currentForm.elements.namedItem('provenance');
      if (!currentProvenance.reportValidity()) { endAction(key); return; }
      const submittedProvenance = currentProvenance.value.trim();
      inboxDrafts.set(item.id, { response: '', provenance: currentProvenance.value });
      setFormButtonsBusy(currentForm, true);
      try {
        await api('pipelines/' + segment(pipeline.id) + '/inbox/' + segment(item.id) + '/decision', {
          method: 'POST', body: JSON.stringify({ decision, provenance: submittedProvenance, scope_token: item.scope_token || '' }),
        });
        if (currentProvenance.value.trim() === submittedProvenance) inboxDrafts.delete(item.id);
        else inboxDrafts.set(item.id, { response: '', provenance: currentProvenance.value });
        setStatus('Your ' + decision + ' decision was recorded for this exact job checkpoint.');
        await refreshDashboard();
      } catch (error) {
        inboxDrafts.set(item.id, { response: '', provenance: currentProvenance.value });
        setStatus(error.message);
      } finally {
        endAction(key);
        if (currentForm.isConnected) setFormButtonsBusy(currentForm, false);
        refreshActionPanel('inbox');
      }
    }
    form.append(provenance, choices); card.append(form);
  } else {
    card.append(element('p', 'This checkpoint is visible but has no supported response action.', 'worker-error'));
  }
  return card;
}
function reconcileBatchSelections(items) {
  const current = new Map(items.map(item => [String(item.id), item]));
  let removed = 0;
  for (const [id, selected] of batchSelections) {
    const item = current.get(id);
    if (!item || !item.batch_eligible || item.scope_token !== selected.scope_token
        || item.pipeline_id !== selected.pipeline_id) {
      batchSelections.delete(id); removed++;
    }
  }
  if (removed) batchSelectionNotice = 'Changed or resolved requests were removed from your selection. Review them again before approving.';
}

function renderApprovalBatches(items) {
  const tray = $('approval-batches');
  if (!tray) return;
  const groups = new Map();
  items.filter(item => item.kind === 'approval' && item.state === 'pending'
    && item.is_current !== false).forEach(item => {
    const id = item.__pipeline.id;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(item);
  });
  const nodes = [];
  if (batchSelectionNotice) nodes.push(withRenderKey(element('p', batchSelectionNotice, 'worker-error'), 'selection-notice'));
  for (const [pipelineId, requests] of groups) {
    const pipeline = requests[0].__pipeline;
    const key = actionKey('batch-decision', pipelineId);
    const busy = actionInFlight(key) || requests.some(item => actionInFlight(actionKey('decision', item.id)));
    const eligible = requests.filter(item => item.batch_eligible);
    const selected = requests.filter(item => batchSelections.has(String(item.id)));
    const section = withRenderKey(element('section', undefined, 'approval-batch'), 'batch:' + pipelineId);
    section.append(element('h3', (pipeline.company_name || 'Company') + ' · approval batch'),
      element('p', 'Review the exact actions and terms below. Select current requests, then record one decision for the selection. New requests are never selected automatically.', 'sub'));
    if (!eligible.length) section.append(element('p', 'Resume this campaign to act on its current approvals.', 'hint'));
    const controls = withRenderKey(element('div', undefined, 'decision-buttons'), 'selection-controls');
    const select = button('Select ' + Math.min(100, eligible.length) + ' current requests', 'quiet-button', () => {
      eligible.slice(0, 100).forEach(item => batchSelections.set(String(item.id),
        { id: item.id, pipeline_id: pipelineId, scope_token: item.scope_token }));
      batchSelectionNotice = ''; renderInbox();
    });
    select.disabled = busy || !eligible.length;
    const clear = button('Clear selection', 'quiet-button', () => {
      requests.forEach(item => batchSelections.delete(String(item.id))); renderInbox();
    });
    clear.disabled = busy || !selected.length;
    controls.append(select, clear); section.append(controls);
    const rows = withRenderKey(element('div', undefined, 'approval-batch-rows'), 'rows');
    for (const item of requests) {
      // A scope change also remounts its checkbox, so a focused stale check cannot survive a refresh.
      const row = withRenderKey(element('div', undefined, 'approval-batch-row'), item.id + ':' + item.scope_token);
      const label = element('label', undefined, 'approval-batch-choice');
      const check = document.createElement('input'); check.type = 'checkbox';
      check.dataset.batchSelection = 'true';
      check.checked = batchSelections.has(String(item.id)); check.disabled = busy || !item.batch_eligible;
      check.setAttribute('aria-label', 'Select approval for ' + (item.job_title || item.id));
      bind(check, 'change', event => {
        if (event.currentTarget.checked) batchSelections.set(String(item.id),
          { id: item.id, pipeline_id: pipelineId, scope_token: item.scope_token });
        else batchSelections.delete(String(item.id));
        batchSelectionNotice = ''; renderInbox();
      });
      label.append(check, element('strong', item.job_title || 'Application checkpoint'));
      const detail = element('div', undefined, 'approval-batch-detail');
      detail.append(element('p', 'Requested action: ' + humanize(item.action), 'action-label'));
      detail.append(element('p', item.summary || humanize(item.action), 'inbox-summary'));
      const review = element('a', 'Review exact action, terms and documents');
      review.href = '#inbox-request-' + item.id;
      bind(review, 'click', event => {
        event.preventDefault();
        const card = $('inbox-request-' + item.id);
        if (card) { card.querySelectorAll('details.inbox-context').forEach(node => { node.open = true; }); card.scrollIntoView({ block: 'start' }); }
      });
      detail.append(review); row.append(label, detail); rows.append(row);
    }
    section.append(rows);
    const choices = withRenderKey(element('div', undefined, 'decision-buttons'), 'batch-actions');
    const count = selected.length;
    const approve = button('Approve ' + count + ' selected requests', 'primary-button', () => sendBatch('approve'));
    const reject = button('Reject ' + count + ' selected requests', 'quiet-button', () => sendBatch('reject'));
    approve.disabled = busy || !count; reject.disabled = busy || !count;
    choices.append(approve, reject); section.append(choices); nodes.push(section);
    async function sendBatch(decision) {
      // Freeze IDs and fingerprints before the async request; no later arrivals join this batch.
      const submitted = requests.filter(item => batchSelections.has(String(item.id)))
        .map(item => ({ id: item.id, scope_token: batchSelections.get(String(item.id)).scope_token }));
      if (!submitted.length || !beginAction(key)) return;
      renderInbox();
      try {
        await api('pipelines/' + segment(pipelineId) + '/inbox/batch-decision', {
          method: 'POST', body: JSON.stringify({ decision, items: submitted }),
        });
        submitted.forEach(item => batchSelections.delete(String(item.id)));
        batchSelectionNotice = '';
        setStatus('Recorded your ' + decision + ' decision for ' + submitted.length + ' exact requests.');
      } catch (error) {
        setStatus(error.message);
      } finally {
        endAction(key);
        await refreshDashboard();
        refreshActionPanel('inbox');
      }
    }
  }
  preserveViewport(tray, () => reconcileChildren(tray, nodes));
}

function renderInbox() {
  captureInboxDrafts();
  const list = $('agent-inbox-list');
  const all = [];
  pipelines.forEach(view => {
    const p = pipelineOf(view), payload = pipelineInboxes.get(p.id);
    if (payload && Array.isArray(payload.items)) payload.items.forEach(item => all.push({ ...item, __pipeline: p }));
  });
  all.sort((a, b) => String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || '')));
  const open = all.filter(isOpen), resolved = all.filter(item => !isOpen(item));
  reconcileBatchSelections(all);
  renderApprovalBatches(open);
  const openNodes = open.map(item => renderInboxItem(item, item.__pipeline));
  const openIds = new Set(open.map(item => String(item.id)));
  const activeCard = document.activeElement?.closest?.('.inbox-card');
  if (activeCard && !openIds.has(String(activeCard.dataset.inboxId || ''))
      && activeCard.querySelector('.inbox-response-form, .inbox-decision-form')) {
    if (!activeCard.querySelector('[data-stale-draft-note]')) {
      const note = withRenderKey(element('p',
        'This checkpoint changed while you were editing. Your unsent text stays here, but its action is disabled; refresh the inbox to review the saved state.',
        'worker-error'), 'stale-draft-note');
      note.dataset.staleDraftNote = 'true';
      activeCard.append(note);
    }
    activeCard.querySelectorAll('button').forEach(control => { control.disabled = true; });
    openNodes.unshift(activeCard);
  }
  if (!openNodes.length) openNodes.push(withRenderKey(element('p', 'No agent checkpoints need attention.', 'empty-state'), 'empty'));
  preserveViewport(list, () => reconcileChildren(list, openNodes));
  const resolvedDetails = $('resolved-agent-history');
  if (resolvedDetails.open) {
    const history = $('resolved-agent-list');
    const resolvedNodes = resolved.map(item => {
      const card = renderInboxItem(item, item.__pipeline);
      card.classList.add('resolved-inbox-card');
      return card;
    });
    if (!resolved.length) resolvedNodes.push(withRenderKey(element('p', 'No resolved agent checkpoints are recorded.', 'sub'), 'empty'));
    preserveViewport(history, () => reconcileChildren(history, resolvedNodes));
  }
  const legacyDetails = $('legacy-question-history');
  if (legacyDetails.open) {
    const oldQuestions = $('legacy-question-list');
    const legacyNodes = legacyQuestions.map(question => {
      const record = withRenderKey(element('article', undefined, 'legacy-question'), 'legacy:' + (question.id || question.prompt));
      record.append(withRenderKey(element('strong', question.prompt || question.question || 'Legacy question'), 'prompt'));
      if (question.answer) record.append(withRenderKey(element('p', 'Historical answer: ' + question.answer, 'sub'), 'answer'));
      if (question.context || question.provenance) record.append(withRenderKey(element('p', question.context || question.provenance, 'sub'), 'context'));
      record.append(withRenderKey(element('p', 'Read-only historical record. Adoption does not treat it as a new user answer.', 'sub'), 'read-only'));
      return record;
    });
    if (!legacyQuestions.length) legacyNodes.push(withRenderKey(element('p', 'No legacy question records are saved.', 'sub'), 'empty'));
    preserveViewport(oldQuestions, () => reconcileChildren(oldQuestions, legacyNodes));
  }
  $('inbox-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}
function captureInboxDrafts() {
  document.querySelectorAll('.inbox-card[data-inbox-id]').forEach(card => {
    const id = card.dataset.inboxId;
    if (!id) return;
    const draft = inboxDrafts.get(id) || { response: '', provenance: '' };
    card.querySelectorAll('[data-draft-field]').forEach(control => {
      draft[control.dataset.draftField] = control.value;
    });
    inboxDrafts.set(id, draft);
  });
}
async function setCampaignVisibility(id, hidden) {
  const key = actionKey('visibility', id);
  if (!beginAction(key)) return;
  renderPipelines();
  try {
    await api('pipelines/' + segment(id) + '/visibility', {method: 'POST', body: JSON.stringify({hidden})});
    expandedWorkers.delete(id);
    expandedFinishedWorkers.delete(id);
    setStatus(hidden ? 'Campaign hidden. You can restore it from Show hidden campaigns.' : 'Campaign restored.');
    await refreshDashboard();
  } catch (error) { setStatus(error.message); }
  finally { endAction(key); renderPipelines(); }
}
function renderPipelines() {
  const root = $('pipeline-list');
  const nodes = [];
  const hiddenCount = pipelines.filter(view => pipelineOf(view).dashboard_hidden).length;
  const visibilityToggle = $('hidden-campaigns-toggle');
  visibilityToggle.textContent = (showHiddenCampaigns ? 'Hide hidden campaigns' : 'Show hidden campaigns') + ' (' + hiddenCount + ')';
  visibilityToggle.setAttribute('aria-pressed', String(showHiddenCampaigns));
  bind(visibilityToggle, 'click', () => { showHiddenCampaigns = !showHiddenCampaigns; renderPipelines(); });
  const visiblePipelines = pipelines.filter(view => showHiddenCampaigns || !pipelineOf(view).dashboard_hidden);
  if (!pipelineCurrent) nodes.push(withRenderKey(element('p', 'Pipeline status requires the agentic_pipeline_v1 service release.', 'empty-state'), 'status'));
  else if (!visiblePipelines.length) nodes.push(withRenderKey(element('p', hiddenCount ? 'All campaigns are hidden. Use Show hidden campaigns to restore one.' : 'No company pipelines yet. Start one above to begin discovery.', 'empty-state'), 'empty'));
  else visiblePipelines.forEach(view => nodes.push(renderPipeline(view)));
  preserveViewport(root, () => reconcileChildren(root, nodes));
  $('pipeline-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  renderWorkspaceOverview();
}
function renderWorkspaceOverview() {
  const totals = pipelines.reduce((result, view) => {
    const counts = countsOf(view);
    result.submitted += count(counts.submitted);
    result.checkpoints += count(counts.awaiting_user);
    result.active += (Array.isArray(view.workers) ? view.workers : [])
      .filter(worker => ['running', 'queued'].includes(worker.state)).length;
    return result;
  }, { submitted: 0, checkpoints: 0, active: 0 });
  $('overview-companies').textContent = String(pipelines.length);
  $('overview-active').textContent = String(totals.active);
  $('overview-submitted').textContent = String(totals.submitted);
  $('overview-checkpoints').textContent = String(totals.checkpoints);
}
function renderSummary() {
  const root = $('summary'), nodes = [];
  [
    ['Historical job records', jobs.length],
    ['Recorded as submitted', jobs.filter(job => job.phase === 'submitted').length],
    ['Companies with saved jobs', new Set(jobs.map(job => job.company_id).filter(Boolean)).size],
    ['Pending legacy questions', summaries.pending_questions || 0],
  ].forEach(([label, value]) => {
    const card = withRenderKey(element('div', undefined, 'summary-card'), 'summary:' + label);
    card.append(element('strong', count(value)), element('span', label)); nodes.push(card);
  });
  preserveViewport(root, () => reconcileChildren(root, nodes));
}
function renderJobs() {
  const body = $('job-rows'), nodes = [];
  const query = $('search').value.trim().toLocaleLowerCase();
  const filtered = jobs.filter(job => [job.title, job.company_name, job.location, job.requisition_id]
    .some(value => String(value || '').toLocaleLowerCase().includes(query)));
  const pages = Math.max(1, Math.ceil(filtered.length / jobsPerPage));
  jobPage = Math.min(Math.max(0, jobPage), pages - 1);
  const first = jobPage * jobsPerPage;
  $('jobs-result-count').textContent = filtered.length ? 'Showing ' + (first + 1) + '–' + Math.min(first + jobsPerPage, filtered.length) + ' of ' + filtered.length + ' records' : 'No matching records';
  $('jobs-page-number').textContent = 'Page ' + (jobPage + 1) + ' of ' + pages;
  $('jobs-previous').disabled = jobPage === 0;
  $('jobs-next').disabled = jobPage + 1 >= pages;
  if (!filtered.length) {
    const row = element('tr'), cell = element('td', query ? 'No matching historical records.' : 'No historical records are available.');
    cell.colSpan = 5; row.append(cell); nodes.push(withRenderKey(row, 'empty'));
    preserveViewport(body, () => reconcileChildren(body, nodes)); return;
  }
  filtered.slice(first, first + jobsPerPage).forEach(job => {
    const row = withRenderKey(element('tr'), 'job:' + job.id), role = element('td');
    role.append(element('strong', job.title || 'Untitled role'), element('p', job.company_name || job.company_id || '', 'sub'));
    const location = element('td', job.location || 'Unknown');
    const jobState = job.application_status || job.phase || 'unknown';
    const stage = element('td');
    stage.append(element('span', humanize(jobState), 'badge status-' + String(jobState).replace(/[^a-z_]/g, '')));
    const progress = element('td', job.application_provenance || (job.phase === 'submitted' ? 'Submission recorded' : job.run_status && job.run_status !== 'idle' ? humanize(job.run_status) : 'Saved record'));
    const action = element('td');
    const recordButton = button('View record', 'small-button', () => showJob(job.id));
    recordButton.setAttribute('aria-label', 'View record for ' + (job.title || 'untitled role') + ' at ' + (job.company_name || job.company_id || 'company'));
    action.append(recordButton);
    row.append(role, location, stage, progress, action); nodes.push(row);
  });
  preserveViewport(body, () => reconcileChildren(body, nodes));
}
function renderTechnologies() {
  const root = $('technology-list'), nodes = [];
  const query = $('technology-search').value.trim().toLocaleLowerCase();
  const visible = technologies.filter(row => [row.technology, row.name, row.context]
    .some(value => String(value || '').toLocaleLowerCase().includes(query)));
  $('technology-results').textContent = visible.length + ' of ' + technologies.length + ' saved technology answers';
  if (!technologies.length) {
    nodes.push(withRenderKey(element('p', 'No technology history is saved.', 'empty-state'), 'empty'));
    preserveViewport(root, () => reconcileChildren(root, nodes)); return;
  }
  const groups = new Map();
  visible.forEach(row => {
    const key = row.answer || 'unknown';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });
  groups.forEach((records, label) => {
    const section = withRenderKey(element('section', undefined, 'technology-group'), 'technology:' + label);
    const groupLabel = label === 'yes' ? 'Confirmed knowledge' : label === 'no' ? 'Not currently known' : humanize(label);
    section.append(withRenderKey(element('h3', groupLabel + ' (' + records.length + ')'), 'heading'));
    const list = element('ul', undefined, 'technology-grid');
    records.forEach(row => {
      const card = withRenderKey(element('li', undefined, 'technology-card'), 'tech:' + (row.id || row.technology || row.name));
      const heading = element('div', undefined, 'technology-card-heading');
      heading.append(element('strong', row.technology || row.name || 'Technology'), element('span', humanize(label), 'badge status-' + String(label).replace(/[^a-z_]/g, '')));
      card.append(heading);
      if (row.context) card.append(element('p', row.context));
      list.append(card);
    });
    section.append(list); nodes.push(section);
  });
  if (!visible.length && technologies.length) nodes.push(withRenderKey(element('p', 'No matching technology answers.', 'empty-state'), 'no-match'));
  preserveViewport(root, () => reconcileChildren(root, nodes));
}
function renderCompanies() {
  const root = $('company-list'), nodes = [];
  const historical = pipelines.filter(view => pipelineOf(view).mode === 'legacy');
  if (!companies.length && !historical.length) {
    nodes.push(withRenderKey(element('p', 'No saved company records are available.', 'empty-state'), 'empty'));
    preserveViewport(root, () => reconcileChildren(root, nodes)); return;
  }
  companies.forEach(company => {
    const card = withRenderKey(element('article', undefined, 'record company-card'), 'company:' + company.id);
    card.append(withRenderKey(element('h3', company.name || company.id), 'heading'));
    if (company.careers_url) {
      card.append(externalLink(company.careers_url, company.careers_url));
    }
    card.append(element('p', company.enabled ? 'Enabled in the historical workflow.' : 'Disabled in the historical workflow.'));
    nodes.push(card);
  });
  historical.forEach(view => {
    const p = pipelineOf(view), card = withRenderKey(element('article', undefined, 'record company-card'), 'legacy-company:' + p.id);
    const heading = withRenderKey(element('div', undefined, 'section-heading'), 'heading');
    heading.append(withRenderKey(element('h3', (p.company_name || p.company_id) + ' · historical run'), 'name'));
    heading.append(button(expandedLegacy.has(p.id) ? 'Hide jobs' : 'Inspect adoption options', 'outline-button', async () => {
      if (expandedLegacy.has(p.id)) expandedLegacy.delete(p.id);
      else { expandedLegacy.add(p.id); pipelineJobs.set(p.id, null); await loadPipelineJobs(p.id); }
      renderCompanies();
    }));
    card.append(heading, withRenderKey(element('p', 'Historical status: ' + statusLabel(view) + '. This run does not start automatically.', 'sub'), 'status'));
    if (expandedLegacy.has(p.id)) card.append(renderPipelineJobs(p, view));
    nodes.push(card);
  });
  preserveViewport(root, () => reconcileChildren(root, nodes));
}
async function showJob(id) {
  try {
    currentJobDetail = await api('jobs/' + segment(id));
    const detail = currentJobDetail;
    $('detail-title').textContent = detail.title || 'Application details';
    const summary = $('detail-summary'); summary.replaceChildren();
    const grid = element('div', undefined, 'detail-summary-grid');
    [
      ['Company', detail.company_name || detail.company_id],
      ['Location', detail.location],
      ['Application state', detail.application_outcome?.status || detail.application_status || detail.phase],
      ['Recruiter outcome source', detail.application_outcome?.provenance || 'Not recorded'],
      ['Job URL', detail.url],
    ].forEach(([label, value]) => {
      const p = element('p'); p.append(element('strong', label + ': '), document.createTextNode(String(value || 'Unknown'))); grid.append(p);
    });
    summary.append(grid);
    $('detail-text').textContent = JSON.stringify(detail, null, 2);
    $('outcome-section').classList.toggle('hidden', !(detail.application_outcome?.submitted === true || detail.phase === 'submitted'));
    $('outcome-value').value = detail.application_outcome?.status || 'pending';
    $('outcome-source').value = ''; $('outcome-note').value = '';
    $('detail').classList.remove('hidden'); $('detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
    renderPackages(detail);
  } catch (error) { setStatus(error.message); }
}
function renderPackages(detail) {
  const root = $('detail-packages'); root.replaceChildren();
  (detail.packages || []).forEach(pack => {
    const card = element('section', undefined, 'record package-card');
    card.append(element('h3', 'Historical package ' + (pack.id || '')));
    (pack.files || []).forEach(file => {
      const filename = file.filename || file.name;
      if (!['resume.pdf', 'cover-letter.pdf'].includes(filename)) return;
      card.append(button('Download ' + filename, 'small-button', async () => {
        try {
          const blob = await api('packages/' + segment(pack.id) + '/' + segment(filename), { expectBlob: true });
          const url = URL.createObjectURL(blob), link = document.createElement('a');
          link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url);
        } catch (error) { setStatus(error.message); }
      }));
    });
    root.append(card);
  });
}

function pipelineNeedsPolling(view) {
  const p = pipelineOf(view);
  return ['queued', 'running', 'stopping'].includes(p.status)
    || (Array.isArray(view.workers) && view.workers.some(worker => ['queued', 'running', 'waiting_child'].includes(worker.state)));
}
function shouldFetchPipelineInbox(view) {
  return pipelineOf(view).mode === 'agentic' && currentView === 'inbox';
}
async function refreshPipelineDetails(sequence, revision) {
  const requests = [];
  for (const view of pipelines) {
    const p = pipelineOf(view);
    if (p.mode !== 'agentic' && p.mode) continue;
    if (shouldFetchPipelineInbox(view)) {
      requests.push(api('pipelines/' + segment(p.id) + '/inbox').then(value => {
        if (sequence === refreshSequence && revision === dataRevision) pipelineInboxes.set(p.id, value);
      }).catch(error => { if (sequence === refreshSequence && revision === dataRevision) setStatus(error.message); }));
    }
    if (currentView === 'pipelines' && expandedWorkers.has(p.id)) {
      requests.push(loadPipelineWorkers(p.id));
    }
    if (currentView === 'pipelines' && expandedPipelines.has(p.id)) {
      requests.push(loadPipelineJobs(p.id));
    }
    if (currentView === 'pipelines' && expandedRuntime.has(p.id)) {
      requests.push(loadRuntimeStatus(p.id));
    }
    if (currentView === 'companies' && expandedLegacy.has(p.id)) requests.push(loadPipelineJobs(p.id));
  }
  await Promise.allSettled(requests);
}
async function refreshVisiblePanel(sequence, revision) {
  const panel = currentView;
  let request = null;
  if (panel === 'jobs') {
    request = Promise.allSettled([api('jobs'), api('summary')]).then(results => {
      if (sequence !== refreshSequence || revision !== dataRevision) return;
      if (results[0].status === 'fulfilled') jobs = Array.isArray(results[0].value) ? results[0].value : [];
      else setStatus('Application records could not be loaded: ' + results[0].reason.message);
      if (results[1].status === 'fulfilled') summaries = results[1].value || {};
      renderSummary(); renderJobs();
    });
  } else if (panel === 'technologies') {
    request = api('technologies').then(value => {
      if (sequence !== refreshSequence || revision !== dataRevision) return;
      technologies = Array.isArray(value) ? value : []; renderTechnologies();
    }).catch(error => { if (sequence === refreshSequence && revision === dataRevision) setStatus('Technology history could not be loaded: ' + error.message); });
  } else if (panel === 'companies') {
    request = api('companies').then(value => {
      if (sequence !== refreshSequence || revision !== dataRevision) return;
      companies = Array.isArray(value) ? value : []; renderCompanies();
    }).catch(error => { if (sequence === refreshSequence && revision === dataRevision) setStatus('Saved companies could not be loaded: ' + error.message); });
  } else if (panel === 'inbox' && $('legacy-question-history').open) {
    request = api('questions').then(value => {
      if (sequence !== refreshSequence || revision !== dataRevision) return;
      legacyQuestions = Array.isArray(value) ? value : []; renderInbox();
    }).catch(error => { if (sequence === refreshSequence && revision === dataRevision) setStatus('Legacy questions could not be loaded: ' + error.message); });
  }
  if (request) await request;
}
async function runRefresh({ includeHealth = false, includePanel = false, force = false } = {}) {
  if (refreshPromise) {
    const previous = refreshPromise;
    await previous.promise.catch(() => {});
    if (!force && previous.revision === dataRevision) return;
    return runRefresh({ includeHealth, includePanel, force: false });
  }
  const sequence = ++refreshSequence, revision = dataRevision;
  const flight = { sequence, revision, promise: null };
  flight.promise = (async () => {
    const core = includeHealth
      ? await Promise.allSettled([api('health'), api('pipelines'), api('agent-quota')])
      : await Promise.allSettled([Promise.resolve(null), api('pipelines'), api('agent-quota')]);
    if (sequence !== refreshSequence || revision !== dataRevision) return;
    if (core[0].status === 'fulfilled' && core[0].value) {
      serviceHealth = core[0].value; renderHealth();
    }
    if (core[1].status === 'fulfilled') {
      const next = Array.isArray(core[1].value) ? core[1].value : [];
      const liveIds = new Set(next.map(view => pipelineOf(view).id));
      for (const id of pipelineJobs.keys()) if (!liveIds.has(id)) pipelineJobs.delete(id);
      for (const id of workerRecords.keys()) if (!liveIds.has(id)) workerRecords.delete(id);
      for (const id of pipelineInboxes.keys()) if (!liveIds.has(id)) pipelineInboxes.delete(id);
      pipelines = next;
    } else setStatus('Pipeline records could not be loaded: ' + core[1].reason.message);
    if (core[2].status === 'fulfilled') window.agentQuotaState = core[2].value;
    await refreshPipelineDetails(sequence, revision);
    if (sequence !== refreshSequence || revision !== dataRevision) return;
    if (currentView === 'pipelines') renderPipelines();
    if (currentView === 'inbox') renderInbox();
    if (currentView === 'companies') renderCompanies();
    renderQuota(window.agentQuotaState);
    if (includePanel) await refreshVisiblePanel(sequence, revision);
    if (sequence !== refreshSequence || revision !== dataRevision) return;
    updatePolling();
  })();
  refreshPromise = flight;
  try { await flight.promise; }
  finally { if (refreshPromise === flight) refreshPromise = null; }
}
function refreshDashboard() { return runRefresh({ includeHealth: true, includePanel: true, force: true }); }
function refreshLiveState() { return runRefresh({ includeHealth: false, includePanel: false, force: false }); }
function renderQuota(quota) {
  const state = quota || { paused: false };
  $('quota-status').textContent = state.paused
    ? 'Account-wide pause recorded. Confirm your quota has reset before resuming saved work.'
    : 'No account-wide quota pause is recorded.';
  $('workspace-alert').classList.toggle('hidden', !state.paused);
  $('quota-resume').disabled = !pipelineCurrent || !state.paused || !$('quota-reset-confirm').checked;
}
function formatMetric(value, suffix = '') {
  if (value === undefined || value === null || value === '') return 'Unavailable';
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value.toLocaleString() + suffix;
  }
  return String(value) + suffix;
}
function usageMetricText(group, field, suffix = '') {
  const value = formatMetric(group?.[field], suffix);
  const known = group?.known?.[field], missing = group?.missing?.[field];
  if (!Number.isInteger(known) || !Number.isInteger(missing) || known + missing === 0 || missing === 0) return value;
  return value + ' · reported for ' + known + ' of ' + (known + missing) + ' turns';
}
function efficiencyCard(label, value, suffix = '') {
  const card = withRenderKey(element('div', undefined, 'efficiency-metric'), 'efficiency:' + label);
  card.append(element('strong', formatMetric(value, suffix)), element('span', label));
  return card;
}
function hostReadinessText(health = serviceHealth) {
  const host = health?.runtime_status?.execution_host || health?.execution_host;
  const auth = health?.authentication || health?.auth_status;
  const hostText = host && typeof host === 'object'
    ? [host.mode, host.status, host.ready === true ? 'ready' : host.ready === false ? 'not ready' : null, host.detail]
      .filter(value => value !== undefined && value !== null && value !== '').join(' · ') || 'details supplied, readiness unavailable'
    : (host === undefined || host === null ? 'unavailable from the service' : String(host));
  const authText = auth && typeof auth === 'object'
    ? [auth.status, auth.signed_in === true ? 'signed in' : auth.signed_in === false ? 'not signed in' : null, auth.detail]
      .filter(value => value !== undefined && value !== null && value !== '').join(' · ') || 'details supplied, state unavailable'
    : (auth === undefined || auth === null ? 'unavailable from the service' : String(auth));
  return 'Execution host: ' + hostText + '. Authentication state: ' + authText + '.';
}
function renderEfficiency() {
  $('host-readiness').textContent = hostReadinessText();
  const root = $('efficiency-metrics'), report = efficiencyMetrics;
  const nodes = [];
  if (efficiencyMetricsError) nodes.push(withRenderKey(element('p', 'Usage metrics could not be loaded: ' + efficiencyMetricsError, 'worker-error'), 'error'));
  if (!report) {
    nodes.push(withRenderKey(element('p', 'Usage metrics are unavailable until the service provides an efficiency report.', 'sub'), 'unavailable'));
    reconcileChildren(root, nodes); return;
  }
  const totals = report.totals || {};
  [
    ['Agent turns', formatMetric(totals.turns)],
    ['Input tokens', usageMetricText(totals, 'input_tokens')],
    ['Cached input tokens', usageMetricText(totals, 'cached_input_tokens')],
    ['Output tokens', usageMetricText(totals, 'output_tokens')],
    ['Reasoning tokens', usageMetricText(totals, 'reasoning_tokens')],
    ['Tool calls', usageMetricText(totals, 'tool_call_count')],
    ['Agent execution time (sum of turns)', usageMetricText(totals, 'model_elapsed_seconds', ' s')],
    ['Awaiting user (union of waits)', report.human_wait_seconds ?? totals.human_wait_seconds, ' s'],
  ].forEach(([label, value, suffix]) => nodes.push(efficiencyCard(label, value, suffix || '')));
  const cache = report.cache || {};
  const timing = report.timing || {};
  [
    ['Browser tool time (measured worker seconds)', timing.browser_tool_seconds],
    ['Other tool time (measured worker seconds)', timing.other_tool_seconds],
    ['Model / network / orchestration time', timing.model_and_transport_seconds],
    ['Worker startup time', timing.startup_seconds],
    ['Browser call latency p50', timing.browser_p50_seconds],
    ['Browser call latency p95', timing.browser_p95_seconds],
  ].forEach(([label, value]) => nodes.push(efficiencyCard(label, value == null ? 'Unavailable' : Number(value).toFixed(2), value == null ? '' : ' s')));
  nodes.push(withRenderKey(element('p', 'Timing covers ' + (timing.measured_turns || 0)
    + ' instrumented turns. Model/network time is not pure inference. Concurrent workers overlap; category totals are not campaign wall time.', 'sub'), 'timing-coverage'));
  [
    ['Evaluation cache hits', cache.evaluation_hits], ['Evaluation cache misses', cache.evaluation_misses],
    ['Document render cache hits', cache.render_hits], ['Document render cache misses', cache.render_misses],
  ].forEach(([label, value]) => nodes.push(efficiencyCard(label, value)));
  const roles = report.by_role && typeof report.by_role === 'object' ? Object.entries(report.by_role) : [];
  if (roles.length) roles.forEach(([role, values]) => {
    const card = withRenderKey(element('div', undefined, 'efficiency-role'), 'role:' + role);
    card.append(element('strong', humanize(role)));
    const fields = values && typeof values === 'object' ? values : {};
    card.append(element('span', 'Turns ' + formatMetric(fields.turns)
      + ' · Input ' + usageMetricText(fields, 'input_tokens')
      + ' · Output ' + usageMetricText(fields, 'output_tokens')
      + ' · Tools ' + usageMetricText(fields, 'tool_call_count')));
    nodes.push(card);
  });
  else nodes.push(withRenderKey(element('p', 'Per-role usage is unavailable in this report.', 'sub'), 'roles-unavailable'));
  (report.by_execution || []).forEach((values, index) => {
    const choice = values.execution || {};
    const label = choice.provider ? `${choice.provider} · ${choice.model} · ${choice.effort}` : 'Historical usage · execution choice not recorded';
    const card = withRenderKey(element('div', undefined, 'efficiency-role'), 'execution:' + index);
    card.append(element('strong', label), element('span', 'Turns ' + formatMetric(values.turns)
      + ' · Input ' + usageMetricText(values, 'input_tokens') + ' · Output ' + usageMetricText(values, 'output_tokens')));
    nodes.push(card);
  });
  nodes.push(withRenderKey(element('p', 'Inbox waits are measured separately. Parallel worker times overlap; cached input is part of input and reasoning is part of output.', 'sub'), 'measurement-note'));
  reconcileChildren(root, nodes);
}
async function loadEfficiency() {
  const requestId = (panelRequestIds.get('efficiency') || 0) + 1;
  panelRequestIds.set('efficiency', requestId);
  const revision = dataRevision;
  efficiencyMetricsError = null;
  try {
    const [report, health] = await Promise.all([api('efficiency'), api('health')]);
    if (panelRequestIds.get('efficiency') !== requestId || dataRevision !== revision || !$('efficiency-details').open) return;
    efficiencyMetrics = report || {};
    serviceHealth = health || serviceHealth;
    renderHealth(); renderEfficiency();
  } catch (error) {
    if (panelRequestIds.get('efficiency') !== requestId || dataRevision !== revision) return;
    efficiencyMetricsError = error.message;
    renderEfficiency();
  }
}
function updatePolling() {
  const active = pipelines.some(view => {
    return pipelineNeedsPolling(view);
  }) || (currentView === 'pipelines' && expandedRuntime.size > 0);
  if (active && !pollTimer) pollTimer = setInterval(() => refreshLiveState().catch(error => setStatus(error.message)), 5000);
  else if (!active && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function isAllowedCareersUrl(url) {
  if (url.username || url.password) return false;
  if (url.protocol === 'https:') return true;
  return serviceHealth?.fixture_test_mode?.allow_loopback_http_careers_url === true
    && url.href === 'http://127.0.0.1:8765/__fictional/board';
}

$('pipeline-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!pipelineCurrent) return;
  const submitted = {
    companyName: $('pipeline-company-name').value.trim(),
    careersUrl: $('pipeline-careers-url').value.trim(),
    title: $('pipeline-title-filter').value.trim(),
    location: $('pipeline-location-filter').value.trim(),
  };
  const companyName = submitted.companyName;
  const careersUrl = submitted.careersUrl;
  let url;
  try { url = new URL(careersUrl); } catch (_) { setStatus('Enter a valid HTTPS careers page URL.'); return; }
  if (!isAllowedCareersUrl(url)) {
    setStatus(serviceHealth?.fixture_test_mode?.allow_loopback_http_careers_url === true
      ? 'Use HTTPS or the exact loopback fixture URL http://127.0.0.1:8765/__fictional/board.'
      : 'The careers page URL must use HTTPS.');
    return;
  }
  $('pipeline-submit').disabled = true;
  try {
    const view = await api('pipelines', {
      method: 'POST',
      body: JSON.stringify({
        company_name: companyName,
        careers_url: careersUrl,
        title_contains: submitted.title,
        location_contains: submitted.location,
      }),
    });
    const unchanged = $('pipeline-company-name').value.trim() === submitted.companyName
      && $('pipeline-careers-url').value.trim() === submitted.careersUrl
      && $('pipeline-title-filter').value.trim() === submitted.title
      && $('pipeline-location-filter').value.trim() === submitted.location;
    if (unchanged) form.reset();
    const p = pipelineOf(view);
    if (p.id) expandedPipelines.add(p.id);
    setStatus('Company pipeline created. Discovery will explore that careers site.');
    await refreshDashboard();
  } catch (error) { setStatus(error.message); }
  finally { $('pipeline-submit').disabled = !pipelineCurrent; }
});
$('pipeline-refresh').addEventListener('click', () => refreshDashboard().catch(error => setStatus(error.message)));
$('inbox-refresh').addEventListener('click', () => refreshDashboard().catch(error => setStatus(error.message)));
$('refresh').addEventListener('click', () => refreshDashboard().catch(error => setStatus(error.message)));
$('search').addEventListener('input', () => { jobPage = 0; renderJobs(); });
$('jobs-previous').addEventListener('click', () => { jobPage -= 1; renderJobs(); $('jobs').scrollIntoView({ block: 'start', behavior: 'instant' }); });
$('jobs-next').addEventListener('click', () => { jobPage += 1; renderJobs(); $('jobs').scrollIntoView({ block: 'start', behavior: 'instant' }); });
$('technology-search').addEventListener('input', renderTechnologies);
$('quota-reset-confirm').addEventListener('change', () => renderQuota(window.agentQuotaState));
$('quota-resume').addEventListener('click', async () => {
  if (!$('quota-reset-confirm').checked) return;
  $('quota-resume').disabled = true;
  try {
    await api('agent-quota/resume', { method: 'POST', body: '{}' });
    $('quota-reset-confirm').checked = false;
    setStatus('Account quota latch cleared; eligible saved sessions were queued to resume.');
    await refreshDashboard();
  } catch (error) { setStatus(error.message); }
});
$('outcome-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!currentJobDetail) return;
  const source = $('outcome-source').value.trim(), note = $('outcome-note').value.trim();
  if (!source || !note) { setStatus('Enter the outcome source and a short note.'); return; }
  try {
    await api('jobs/' + segment(currentJobDetail.id) + '/application-status', {
      method: 'POST',
      body: JSON.stringify({
        status: $('outcome-value').value,
        provenance: 'Source: ' + source + '\nNote: ' + note,
      }),
    });
    setStatus('Recruiter outcome recorded with source and note.');
    await showJob(currentJobDetail.id); await refreshDashboard();
  } catch (error) { setStatus(error.message); }
});
$('close-detail').addEventListener('click', () => { $('detail').classList.add('hidden'); currentJobDetail = null; });
const workspaceViews = { pipelines: 'Pipelines', jobs: 'Applications', inbox: 'Agent inbox', technologies: 'Technology history', companies: 'Saved companies', settings: 'Settings' };
function navigateWorkspace(view, { updateHistory = true, focus = false } = {}) {
  if (!Object.hasOwn(workspaceViews, view)) view = 'pipelines';
  currentView = view;
  document.querySelectorAll('.primary-nav button').forEach(nav => {
    const selected = nav.dataset.view === view;
    nav.classList.toggle('active', selected);
    if (selected) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current');
  });
  document.querySelectorAll('.view').forEach(panel => panel.classList.toggle('hidden', panel.id !== view));
  $('view-label').textContent = workspaceViews[view];
  document.title = workspaceViews[view] + ' · Application Pipeline';
  $('detail').classList.toggle('hidden', view !== 'jobs' || !currentJobDetail);
  if (updateHistory && window.location.hash !== '#' + view) window.history.pushState({ view }, '', '#' + view);
  if (focus) $('workspace-main').focus({ preventScroll: true });
  if (view === 'settings') loadWorkerSettings();
  window.scrollTo({ top: 0, behavior: 'instant' });
  refreshDashboard().catch(error => setStatus(error.message));
}
function openPipelineLaunch() {
  if (currentView !== 'pipelines') navigateWorkspace('pipelines');
  $('launch-panel').open = true;
  $('launch-panel').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
  $('pipeline-company-name').focus({ preventScroll: true });
}
document.querySelectorAll('.primary-nav button').forEach(nav => nav.addEventListener('click', () => navigateWorkspace(nav.dataset.view)));
['header-launch', 'sidebar-launch', 'hero-launch'].forEach(id => $(id).addEventListener('click', openPipelineLaunch));
$('hero-inbox').addEventListener('click', () => navigateWorkspace('inbox', { focus: true }));
$('review-capacity').addEventListener('click', () => {
  if (currentView !== 'pipelines') navigateWorkspace('pipelines');
  $('capacity-panel').open = true;
  $('capacity-panel').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
});
$('connection-toggle').addEventListener('click', () => {
  $('connection-diagnostics').open = true;
  $('connection-diagnostics').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
});
window.addEventListener('popstate', () => navigateWorkspace(window.location.hash.slice(1), { updateHistory: false }));
$('status-dismiss').addEventListener('click', () => setStatus(''));
$('efficiency-details').addEventListener('toggle', () => {
  if ($('efficiency-details').open) loadEfficiency();
});
$('efficiency-refresh').addEventListener('click', () => loadEfficiency());
$('legacy-question-history').addEventListener('toggle', () => {
  if ($('legacy-question-history').open && currentView === 'inbox') refreshDashboard().catch(error => setStatus(error.message));
  else if (currentView === 'inbox') renderInbox();
});
$('resolved-agent-history').addEventListener('toggle', () => {
  if ($('resolved-agent-history').open && currentView === 'inbox') refreshDashboard().catch(error => setStatus(error.message));
  else if (currentView === 'inbox') renderInbox();
});
$('browser-check-form').addEventListener('submit', async event => {
  event.preventDefault();
  const note = $('browser-connection-note').value.trim();
  if (note.length < 8) { setStatus('Describe the actual browser connection change first.'); return; }
  $('browser-check-submit').disabled = true;
  try {
    await api('browser/check', { method: 'POST', body: JSON.stringify({ connection_change_note: note }) });
    setStatus('The fictional local practice check was requested.');
  } catch (error) { setStatus(error.message); }
  finally { await refreshBrowserStatus(); $('browser-check-submit').disabled = false; }
});
async function bootstrap() {
  const session = await api('session');
  token = session.token;
  const initialView = window.location.hash.slice(1);
  if (Object.hasOwn(workspaceViews, initialView) && initialView !== currentView) {
    currentView = initialView;
    document.querySelectorAll('.primary-nav button').forEach(nav => {
      const selected = nav.dataset.view === initialView;
      nav.classList.toggle('active', selected);
      if (selected) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current');
    });
    document.querySelectorAll('.view').forEach(view => view.classList.toggle('hidden', view.id !== initialView));
    $('view-label').textContent = workspaceViews[initialView];
    document.title = workspaceViews[initialView] + ' · Application Pipeline';
  }
  await refreshBrowserStatus();
  await refreshDashboard();
  if (currentView === 'settings') await loadWorkerSettings();
}
bootstrap().catch(error => {
  $('integration-notice').textContent = 'The local pipeline service could not be reached.';
  $('pipeline-list').replaceChildren(element('p', 'Refresh the local service and reload this page.', 'empty-state'));
  setStatus(error.message);
});
