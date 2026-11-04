// Render the actual dashboard against synthetic records; no real API mutations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const {chromium} = require(path.join(root, 'browser-runtime/node_modules/playwright'));
const output = process.argv[2];
const pid = 'mobile-fixture';
const rationale = 'Documented C++ and automation experience matches this requirement; missing production skills still reduce the score. '.repeat(35);
const jobs = [
  {id: 'scored', title: 'Software Engineer, Framework Solutions and Distributed AI Data Platforms',
    fit_current: true, fit_score: 88, agent_reason: rationale,
    fit_assessment: {eligibility: 'eligible', confidence: 'high', requirements: [
      {requirement: 'Build reliable distributed C++ services', importance: 'required', match: 'mostly_satisfied', evidence: 'Confirmed systems programming and automation experience.'}]},
    profile_refresh: {eligible: true}},
  {id: 'unscored', title: 'Software Engineer, Data Infrastructure', fit_current: false,
    location: 'Cupertino, Austin, Seattle, New York, San Diego, '.repeat(12)},
  {id: 'uncertain', title: 'Infrastructure Engineer', fit_current: true, fit_score: 52,
    run_status: 'submission_uncertain', submission_uncertain: true, manual_discard_available: false},
  {id: 'legacy', title: 'Legacy Evaluation Engineer', fit_current: false, agent_decision: 'approve'},
].map(job => ({company_id: 'fixture-company', company_name: 'Fixture Company', location: 'Cupertino, California',
  country: 'US', employment: 'full_time', arrangement: 'hybrid', selected: true,
  phase: 'new', run_status: 'not_submitted', url: 'https://example.com/job/' + job.id,
  manual_submitted_available: true, manual_discard_available: true, ...job}));
const view = {pipeline: {id: pid, mode: 'agentic', company_name: 'Fixture Company', status: 'stopped',
  stop_requested: true, stage: 'stopped', discovery_paused: true, evaluation_paused: true,
  submission_paused: true, filters: {}, counts: {discovered: 4, selected: 4, evaluated: 2, scored: 2, submitted: 0}},
  workers: [], quota: {paused: false}};
let mutations = 0;
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    mutations++; response.writeHead(405); return response.end('Fixture refuses mutations');
  }
  if (url.pathname === '/' || url.pathname.startsWith('/static/')) {
    const name = url.pathname === '/' ? 'index.html' : path.basename(url.pathname);
    const file = path.join(root, 'app/static', name);
    if (!fs.existsSync(file)) { response.writeHead(404); return response.end(); }
    response.setHeader('Content-Type', name.endsWith('.js') ? 'application/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.html') ? 'text/html' : 'application/octet-stream');
    return response.end(fs.readFileSync(file));
  }
  let value = {};
  if (url.pathname === '/api/session') value = {token: 'synthetic-ui-session'};
  else if (url.pathname === '/api/health') value = {status: 'ok', pipeline_release: 'agentic_pipeline_v1'};
  else if (url.pathname === '/api/pipelines') value = [view];
  else if (url.pathname.endsWith('/jobs')) value = {jobs};
  else if (url.pathname.endsWith('/inbox')) value = {items: []};
  else if (url.pathname === '/api/agent-quota') value = {paused: false};
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify(value));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + server.address().port);
    await page.getByRole('button', {name: 'View job records', exact: true}).click();
    await page.locator('.pipeline-job-row').first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    const measured = [];
    for (const width of [320, 390, 768]) {
      await page.setViewportSize({width, height: 844});
      const dimensions = await page.locator('.pipeline-job-row').evaluateAll(rows => rows.map(row => {
        const rect = row.getBoundingClientRect();
        return {height: rect.height, left: rect.left, right: rect.right};
      }));
      assert(dimensions.every(row => row.height < 350), 'Collapsed records should occupy less than half a phone screen');
      assert(dimensions.every(row => row.left >= 0 && row.right <= width + 1), 'Cards must fit the viewport');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'The page must not overflow horizontally');
      measured.push({width, maxCardHeight: Math.max(...dimensions.map(row => row.height))});
      if (output && width === 390) {
        fs.mkdirSync(output, {recursive: true});
        await page.locator('.pipeline-job-records').screenshot({path: path.join(output, 'phone-records.png')});
      }
    }
    await page.setViewportSize({width: 390, height: 844});
    const scored = page.locator('[data-job-id="scored"]');
    assert(await scored.locator('.pipeline-job-mobile-summary').getByText('88 / 100', {exact: true}).isVisible());
    const toggle = scored.getByRole('button', {name: 'Show details for ' + jobs[0].title, exact: true});
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    await toggle.click();
    for (const suffix of ['fit', 'state', 'actions']) {
      assert(await page.locator('#pipeline-job-' + pid + '-scored-' + suffix).isVisible());
    }
    const note = scored.locator('textarea[name="provenance"]');
    await note.fill('Confirmed factual profile update; preserve this draft.');
    await page.evaluate(() => refreshDashboard());
    assert.equal(await note.inputValue(), 'Confirmed factual profile update; preserve this draft.');
    assert.equal(await scored.locator('.pipeline-job-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(await scored.locator('.pipeline-job-rationale').getAttribute('open'), null);
    await scored.locator('.pipeline-job-rationale > summary').click();
    await page.evaluate(() => refreshDashboard());
    assert(await scored.locator('.pipeline-job-rationale').evaluate(node => node.open));
    let confirmations = 0;
    page.on('dialog', async dialog => { confirmations++; await dialog.dismiss(); });
    await scored.getByRole('button', {name: /^Discard:/}).click();
    assert.equal(confirmations, 1, 'Discard retains its human confirmation');
    assert.equal(mutations, 0, 'Expanding and refreshing records must not send a mutation');
    await scored.locator('.pipeline-job-toggle').click();
    assert.equal(await scored.locator('.pipeline-job-toggle').getAttribute('aria-expanded'), 'false');
    await page.setViewportSize({width: 1440, height: 1000});
    assert.equal(await page.locator('.pipeline-job-table').evaluate(node => getComputedStyle(node).display), 'table');
    assert.equal(await page.locator('.pipeline-job-table thead th').count(), 4);
    assert(await scored.locator('td').last().isVisible());
    assert.equal(await scored.locator('.pipeline-job-toggle').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed: true, measured, desktopTable: true, expandedFieldsReachable: true,
      refreshPreservesExpansionAndDraft: true, manualConfirmationPreserved: true, mutations}));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
