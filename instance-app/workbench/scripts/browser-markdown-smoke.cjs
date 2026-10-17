const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8094';
const output = path.resolve(__dirname, '../.work/markdown-smoke');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [], remoteImages = [];
  const documents = [];
  let managed = false, keeper;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.url().includes('should-not-load')) remoteImages.push(request.url()); });
  // Use only extraction/persistence; this test must never call a model.
  await page.route('**/api/runs', route => route.request().method() === 'POST' ? route.abort() : route.continue());
  try {
    await page.goto(origin + '/index.html');
    await page.waitForFunction(() => document.querySelector('#cap-free').textContent !== '\u2013');
    managed = await page.evaluate(() => RuntimeAPI.detect());
    if (await page.locator('#invite-field').isVisible()) {
      if (process.env.APP_INVITE || !managed) await page.fill('#invite', process.env.APP_INVITE || 'TLM-DEV-LOCAL');
    }
    await page.click('#start');
    await page.waitForURL('**/workspace.html', { timeout: 100000 });
    await page.waitForFunction(() => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
    if (managed) {
      // Keep another real tab attached while reload exercises page-close beacons.
      const popup = page.waitForEvent('popup');
      await page.evaluate(() => window.open('workspace.html', '_blank'));
      keeper = await popup;
      await keeper.waitForFunction(() => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
    }
    const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/markdown-support.md'));
    await page.locator('#file-input').setInputFiles({ name: 'markdown-render-test.md', mimeType: 'text/markdown', buffer: fixture });
    await page.waitForFunction(() => window.TLM_WS.state.doc?.source_filename === 'markdown-render-test.md');
    documents.push(await page.evaluate(() => window.TLM_WS.state.doc.id));
    const doc = page.locator('#sheet .doc');
    assert.equal(await doc.locator('table tbody tr').count(), 4);
    assert.equal(await doc.locator('td[style*="right"]').count(), 4);
    assert.equal(await doc.locator('.task-list-item input:disabled').count(), 2);
    assert.equal(await doc.locator('.task-list-item input:checked').count(), 1);
    assert.equal(await doc.locator('pre code.language-python').count(), 1);
    assert.equal(await doc.locator('h6').count(), 1);
    assert.equal(await doc.locator('.math-block .katex').count(), 1);
    assert.equal(await doc.locator('script, iframe, img').count(), 0);
    assert.equal(await page.evaluate(() => window.markdownInjected), undefined);
    assert.deepEqual(remoteImages, []);
    await page.evaluate(() => {
      const cell = [...document.querySelectorAll('#sheet td')].find(cell => cell.textContent === '表格引用目标');
      cell.scrollIntoView({ block: 'center' });
      const range = document.createRange(); range.selectNodeContents(cell);
      getSelection().removeAllRanges(); getSelection().addRange(range);
      document.querySelector('#reader-scroll').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    });
    await page.waitForSelector('#ask:not([hidden])');
    await page.click('#ask [data-act="ask-extract"]');
    await page.waitForFunction(() => window.TLM_WS.state.nodes.some(node => node.metadata?.source?.quote === '表格引用目标'));
    const source = await page.evaluate(() => window.TLM_WS.state.nodes.find(node => node.metadata?.source?.quote === '表格引用目标').metadata.source);
    assert.equal(fixture.toString().slice(source.selector.start, source.selector.end), '表格引用目标');
    const resolved = await page.evaluate(ref => RuntimeAPI.post('/api/sources/resolve', { refs: [ref] }), source);
    assert(resolved.items[0].resolved);
    await page.reload();
    await page.waitForSelector('#sheet td mark.anchor');
    assert.equal(await page.locator('#sheet td mark.anchor').textContent(), '表格引用目标');
    await page.keyboard.press('Control+f');
    await page.fill('#find-input', '表格引用目标');
    await page.waitForSelector('#sheet td mark.find');
    await page.keyboard.press('Escape');
    await page.locator('#reader-scroll').evaluate(el => { el.scrollTop = 0; });
    await page.screenshot({ path: path.join(output, 'desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    assert(await doc.locator('.md-table').evaluate(el => el.scrollWidth >= el.clientWidth));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: Markdown tables, tasks, code, math, safety, source resolution, reload, search, desktop and mobile');
  } finally {
    const cleanup = keeper || page;
    for (const id of documents) await cleanup.evaluate(id => RuntimeAPI.del('/api/documents/' + id), id).catch(error => console.error('Cleanup failed:', error.message));
    if (managed) console.log('Test lease release:', (await page.context().request.delete(origin + '/control/leases/current')).status());
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
