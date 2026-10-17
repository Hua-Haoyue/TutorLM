/* Tabs, code view, card animation and Chat against a real fake-provider instance app (no interception).
   APP_URL (default http://127.0.0.1:8182), APP_INVITE (default TLM-DEV-LOCAL), PLAYWRIGHT_MODULE (optional).
   Creates and deletes its own documents. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8182';
const invite = process.env.APP_INVITE || 'TLM-DEV-LOCAL';
const output = path.resolve(__dirname, '../.work/tabs-code-chat');

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
    const code = Array.from({ length: 40 }, (_, i) => i === 3 ? 'def allocate(self, *, label: str = "") -> int:' : `    value_${i} = ${i}  # line ${i + 1}`).join('\n');
    Object.assign(docs, await page.evaluate(async (code) => {
      const api = window.RuntimeAPI, S = window.TLM_WS.state, stamp = Date.now();
      const mk = async (title, text) => (await api.post(`/api/projects/${S.project.id}/documents`, { title, text })).document.id;
      const a = await mk('tabs-A-' + stamp, '# A\n\nFirst document.'), b = await mk('tabs-B-' + stamp, '# B\n\nSecond document.');
      const c = (await api.request('/api/code/file', { method: 'PUT', body: { path: `pkg/control_plane/allocator_${stamp}.py`, text: code } })).document.id;
      const n = await api.post(`/api/projects/${S.project.id}/nodes`, { document_id: a, title: 'Card', kind: 'concept', content: 'Card', metadata: { source: { quote: 'First' } } });
      await api.run('explanation', { node_id: n.id });
      return [a, b, c, n.id];
    }, code));
    const [A, B, C, N] = docs;
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.TLM_WS?.state.connected);

    // 1. Tabs: opening documents adds tabs; click switches; drag reorders; × closes; + opens a picker.
    for (const id of [A, B]) { await page.click(`[data-doc="${id}"]`); await page.waitForFunction((id) => window.TLM_WS.state.doc?.id === id, id); }
    const tabs = () => page.$$eval('#doc-tabs [data-tab]', (els) => els.map((e) => e.dataset.tab));
    let order = await tabs();
    assert(order.includes(A) && order.includes(B), 'tabs for opened documents');
    assert.equal(await page.getAttribute(`#doc-tabs [data-tab="${B}"]`, 'aria-selected'), 'true');
    await page.click(`#doc-tabs [data-tab="${A}"]`);
    await page.waitForFunction((id) => window.TLM_WS.state.doc?.id === id, A);
    await page.dragAndDrop(`#doc-tabs [data-tab="${B}"]`, `#doc-tabs [data-tab="${A}"]`, { targetPosition: { x: 4, y: 10 } });
    order = await tabs();
    assert(order.indexOf(B) < order.indexOf(A), 'drag reorders: ' + JSON.stringify(order));
    await page.click('#doc-tabs [data-tab-add]');
    await page.waitForSelector('.tab-picker [data-pick]');
    await page.click(`.tab-picker [data-pick="${C}"]`);
    await page.waitForFunction((id) => window.TLM_WS.state.doc?.id === id, C);
    assert((await tabs()).includes(C), 'picker opens a new tab');
    console.log('PASS tabs: open, switch, drag reorder, + picker');

    // 2 + 3. Code file: Code view only, other views greyed out, editor-style page with line numbers.
    assert.equal(await page.evaluate(() => window.TLM_WS.state.mode), 'code');
    const seg = await page.$$eval('.seg [data-view]', (els) => els.map((e) => [e.dataset.view, e.disabled, e.getAttribute('aria-checked')]));
    assert.deepEqual(seg, [['md', true, 'false'], ['raw', true, 'false'], ['pdf', true, 'false'], ['code', false, 'true']]);
    assert.equal(await page.locator('#sheet .doc-head').count(), 0, 'no document header in code view');
    assert.equal(await page.locator('#sheet .code-reading:not(.code-reading--edit) .code-gutter span').count(), 40, 'one line number per line');
    assert.match(await page.textContent('#sheet .code-crumbs'), /pkg.*control_plane.*allocator_/);
    const align = await page.evaluate(() => {
      const g = document.querySelector('#sheet .code-reading:not(.code-reading--edit) .code-gutter span:nth-child(4)').getBoundingClientRect();
      const r = document.createRange(), pre = document.querySelector('#sheet pre.code-source');
      const t = [...pre.querySelectorAll('*')].find((x) => x.textContent === 'allocate') || pre;
      r.selectNodeContents(t); return [Math.round(g.top), Math.round(r.getBoundingClientRect().top)];
    });
    assert(Math.abs(align[0] - align[1]) <= 2, 'line 4 number aligned with its code: ' + align);
    await page.locator('#sheet pre.code-source').click({ position: { x: 60, y: 30 } });
    assert.equal(await page.locator('#sheet .code-gutter span.on').count(), 1, 'current line highlighted');
    await page.screenshot({ path: path.join(output, 'code.png') });
    console.log('PASS code view: locked to Code, editor layout, line numbers, current line');

    // × closes the active tab and shows a neighbour.
    await page.click(`#doc-tabs [data-tab-close="${C}"]`);
    await page.waitForFunction((id) => window.TLM_WS.state.doc && window.TLM_WS.state.doc.id !== id, C);
    assert(!(await tabs()).includes(C));
    console.log('PASS close tab');

    // 4. Card open / close / enlarge animate.
    await page.click(`#doc-tabs [data-tab="${A}"]`);
    await page.waitForSelector(`.gnode[data-id="${N}"]`);
    await page.click(`.gnode[data-id="${N}"] h4`);
    await page.waitForSelector(`.gnode.sel[data-id="${N}"]`);
    assert(await page.evaluate((id) => document.querySelector(`.gnode[data-id="${id}"]`).getAnimations().length > 0, N), 'open animates');
    await page.waitForTimeout(400);
    await page.click(`.gnode.sel[data-id="${N}"] [data-card-layout="zoom"]`);
    assert(await page.evaluate((id) => document.querySelector(`.gnode[data-id="${id}"]`).getAnimations().length > 0, N), 'enlarge animates');
    await page.waitForFunction((id) => document.querySelector(`.gnode[data-id="${id}"]`).dataset.cardScale === 'large', N);
    await page.waitForTimeout(400);
    await page.click(`.gnode.sel[data-id="${N}"] [data-card-layout="zoom"]`);
    await page.waitForFunction((id) => document.querySelector(`.gnode[data-id="${id}"]`).dataset.cardScale === 'normal', N);
    console.log('PASS card open / enlarge / shrink animate');

    // 5. Chat: icon toggle, rounded card, new conversation, conversation list.
    assert.equal((await page.textContent('#workspace-chat-toggle')).trim(), '', 'icon button, no text');
    await page.click('#workspace-chat-toggle');
    await page.waitForSelector('#workspace-chat:not([hidden])');
    assert(parseFloat(await page.$eval('#workspace-chat', (e) => getComputedStyle(e).borderTopLeftRadius)) >= 16, 'large radius');
    await page.fill('#workspace-chat textarea', '总结一下这个项目');
    await page.keyboard.press('Enter');
    await page.waitForSelector('#workspace-chat .workspace-chat-message.assistant:not(.waiting)', { timeout: 30000 });
    assert.equal(await page.locator('#workspace-chat .workspace-chat-message.user').count(), 1);
    await page.click('#workspace-chat .workspace-chat-head [data-chat-new]');
    await page.waitForSelector('#workspace-chat .workspace-chat-empty');
    await page.click('#workspace-chat [data-chat-list]');
    await page.waitForSelector('#workspace-chat [data-thread]');
    const titles = await page.$$eval('#workspace-chat .workspace-chat-thread-title', (els) => els.map((e) => e.textContent));
    assert(titles.includes('总结一下这个项目'), JSON.stringify(titles));
    await page.locator('#workspace-chat .workspace-chat-thread', { hasText: '总结一下这个项目' }).click();
    await page.waitForSelector('#workspace-chat .workspace-chat-message.user');
    await page.screenshot({ path: path.join(output, 'chat.png') });
    console.log('PASS chat: icon toggle, rounded card, bubbles, new conversation, conversation list');
    assert.deepEqual(errors, []);
    console.log('OK');
  } finally {
    await page.evaluate(async (ids) => { for (const id of ids.slice(0, 3)) await window.RuntimeAPI.del(`/api/documents/${id}`).catch(() => {}); }, docs).catch(() => {});
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
