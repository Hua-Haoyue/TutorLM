/* Feynman check in the browser against a real fake-provider instance app (no interception):
   root node → 优化解释 → 费曼验证 → pick an answer. APP_URL (default http://127.0.0.1:8094),
   APP_INVITE (default TLM-DEV-LOCAL), PLAYWRIGHT_MODULE (optional). Creates and deletes one document. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8094';
const invite = process.env.APP_INVITE || 'TLM-DEV-LOCAL';

(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage();
  page.setDefaultTimeout(25000);
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  let docId;
  try {
    await page.goto(origin + '/index.html');
    await page.waitForFunction(() => document.querySelector('#cap-free')?.textContent !== '–');
    if (await page.isVisible('#invite-field')) await page.fill('#invite', invite);
    await page.click('#start'); await page.waitForURL('**/workspace.html');
    await page.waitForFunction(() => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
    await page.waitForLoadState('networkidle');
    const ids = await page.evaluate(async () => {
      const api = window.RuntimeAPI, S = window.TLM_WS.state;
      const d = (await api.post(`/api/projects/${S.project.id}/documents`, { title: 'feynman-' + Date.now(), text: '# F\n\nGravity pulls masses together.\n' })).document;
      const n = await api.post(`/api/projects/${S.project.id}/nodes`, { document_id: d.id, title: 'Gravity', kind: 'concept', content: 'Gravity', metadata: { source: { quote: 'Gravity pulls masses together.' } } });
      await api.run('explanation', { node_id: n.id });
      return { doc: d.id, node: n.id };
    });
    docId = ids.doc;
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.TLM_WS?.state.connected);
    await page.click(`[data-doc="${ids.doc}"]`);
    await page.waitForSelector(`.gnode[data-id="${ids.node}"]`);
    await page.evaluate((id) => window.TLM_WS.select(id), ids.node);
    const card = `.gnode.sel[data-id="${ids.node}"]`;
    // A root node without children: 优化解释 runs the explanation workflow with purpose=optimize.
    await page.click(`${card} [data-act="node-optimize"]`);
    await page.waitForSelector(`${card} [data-act="node-feynman"]`);
    await page.click(`${card} [data-act="node-feynman"]`);
    await page.waitForSelector(`${card} [data-feynman-choice]`);
    assert.equal(await page.locator(`${card} [data-feynman-choice]`).count(), 2);
    assert.equal(await page.locator(`${card} .feynman`).innerText().then(t => t.includes('笨学生 AI 向你确认')), false);
    const statement = await page.locator(`${card} .feynman-statement`).innerText();
    await page.getByRole('button', { name: '关闭费曼验证', exact: true }).click();
    assert.equal(await page.locator(`${card} .feynman`).count(), 0);
    await page.click(`${card} [data-act="node-feynman"]`);
    assert.equal(await page.locator(`${card} .feynman-statement`).innerText(), statement);
    await page.locator(`${card} [data-feynman-choice]`, { hasText: '前提与结论有方向' }).click();
    await page.waitForSelector(`${card} .feynman--passed`);
    let newQuestions = 0;
    page.on('request', request => {
      if (request.method() === 'POST' && request.url().endsWith('/api/runs') && request.postDataJSON()?.kind === 'feynman') newQuestions++;
    });
    await page.getByRole('button', { name: '关闭费曼验证', exact: true }).click();
    await page.getByRole('button', { name: '已通过费曼验证', exact: true }).click();
    await page.waitForSelector(`${card} .feynman--passed`);
    assert.equal(newQuestions, 0);
    console.log('PASS optimize → Feynman quiz → correct answer');
    assert.deepEqual(errors, []);
    console.log('OK');
  } finally {
    if (docId) await page.evaluate(async (id) => window.RuntimeAPI.del(`/api/documents/${id}`), docId).catch(() => {});
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
