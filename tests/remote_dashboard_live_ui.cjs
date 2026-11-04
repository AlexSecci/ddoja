// Public HTTPS acceptance using a disposable owner and read-only upstream.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(path.resolve('browser-runtime/node_modules/playwright'));
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const phase = process.argv[3];
const keepAlive = setInterval(() => {}, 1000);
(async () => {
  const browser = await chromium.launch({executablePath: config.browser, headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 390, height: 844}, isMobile: true,
      hasTouch: true, ...(phase === 'resume' ? {storageState: config.storage} : {})});
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [], responses = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      const url = new URL(response.url());
      if (url.pathname.startsWith('/api/')) responses.push({path: url.pathname, status: response.status()});
    });
    if (phase === 'setup') {
      const anonymous = await context.request.get(config.url + '/api/session');
      assert.equal(anonymous.status(), 401);
      await page.goto(config.url + '/setup#' + config.setup_token);
      assert.equal(new URL(page.url()).hash, '');
      await page.locator('#username').fill(config.username);
      await page.locator('#password').fill(config.password);
      await page.getByRole('button', {name: 'Create account', exact: true}).click();
      await page.waitForURL(config.url + '/');
    } else {
      await page.goto(config.url + '/');
      assert.equal(new URL(page.url()).pathname, '/');
    }
    await page.locator('#pipeline-list .pipeline-card').first().waitFor();
    assert(await page.locator('#remote-signout').isVisible());
    for (let i = 0; i < 2; i++) {
      await page.reload();
      await page.locator('#pipeline-list .pipeline-card').first().waitFor();
      assert.equal(new URL(page.url()).pathname, '/');
    }
    const apple = page.locator('#pipeline-list .pipeline-card').filter({has: page.getByRole('heading', {name: 'Apple', exact: true})});
    assert.equal(await apple.count(), 1);
    const [workerResponse] = await Promise.all([
      page.waitForResponse(r => /\/api\/pipelines\/[^/]+\/workers$/.test(new URL(r.url()).pathname)),
      apple.locator('.pipeline-workers > summary').click(),
    ]);
    assert.equal(workerResponse.status(), 200);
    const [jobsResponse] = await Promise.all([
      page.waitForResponse(r => /\/api\/pipelines\/[^/]+\/jobs$/.test(new URL(r.url()).pathname)),
      apple.getByRole('button', {name: 'View job records', exact: true}).click(),
    ]);
    assert.equal(jobsResponse.status(), 200);
    await apple.locator('.pipeline-jobs table').waitFor();
    await page.getByRole('button', {name: /All jobs/}).click();
    await page.locator('#job-rows tr').first().waitFor();
    await page.locator('#jobs-company').selectOption({label: 'Apple'});
    await page.locator('#jobs-state').selectOption('scored');
    await page.locator('#jobs-order').selectOption('company');
    assert((await page.locator('#job-rows tr').count()) > 0);
    const staleTokenRecovery = await page.evaluate(async () => {
      token = 'synthetic-expired-local-session';
      return Array.isArray(await api('pipelines'));
    });
    assert(staleTokenRecovery);
    const cookies = await context.cookies();
    const cookie = cookies.find(c => c.name === '__Host-ddoja-session');
    assert(cookie && cookie.secure && cookie.httpOnly && cookie.sameSite === 'Lax');
    assert(cookie.expires > Date.now() / 1000 + 89 * 86400);
    if (phase === 'setup') {
      await context.storageState({path: config.storage});
    } else {
      await page.locator('#remote-signout').click();
      await page.waitForURL(config.url + '/login');
      assert.equal((await context.request.get(config.url + '/api/session')).status(), 401);
      await page.locator('#username').fill(config.username);
      await page.locator('#password').fill(config.password);
      await page.getByRole('button', {name: 'Sign in', exact: true}).click();
      await page.waitForURL(config.url + '/');
      await page.locator('#pipeline-list .pipeline-card').first().waitFor();
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed: true, phase, mobile: true, pipelineDataLoaded: true,
      workersLoaded: true, jobRecordsLoaded: true, allJobsFiltering: true,
      staleLocalTokenRecovered: true, cookieSecureHttpOnly: true, cookieDays: 90,
      logoutAndLogin: phase === 'resume', responseCount: responses.length,
      failedResponses: responses.filter(r => r.status >= 400)}));
  } finally { await browser.close(); }
})().catch(error => {
  let message = error.message;
  for (const secret of [config.password, config.setup_token]) message = message.split(secret).join('[redacted]');
  console.log(JSON.stringify({passed: false, phase, error: message}));
  process.exitCode = 1;
}).finally(() => clearInterval(keepAlive));
