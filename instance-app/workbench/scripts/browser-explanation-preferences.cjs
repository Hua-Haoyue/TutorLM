/* Run only against a disposable local backend with the offline fake provider. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8094';

(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  let documentId;
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin + '/index.html');
    assert.equal(await page.evaluate(() => RuntimeAPI.detect()), false, 'Use a disposable standalone backend');
    assert.equal((await page.evaluate(() => RuntimeAPI.get('/api/runtime', { auth: false }))).provider.name, 'fake');
    await page.waitForFunction(() => !document.querySelector('#invite-field').hidden);
    await page.fill('#invite', process.env.APP_INVITE || 'TLM-DEV-LOCAL');
    await page.click('#start');
    await page.waitForURL('**/workspace.html');
    await page.waitForFunction(() => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
    const nodeId = await page.evaluate(async () => {
      const project = window.TLM_WS.state.project.id;
      const { document } = await RuntimeAPI.post('/api/projects/' + project + '/documents', {
        title: 'Style regression', text: '# 推导\n\n设 b 不等于零。由 a/b=c 可得 a=bc。',
      });
      const node = await RuntimeAPI.post('/api/projects/' + project + '/nodes', {
        document_id: document.id, title: '为什么可以相乘？', content: '由 a/b=c 可得 a=bc。',
      });
      return { id: node.id, documentId: document.id };
    });
    documentId = nodeId.documentId;
    await page.reload();
    await page.waitForSelector('.gnode');
    await page.evaluate(id => window.TLM_WS.select(id), nodeId.id);
    for (const style of ['plain', 'rigorous', 'analogy', 'example', 'socratic', 'auto']) {
      await page.click('#prefs-btn');
      await page.click(`[data-pref="style"] [data-val="${style}"]`);
      await page.click('[data-pref="length"] [data-val="short"]');
      await page.getByRole('button', { name: '完成', exact: true }).click();
      const response = page.waitForResponse(response => response.url().endsWith('/api/runs') && response.request().method() === 'POST');
      await page.locator(`.gnode[data-id="${nodeId.id}"] [data-act="node-explain"]`).click();
      const posted = (await response).request().postDataJSON();
      assert.equal(posted.input.preferences.style, style);
      assert.equal(posted.input.preferences.length, 'short');
      assert.equal(posted.input.question, undefined);
      await page.waitForFunction(({ id, style }) => {
        const exp = window.TLM_WS.state.history[id]?.[0];
        return exp?.learning?.context?.explanation_preferences?.style === style;
      }, { id: nodeId.id, style });
      const policy = await page.evaluate(id => window.TLM_WS.state.history[id][0].learning.context.explanation_policy, nodeId.id);
      assert.equal(policy.depth_limited, ['plain', 'analogy'].includes(style));
      console.log('PASS settings -> request -> stored explanation:', style);
    }
    assert.deepEqual(errors, []);
  } finally {
    if (documentId) await page.evaluate(id => RuntimeAPI.del('/api/documents/' + id), documentId);
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
