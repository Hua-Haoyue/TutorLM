/* Auto-layout order, card header / action rows, IR Enter + arrow keys and Chat web sources
   against a real fake-provider instance app. APP_URL (default http://127.0.0.1:8182),
   APP_INVITE (default TLM-DEV-LOCAL), PLAYWRIGHT_MODULE (optional). Creates and deletes
   its own documents; Chat sources are injected into one thread response (the fake
   provider has no web search). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8182';
const invite = process.env.APP_INVITE || 'TLM-DEV-LOCAL';
const output = path.resolve(__dirname, '../.work/layout-ir-sources');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const docs = [];
  try {
    await page.goto(origin + '/index.html');
    await page.waitForFunction(() => document.querySelector('#cap-free')?.textContent !== '–');
    if (await page.isVisible('#invite-field')) await page.fill('#invite', invite);
    await page.click('#start'); await page.waitForURL('**/workspace.html');
    await page.waitForFunction(() => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
    await page.waitForLoadState('networkidle');
    const text = 'Alpha concept here.\n\nBeta concept here.\n\nGamma concept here.\n\nDelta concept here.';
    const ids = await page.evaluate(async (text) => {
      const api = window.RuntimeAPI, S = window.TLM_WS.state, stamp = Date.now();
      const doc = (await api.post(`/api/projects/${S.project.id}/documents`, { title: 'layout-' + stamp, text })).document;
      const mk = async (word) => {
        const start = text.indexOf(word), end = start + word.length;
        return (await api.post(`/api/projects/${S.project.id}/nodes`, { document_id: doc.id, title: word, kind: 'concept', content: word,
          metadata: { source: { document_id: doc.id, revision_id: doc.current_revision_id, quote: word,
            selector: { type: 'text', start, end, unit: 'utf16' } }, position: { x: 900 - start * 5, y: 900 - start * 9 } } })).id;
      };
      // Created out of document order on purpose.
      const delta = await mk('Delta'), beta = await mk('Beta'), gamma = await mk('Gamma'), alpha = await mk('Alpha');
      // Gamma is a prerequisite of Beta: it moves right of Beta.
      await api.post(`/api/projects/${S.project.id}/graph/edges`, { source_node_id: gamma, target_node_id: beta, relation: 'prerequisite_of' });
      const md = (await api.post(`/api/projects/${S.project.id}/documents`, { title: 'ir-' + stamp,
        text: '# Title\n\nPara one.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |' })).document;
      return { doc: doc.id, md: md.id, alpha, beta, gamma, delta };
    }, text);
    docs.push(ids.doc, ids.md);
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.TLM_WS?.state.connected);

    // 1. Auto-layout: circled concepts left in document order; a prerequisite to the right.
    await page.click(`[data-doc="${ids.doc}"]`);
    await page.waitForSelector(`.gnode[data-id="${ids.alpha}"]`);
    await page.click('[data-act="auto-layout"]');
    await page.waitForFunction(() => !document.querySelector('[data-act="auto-layout"]').disabled
      && document.querySelector('#st-state').textContent === '已同步');
    const pos = await page.evaluate((ids) => Object.fromEntries(Object.entries(ids).filter(([k]) => !['doc', 'md'].includes(k))
      .map(([k, id]) => [k, window.TLM_WS.state.nodes.find((n) => n.id === id).metadata.position])), ids);
    assert(pos.alpha.x === pos.beta.x && pos.beta.x === pos.delta.x, 'circled concepts share the left column: ' + JSON.stringify(pos));
    assert(pos.gamma.x > pos.beta.x, 'prerequisite right of the concept that needs it');
    assert(pos.alpha.y < pos.beta.y && pos.beta.y < pos.delta.y, 'left column in document order: ' + JSON.stringify(pos));
    console.log('PASS auto-layout: document order, prerequisite to the right');

    // 2. Card: pager on the title line, actions on one row.
    await page.click(`.gnode[data-id="${ids.alpha}"] h4`);
    for (const label of ['生成解释', '重新生成']) {
      await page.locator(`.gnode.sel[data-id="${ids.alpha}"] [data-act="node-explain"]`, { hasText: label }).click();
      await page.waitForFunction((id) => !document.querySelector(`.gnode.sel[data-id="${id}"] [data-act="node-explain"]`)?.disabled
        && document.querySelector(`.gnode.sel[data-id="${id}"] .answer`), ids.alpha, { timeout: 30000 });
    }
    await page.waitForSelector(`.gnode.sel[data-id="${ids.alpha}"] .gnode__titlerow .pager`);
    const rows = await page.evaluate((id) => {
      const card = document.querySelector(`.gnode.sel[data-id="${id}"]`), r = (s) => card.querySelector(s).getBoundingClientRect();
      return { title: r('.gnode__titlerow h4'), pager: r('.gnode__titlerow .pager'), card: card.getBoundingClientRect(),
        actions: [...card.querySelectorAll('.node-learning-actions > button')].map((b) => Math.round(b.getBoundingClientRect().top)) };
    }, ids.alpha);
    assert(Math.abs(rows.pager.top - rows.title.top) < 8 && rows.pager.left > rows.title.left, 'pager after the title');
    assert(rows.card.right - rows.pager.right < 24, 'pager right-aligned');
    assert(rows.actions.length >= 2 && new Set(rows.actions).size === 1, 'actions on one row: ' + rows.actions);
    assert.equal(await page.locator('[data-act="retelling-toggle"]').count(), 0, 'no 原解释/新解释 switch beside the pager');
    await page.screenshot({ path: path.join(output, 'card.png') });
    console.log('PASS card: pager on the title line, actions in one row');

    // 3. IR: Enter makes a new paragraph; ↓ moves to the next block; Enter in the last
    //    table row adds a paragraph after the table.
    await page.click(`[data-doc="${ids.md}"]`);
    await page.waitForSelector('#sheet .doc p');
    const para = await page.locator('#sheet .doc p', { hasText: 'Para one.' }).boundingBox();
    await page.mouse.click(para.x + para.width - 2, para.y + para.height / 2);
    await page.waitForSelector('#sheet textarea.ir-input');
    await page.keyboard.press('End'); await page.keyboard.press('Enter'); await page.keyboard.type('Second para.');
    for (let i = 0; i < 3; i++) await page.keyboard.press('ArrowUp');   // two lines up, then the heading
    await page.waitForFunction(() => document.querySelector('#sheet textarea.ir-input')?.value === '# Title');
    await page.keyboard.press('ArrowDown');
    await page.waitForFunction(() => document.querySelector('#sheet textarea.ir-input')?.value.startsWith('Para one.'));
    assert(await page.evaluate(() => window.TLM_WS.state.text.includes('Para one.\n\nSecond para.\n\n| a | b |')), 'Enter made a new paragraph');
    await page.keyboard.press('Escape');
    const cell = await page.locator('#sheet td', { hasText: '3' }).boundingBox();
    await page.mouse.click(cell.x + cell.width / 2, cell.y + cell.height / 2);
    await page.waitForSelector('#sheet td textarea.ir-input');
    await page.keyboard.press('Enter');
    await page.waitForSelector('#sheet .ir-insert textarea');
    await page.keyboard.type('After table.');
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => window.TLM_WS.state.text.endsWith('| 3 | 4 |\n\nAfter table.'));
    await page.waitForSelector('#sheet .doc p:has-text("After table.")');
    // ↓ at the end of the document opens a new paragraph there; leaving it empty changes nothing.
    const last = await page.locator('#sheet .doc p', { hasText: 'After table.' }).boundingBox();
    await page.mouse.click(last.x + last.width - 2, last.y + last.height / 2);
    await page.keyboard.press('ArrowDown');
    await page.waitForSelector('#sheet .ir-insert textarea');
    await page.keyboard.press('Escape');
    assert(await page.evaluate(() => window.TLM_WS.state.text.endsWith('After table.')));
    await page.waitForFunction(() => document.querySelector('#st-state')?.textContent === '已同步', null, { timeout: 30000 });
    console.log('PASS IR: Enter paragraph, arrow navigation, paragraph after a table');

    // 4. Chat web sources: numbered links, [n] citations link to them, unsafe URLs dropped.
    await page.route('**/api/chat/threads/*', async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      const res = await route.fetch(), body = await res.json();
      body.messages = [{ id: 1, role: 'user', body: { text: 'Q' }, created: 1 },
        { id: 2, role: 'assistant', created: 2, body: { text: 'Answer citing [1] and [2], not [3].', tools: [{ tool: 'web_search', status: 'ok' }],
          sources: [{ uri: 'https://example.org/a', title: 'Page A' }, { uri: 'https://www.example.net/b', title: 'Page B' }, { uri: 'javascript:alert(1)', title: 'x' }] } }];
      await route.fulfill({ response: res, json: body });
    });
    await page.click('#workspace-chat-toggle');
    await page.waitForSelector('#workspace-chat .workspace-chat-sources a');
    const links = await page.$$eval('#workspace-chat .workspace-chat-sources a', (as) => as.map((a) => [a.href, a.rel, a.target]));
    assert.deepEqual(links.map((l) => l[0]), ['https://example.org/a', 'https://www.example.net/b']);
    assert(links.every((l) => l[1].includes('noopener') && l[2] === '_blank'));
    const cites = await page.$$eval('#workspace-chat .workspace-chat-cite', (as) => as.map((a) => [a.textContent, a.href]));
    assert.deepEqual(cites, [['1', 'https://example.org/a'], ['2', 'https://www.example.net/b']]);
    // The fake provider declares web search: the globe is Chat's own switch and leaves
    // the 偏好设置 web search (used by explanations) untouched.
    const web = page.locator('#workspace-chat [data-chat-web]');
    const was = await web.getAttribute('aria-pressed'), prefWeb = await page.evaluate(() => window.TLM_WS.modelOptions().web_search);
    await web.click();
    assert.notEqual(await web.getAttribute('aria-pressed'), was);
    assert.equal(await page.evaluate(() => localStorage.getItem('tlm.chat.web')), was === 'true' ? 'off' : 'on');
    assert.equal(await page.evaluate(() => window.TLM_WS.modelOptions().web_search), prefWeb);
    await web.click();
    await page.screenshot({ path: path.join(output, 'chat-sources.png') });
    console.log('PASS chat: web sources and citations');
    assert.deepEqual(errors, []);
    console.log('OK');
  } finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => {});
    await page.evaluate(async (ids) => { for (const id of ids) await window.RuntimeAPI.del(`/api/documents/${id}`).catch(() => {}); }, docs).catch(() => {});
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
