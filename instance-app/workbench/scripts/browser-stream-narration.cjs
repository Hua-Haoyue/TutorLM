/* Streaming explanation text, web reference links, and a narration panel that keeps
   generating in the background, reopens at any time and can be enlarged.
   Real fake-provider instance; run polls are slowed / enriched by interception
   (the fake provider neither streams nor searches). APP_URL (default
   http://127.0.0.1:8182), APP_INVITE (default TLM-DEV-LOCAL), PLAYWRIGHT_MODULE. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8182';
const invite = process.env.APP_INVITE || 'TLM-DEV-LOCAL';
const output = path.resolve(__dirname, '../.work/stream-narration');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage();
  page.setDefaultTimeout(20000);
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
      const doc = (await api.post(`/api/projects/${S.project.id}/documents`, { title: 'stream-' + Date.now(), text: '# 矩阵\n\n特征值描述矩阵在特征方向上的伸缩。' })).document;
      const n = await api.post(`/api/projects/${S.project.id}/nodes`, { document_id: doc.id, title: '特征值', kind: 'concept', content: '特征值',
        metadata: { source: { quote: '特征值' }, pages: [] } });
      return { doc: doc.id, node: n.id };
    });
    docId = ids.doc;
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.TLM_WS?.state.connected);
    await page.click(`[data-doc="${ids.doc}"]`);
    await page.waitForSelector(`.gnode[data-id="${ids.node}"]`);

    // Slow every run down and stream partial text for explanations; enrich the final one with web pages.
    const polls = new Map(), kinds = new Map();
    await page.route('**/api/runs/*', async (route) => {
      const req = route.request();
      if (req.method() !== 'GET' || /\/api\/runs\/[^/]+\/(events|cancel)/.test(req.url())) return route.continue();
      const res = await route.fetch(), body = await res.json();
      kinds.set(body.id, body.kind);
      const k = (polls.get(body.id) || 0) + 1; polls.set(body.id, k);
      const hold = body.kind === 'narration' ? 14 : 6;
      if (k <= hold) {
        delete body.result;
        Object.assign(body, { state: 'running', progress: body.kind === 'narration' ? k * 6 : undefined });
        if (body.kind === 'explanation') body.partial = '特征值是方阵 $A$ 满足 **流式** 第' + k + '段';
      } else if (body.kind === 'explanation' && body.result?.explanation) {
        body.result.explanation.learning = { ...(body.result.explanation.learning || {}),
          web_sources: [{ uri: 'https://zh.wikipedia.org/wiki/特征值', title: '特征值 - 维基百科' }, { uri: 'javascript:alert(1)', title: 'bad' }] };
      }
      await route.fulfill({ response: res, json: body });
    });

    // 1. Streaming in the card, then the final answer with reference links.
    await page.click(`.gnode[data-id="${ids.node}"] h4`);
    await page.click(`.gnode.sel[data-id="${ids.node}"] [data-act="node-explain"]`);
    await page.waitForSelector(`.gnode.sel[data-id="${ids.node}"] .gen-stream`);
    const first = await page.textContent(`.gnode.sel[data-id="${ids.node}"] .gen-stream`);
    await page.waitForFunction((id) => /第[3-6]段/.test(document.querySelector(`.gnode.sel[data-id="${id}"] .gen-stream`)?.textContent || ''), ids.node);
    assert.match(first, /特征值是方阵/);
    assert(await page.locator(`.gnode.sel[data-id="${ids.node}"] .gen-stream .katex, .gnode.sel[data-id="${ids.node}"] .gen-stream strong`).count() >= 1, 'streamed markdown is rendered');
    await page.screenshot({ path: path.join(output, 'streaming.png') });
    await page.waitForSelector(`.gnode.sel[data-id="${ids.node}"] .xpl__sources a`);
    assert.equal(await page.locator(`.gnode.sel[data-id="${ids.node}"] .gen-stream`).count(), 0, 'stream replaced by the answer');
    const links = await page.$$eval(`.gnode.sel[data-id="${ids.node}"] .xpl__sources a`, (as) => as.map((a) => [a.href, a.rel, a.target, a.textContent]));
    assert.equal(links.length, 1);
    assert(links[0][0].startsWith('https://zh.wikipedia.org/') && links[0][1].includes('noopener') && links[0][2] === '_blank');
    console.log('PASS streaming explanation + reference links');

    // 2. Narration keeps running after the panel closes; status and text reopen it; it enlarges.
    await page.click('#inspector [data-act="narrate"]');
    await page.waitForSelector('#narr-go');
    await page.click('#narr-go');
    await page.waitForFunction(() => document.querySelector('#narr-go')?.disabled && !document.querySelector('#narr-run').hidden);
    await page.keyboard.press('Escape');
    await page.waitForSelector('#inspector .inspector__narration-status');
    await page.click('#inspector .inspector__narration-status');
    await page.waitForSelector('#narr-run:not([hidden])');
    assert(await page.isDisabled('#narr-go'), 'reopened panel shows the running job');
    assert.match(await page.textContent('#narr-run'), /生成中|排队中/);
    await page.click('#narr-max');
    const box = await page.$eval('#modal-host .modal', (m) => [m.classList.contains('modal--max'), m.getBoundingClientRect().width / innerWidth, m.getBoundingClientRect().height / innerHeight]);
    assert(box[0] && box[1] >= 0.85 && box[2] >= 0.9, 'enlarged: ' + box);
    await page.waitForFunction(() => document.querySelector('#narr-run').hidden && document.querySelector('#narr-prev').textContent.trim().length > 0, null, { timeout: 40000 })
      .catch(async (e) => { console.log('toasts', await page.$$eval('.toast', (t) => t.map((x) => x.textContent))); throw e; });
    await page.screenshot({ path: path.join(output, 'narration-max.png') });
    await page.click('#narr-max');
    assert.equal(await page.$eval('#modal-host .modal', (m) => m.classList.contains('modal--max')), false);
    await page.keyboard.press('Escape');
    // The saved narration text in the inspector opens the panel too.
    await page.click('#inspector .inspector__narration-text');
    await page.waitForSelector('#narr-prev');
    assert((await page.textContent('#narr-prev')).trim().length > 0);
    await page.keyboard.press('Escape');
    console.log('PASS narration: background job, reopen from status/text, enlarge');
    assert.deepEqual(errors, []);
    console.log('OK');
  } finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => {});
    if (docId) await page.evaluate((id) => window.RuntimeAPI.del(`/api/documents/${id}`).catch(() => {}), docId).catch(() => {});
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
