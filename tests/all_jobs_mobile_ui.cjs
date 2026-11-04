// Exercise real dashboard controls against synthetic jobs; refuse API mutations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const {chromium} = require(path.join(root, 'browser-runtime/node_modules/playwright'));
const output = process.argv[2];
const states = ['scored', 'unscored', 'legacy', 'needs_rescoring', 'submitted', 'discarded', 'uncertain', 'excluded', 'active', 'failed'];
const jobs = states.map((state, index) => ({
  id: state, company_id: index % 2 ? 'beta' : 'alpha', company_name: index % 2 ? 'Beta Company' : 'Alpha Company',
  title: state === 'scored' ? 'Software Engineer, Framework Solutions and Distributed AI Data Platforms' : 'Software Engineer ' + state,
  location: state === 'scored' ? 'Cupertino, Austin, Seattle, New York, San Diego, '.repeat(12) : 'Cupertino, California',
  requisition_id: String(1000 + index), url: 'https://example.com/jobs/' + state,
  record_status: state, phase: state === 'submitted' ? 'submitted' : 'new',
  fit_current: !['unscored', 'legacy', 'needs_rescoring', 'failed'].includes(state),
  fit_score: state === 'discarded' ? 0 : 100 - index,
  manual_submitted_available: state === 'scored', manual_discard_available: state === 'scored',
  document_generation_available: state === 'scored',
  application_provenance: state === 'scored' ? 'A lengthy saved record explanation. '.repeat(60) : 'Saved record',
  document_request: state === 'scored' ? {state: 'completed', current: true, artifacts: [
    {id: 'resume-artifact', kind: 'resume'}, {id: 'cover-artifact', kind: 'cover_letter'}]} : null,
}));
// Include a terminal record without a score and enough records to page.
jobs.push({...jobs[4], id: 'submitted-unscored', fit_current: false, fit_score: null});
for (let i = 0; i < 16; i++) jobs.push({...jobs[0], id: 'extra-' + i, title: 'Additional Engineer ' + i,
  fit_score: 80 - i, location: 'Austin', document_request: null});
