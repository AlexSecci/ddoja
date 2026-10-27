// Real browser test of the shipped settings controls against a synthetic API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const { chromium } = require(path.join(root, 'browser-runtime/node_modules/playwright'));
let saved = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let saves = 0;
const section = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8').split('    <section id="settings"')[1].split('    <section id="detail"')[0];
const html = '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/theme.css"></head><body><main><section id="settings"' + section + '</main><script>async function api(p,o={}){const r=await fetch("/api/"+p,{...o,headers:{"Content-Type":"application/json"}});if(!r.ok)throw Error("Fixture error");return r.json();}</script><script src="/static/settings.js"></script><script>loadWorkerSettings();</script></body></html>';
const server = http.createServer(async (req, res) => {
  if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(html); }
  if (req.url.startsWith('/static/')) {
    const file = path.join(root, 'app/static', path.basename(req.url));
    if (fs.existsSync(file)) { res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream'); return res.end(fs.readFileSync(file)); }
  }
  if (req.url === '/api/settings/workers') {
    if (req.method === 'PUT') {
      let body = ''; for await (const part of req) body += part;
      const value = JSON.parse(body); assert.equal(value.revision, saved.revision);
      saved = { ...saved, roles: value.roles, revision: saved.revision + 1 }; saves++;
    }
    res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify(saved));
  }
  res.statusCode = 404; res.end();
});
(async () => {
  let browser;
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    browser = await chromium.launch({ executablePath: saved.playwright.browser, headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
    page.setDefaultTimeout(5000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + server.address().port);
    await page.locator('#settings').evaluate(node => node.classList.remove('hidden'));
    await page.getByRole('button', { name: 'Save worker settings' }).waitFor();
    const evaluation = page.getByRole('group', { name: 'Evaluation', exact: true });
    await evaluation.getByLabel('Provider', { exact: true }).selectOption('deepseek');
    assert.deepEqual(await evaluation.getByLabel('Reasoning effort').locator('option').evaluateAll(nodes => nodes.map(n => n.value)), ['low', 'high', 'max']);
    assert.equal(await evaluation.getByLabel('Browser', { exact: true }).inputValue(), 'playwright');
    await evaluation.getByLabel('Reasoning effort').selectOption('low');
    await page.getByRole('button', { name: 'Save worker settings' }).click();
    await page.getByText('Saved. New invocations use these choices;', { exact: false }).waitFor();
    assert.equal(saved.roles.evaluation.provider, 'deepseek');
    assert.equal(saved.roles.evaluation.effort, 'low');
    assert.equal(saved.roles.materials.provider, 'openai');
    await page.reload();
    await page.locator('#settings').evaluate(node => node.classList.remove('hidden'));
    await page.getByText('Saved revision 1.', { exact: true }).waitFor();
    assert.equal(await evaluation.getByLabel('Provider', { exact: true }).inputValue(), 'deepseek');
    await evaluation.getByLabel('Provider', { exact: true }).selectOption('openai');
    await page.getByRole('button', { name: 'Save worker settings' }).click();
    await page.getByText('Saved. New invocations use these choices;', { exact: false }).waitFor();
    assert.equal(saved.roles.evaluation.provider, 'openai');
    assert.equal(saves, 2); assert.deepEqual(errors, []);
    await page.screenshot({ path: process.argv[3], fullPage: true });
    console.log(JSON.stringify({ passed: true, saves, page_errors: errors.length }));
  } catch (error) {
    if (browser) {
      const page = browser.contexts()[0]?.pages()[0];
      if (page) {
        await page.screenshot({ path: process.argv[3], fullPage: true });
        error.message += ' · Settings status: ' + await page.locator('#settings-status').textContent();
      }
    }
    throw error;
  } finally { if (browser) await browser.close(); server.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
