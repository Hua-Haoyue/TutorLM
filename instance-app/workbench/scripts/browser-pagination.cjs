/* Disposable standalone fake backend only; no real model calls. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8111';

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  let docId;
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin + '/index.html');
    assert.equal((await page.evaluate(() => RuntimeAPI.get('/api/runtime', { auth: false }))).provider.name, 'fake');
    await page.waitForFunction(() => !document.querySelector('#invite-field').hidden);
    await page.fill('#invite', process.env.APP_INVITE || 'TLM-DEV-LOCAL');
    await page.click('#start'); await page.waitForURL('**/workspace.html');
    await page.waitForFunction(() => window.TLM_WS?.state.project && window.TLM_WS.state.connected);
    const fixture = await page.evaluate(async () => {
      const pid = window.TLM_WS.state.project.id;
      const { document } = await RuntimeAPI.post(`/api/projects/${pid}/documents`, { title: 'Pagination regression', text: '# Versions\n\nA fixed passage.' });
      const node = await RuntimeAPI.post(`/api/projects/${pid}/nodes`, { title: 'Versioned node', document_id: document.id, content: 'A fixed passage.' });
      const ids = [];
      for (const question of ['version one', 'version two', 'version three']) {
        ids.push((await RuntimeAPI.run('explanation', { node_id: node.id, question })).explanation.id);
      }
      await RuntimeAPI.patch('/api/nodes/' + node.id, { metadata: { pages: ids, page: 2 } });
      return { documentId: document.id, nodeId: node.id, ids };
    });
    docId = fixture.documentId;
    await page.reload(); await page.waitForSelector('.gnode');
    await page.locator(`[data-doc="${docId}"]`).click();
    await page.evaluate(id => window.TLM_WS.select(id), fixture.nodeId);
    const card = page.locator(`.gnode.sel[data-id="${fixture.nodeId}"]`);
    await card.getByRole('group', { name: '解释版本', exact: true }).waitFor();
    assert.equal(await card.locator('.answer').getAttribute('data-exp'), fixture.ids[2]);
    assert(await card.getByRole('button', { name: '下一版', exact: true }).isDisabled());
    await card.getByRole('button', { name: '上一版', exact: true }).click();
    await page.waitForFunction(id => document.querySelector('.gnode.sel .answer')?.dataset.exp === id, fixture.ids[1]);
    await card.getByRole('button', { name: '上一版', exact: true }).click();
    await page.waitForFunction(id => document.querySelector('.gnode.sel .answer')?.dataset.exp === id, fixture.ids[0]);
    assert(await card.getByRole('button', { name: '上一版', exact: true }).isDisabled());
    await page.waitForFunction(() => document.querySelector('#st-state').textContent === '已同步');
    await page.reload(); await page.waitForSelector('.gnode');
    await page.evaluate(id => window.TLM_WS.select(id), fixture.nodeId);
    await page.waitForFunction(id => document.querySelector('.gnode.sel .answer')?.dataset.exp === id, fixture.ids[0]);
    await card.getByRole('button', { name: '下一版', exact: true }).click();
    await page.waitForFunction(id => document.querySelector('.gnode.sel .answer')?.dataset.exp === id, fixture.ids[1]);
    assert.deepEqual(errors, []);
    console.log('PASS pagination: correct version, boundary buttons, previous/next, reload persistence');
  } finally {
    if (docId) await page.evaluate(id => RuntimeAPI.del('/api/documents/' + id), docId);
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