let mutations = 0;
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (!['GET', 'HEAD'].includes(request.method)) {
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
  else if (url.pathname === '/api/pipelines') value = [];
  else if (url.pathname === '/api/jobs') value = jobs;
  else if (url.pathname === '/api/agent-quota') value = {paused: false};
  response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(value));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + server.address().port);
    await page.locator('[data-view="jobs"]').click();
    await page.locator('.all-job-row').first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('.summary-card').filter({hasText: 'Scored jobs'}).locator('strong').innerText(),
      String(jobs.filter(job => job.record_status === 'scored').length), 'Summary agrees with the Scored status filter');
    const ids = () => page.locator('.all-job-row').evaluateAll(rows => rows.map(row => row.dataset.jobId));
    for (const state of states) {
      await page.selectOption('#jobs-state', state);
      const expected = jobs.filter(job => job.record_status === state).sort((a, b) => (b.fit_current ? b.fit_score : -1) - (a.fit_current ? a.fit_score : -1));
      assert.deepEqual(await ids(), expected.map(job => job.id), state + ' must match the displayed status');
      await page.evaluate(() => refreshDashboard());
      assert.equal(await page.inputValue('#jobs-state'), state);
      assert.deepEqual(await ids(), expected.map(job => job.id), state + ' survives refresh');
    }
    await page.selectOption('#jobs-state', 'scored');
    await page.selectOption('#jobs-company', 'beta');
    assert.equal(await page.locator('.all-job-row').count(), 0, 'Company and status filters combine');
    assert.equal(await page.locator('#jobs-result-count').innerText(), 'No matching records');
    await page.selectOption('#jobs-state', 'all');
    await page.selectOption('#jobs-company', '');
    await page.locator('#search').fill('1000');
    assert((await ids()).every(id => id === 'scored' || id.startsWith('extra-')));
    await page.locator('#search').fill('');
    await page.selectOption('#jobs-order', 'score_asc');
    assert.equal((await ids())[0], 'discarded', 'A zero score is retained and sorts first');
    await page.selectOption('#jobs-order', 'company');
    assert.deepEqual(await ids(), jobs.slice().sort((a, b) => a.company_name.localeCompare(b.company_name)
      || (b.fit_current ? b.fit_score : -1) - (a.fit_current ? a.fit_score : -1)).slice(0, 20).map(job => job.id));
    await page.selectOption('#jobs-order', 'score_desc');
    await page.locator('#jobs-next').click();
    assert.equal(await page.locator('#jobs-page-number').innerText(), 'Page 2 of 2');
    await page.selectOption('#jobs-state', 'submitted');
    assert.equal(await page.locator('#jobs-page-number').innerText(), 'Page 1 of 1', 'Changing filters resets the page');
    await page.selectOption('#jobs-state', 'all');
    const measured = [];
    for (const width of [320, 390, 768]) {
      await page.setViewportSize({width, height: 844});
      const dimensions = await page.locator('.all-job-row').evaluateAll(rows => rows.map(row => {
        const rect = row.getBoundingClientRect(); return {height: rect.height, left: rect.left, right: rect.right};
      }));
      assert(dimensions.every(row => row.height < 400), 'Collapsed records should occupy less than half a phone screen');
      assert(dimensions.every(row => row.left >= 0 && row.right <= width + 1), 'Cards fit the viewport');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No page horizontal overflow');
      for (const selector of ['#search', '#jobs-company', '#jobs-state', '#jobs-order', '#refresh']) {
        const rect = await page.locator(selector).boundingBox();
        assert(rect.x >= 0 && rect.x + rect.width <= width + 1 && rect.height >= 44, selector + ' is reachable and touch-sized');
      }
      measured.push({width, maxCardHeight: Math.max(...dimensions.map(row => row.height))});
      if (output && width === 390) {
        fs.mkdirSync(output, {recursive: true});
        await page.screenshot({path: path.join(output, 'all-jobs-phone.png')});
        await page.locator('.all-job-row').first().screenshot({path: path.join(output, 'all-jobs-card.png')});
      }
    }
    await page.setViewportSize({width: 390, height: 844});
    const first = page.locator('.all-job-row[data-job-id="scored"]');
    assert(await first.locator('.all-job-mobile-summary').getByText('100/100', {exact: true}).isVisible());
    await first.locator('.all-job-toggle').click();
    for (const suffix of ['score', 'location', 'state', 'progress', 'actions']) {
      assert(await page.locator('#all-job-scored-' + suffix).isVisible(), suffix + ' is reachable');
    }
    await page.evaluate(() => refreshDashboard());
    assert.equal(await first.locator('.all-job-toggle').getAttribute('aria-expanded'), 'true');
    assert(await first.getByRole('button', {name: 'Download résumé', exact: true}).isVisible());
    assert(await first.getByRole('button', {name: 'Download cover letter', exact: true}).isVisible());
    let confirmations = 0;
    page.on('dialog', async dialog => { confirmations++; await dialog.dismiss(); });
    await first.getByRole('button', {name: /^Discard:/}).click();
    await first.getByRole('button', {name: /^Mark submitted:/}).click();
    assert.equal(confirmations, 2, 'Manual decisions still require confirmation');
    await page.selectOption('#jobs-state', 'submitted');
    await page.selectOption('#jobs-state', 'scored');
    assert.equal(await first.locator('.all-job-toggle').getAttribute('aria-expanded'), 'true', 'Filters preserve expansion');
    await first.locator('.all-job-toggle').click();
    await page.setViewportSize({width: 1440, height: 1000});
    assert.equal(await page.locator('.all-job-table').evaluate(node => getComputedStyle(node).display), 'table');
    assert.equal(await page.locator('.all-job-table thead th').count(), 6);
    assert(await first.locator('td').last().isVisible());
    assert.equal(await first.locator('.all-job-toggle').isVisible(), false);
    assert.deepEqual(errors, []); assert.equal(mutations, 0);
    console.log(JSON.stringify({passed: true, allStatesFiltered: true, paginationAndOrdering: true, measured,
      expandedFieldsReachable: true, expansionSurvivesRefresh: true, desktopTable: true, mutations}));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
