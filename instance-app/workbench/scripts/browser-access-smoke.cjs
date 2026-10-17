/* Opt-in against a disposable loopback managed stack; never calls a model. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL;
assert.ok(origin && ['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Set APP_URL to a disposable loopback stack');
const invite = process.env.APP_INVITE || '';

(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const contexts = [];
  try {
    async function client(viewport) {
      const context = await browser.newContext({ viewport }); contexts.push(context);
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      return { page, errors };
    }
    async function enter(page, share = '') {
      await page.goto(origin, { waitUntil: 'networkidle' });
      if (invite) {
        let allocations = 0;
        const observe = request => { if (request.method() === 'POST' && request.url().endsWith('/control/leases')) allocations++; };
        page.on('request', observe);
        await page.locator('#share-code-input').fill(share);
        assert.equal(await page.locator('#invite').getAttribute('required') !== null, true);
        await page.locator('#start').click();
        assert.equal(allocations, 0, 'required invitation prevents allocation, including share entry');
        page.off('request', observe);
      }
      await page.locator('#invite').fill(invite);
      await page.locator('#share-code-input').fill(share);
      await page.locator('#start').click();
      await page.waitForURL('**/workspace.html', { timeout: 90000 });
      await page.waitForFunction(() => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
      const close = page.locator('#modal-host [data-close]');
      if (await close.count()) await close.click();
    }
    const owner = await client({ width: 1440, height: 960 });
    await enter(owner.page);
    assert.equal(await owner.page.evaluate(() => RuntimeAPI.runtime.provider.name), 'fake');
    const doc = await owner.page.evaluate(async () => {
      const S = TLM_WS.state;
      return (await RuntimeAPI.post(`/api/projects/${S.project.id}/documents`, { title: 'Browser share source', text: '# Browser copy test' })).document;
    });
    await owner.page.locator('#share-btn').click();
    await owner.page.locator('#share-code').waitFor();
    const code = await owner.page.locator('#share-code').inputValue();
    assert.ok(code.startsWith('TLM-SHARE-'));
    assert.equal(await owner.page.locator('#share-link,#share-qr').count(), 0);
    const output = path.resolve(__dirname, '../.work/access-smoke'); fs.mkdirSync(output, { recursive: true });
    await owner.page.screenshot({ path: path.join(output, 'share-desktop.png') });
    const receiver = await client({ width: 390, height: 844 });
    await enter(receiver.page, code);
    await receiver.page.waitForFunction(id => window.TLM_WS.state.docs.some(doc => doc.id === id), doc.id);
    await receiver.page.waitForFunction(id => window.TLM_WS.state.doc?.id === id && document.querySelector('#sheet').textContent.includes('Browser copy test'), doc.id);
    const copy = await receiver.page.evaluate(id => RuntimeAPI.get(`/api/documents/${id}`), doc.id);
    assert.equal(copy.document.title, doc.title);
    await receiver.page.evaluate(id => RuntimeAPI.patch(`/api/documents/${id}`, { title: 'Receiver edit' }), doc.id);
    await receiver.page.screenshot({ path: path.join(output, 'copy-mobile.png') });
    await enter(owner.page);
    const unchanged = await owner.page.evaluate(id => RuntimeAPI.get(`/api/documents/${id}`), doc.id);
    assert.equal(unchanged.document.title, doc.title);
    assert.notEqual(await owner.page.evaluate(() => RuntimeAPI.lease.workspace_id), await receiver.page.evaluate(() => RuntimeAPI.lease.workspace_id));
    assert.deepEqual(owner.errors, []); assert.deepEqual(receiver.errors, []);
    console.log(`PASS: browser ${invite ? 'invitation-required' : 'no-code'} entry, share button, string-only dialog, independent receiver import, isolated edits, desktop/mobile`);
  } catch (error) {
    for (const context of contexts) for (const page of context.pages()) {
      console.error('Failure URL:', page.url(), 'Share state:', await page.locator('#share-body').innerText().catch(() => 'No share dialog'));
    }
    throw error;
  } finally {
    for (const context of contexts) {
      for (const page of context.pages()) {
        await page.evaluate(async () => { if (window.RuntimeAPI?.lease) await RuntimeAPI.release(); }).catch(() => {});
      }
      await context.close();
    }
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
