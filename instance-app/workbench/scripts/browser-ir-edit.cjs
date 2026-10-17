/* In-place (IR) editing of the md reader against a real instance app (no interception).
   APP_URL (default http://127.0.0.1:8094), APP_INVITE (default TLM-DEV-LOCAL),
   PLAYWRIGHT_MODULE (optional). Creates and deletes one test document. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8094';
const invite = process.env.APP_INVITE || 'TLM-DEV-LOCAL';
const output = path.resolve(__dirname, '../.work/ir-edit');

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

    // A document with one node anchored (by text offsets) to "gamma".
    const text = '# Title\n\nAlpha beta gamma delta.\n\nSecond paragraph with $x^2$.\n';
    const ids = await page.evaluate(async (text) => {
      const api = window.RuntimeAPI, S = window.TLM_WS.state;
      const d = (await api.post(`/api/projects/${S.project.id}/documents`, { title: 'ir-' + Date.now(), text })).document;
      const start = text.indexOf('gamma'), end = start + 5;
      const n = await api.post(`/api/projects/${S.project.id}/nodes`, { document_id: d.id, title: 'gamma', kind: 'concept', content: 'gamma',
        metadata: { source: { document_id: d.id, revision_id: d.current_revision_id, selector: { type: 'text', start, end, unit: 'utf16' },
          quote: 'gamma', prefix: text.slice(start - 32 < 0 ? 0 : start - 32, start), suffix: text.slice(end, end + 32) } } });
      return { doc: d.id, rev: d.current_revision_id, node: n.id };
    }, text);
    docId = ids.doc;
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.TLM_WS?.state.connected);
    await page.click(`[data-doc="${ids.doc}"]`);
    await page.waitForFunction(() => document.querySelector('#sheet mark.anchor'));

    // 1. Click a paragraph → its Markdown source is edited in place; typing before the anchor is allowed.
    const para = page.locator('#sheet p', { hasText: 'Alpha beta' });
    const box = await para.boundingBox();
    await page.mouse.click(box.x + 3, box.y + box.height / 2);
    await page.waitForSelector('#sheet textarea.ir-input');
    assert.equal(await page.inputValue('#sheet textarea.ir-input'), 'Alpha beta gamma delta.');
    await page.keyboard.press('Home');
    await page.keyboard.type('New ');
    // 2. Deleting inside the anchored word is refused with the bottom notice.
    await page.evaluate(() => { const ta = document.querySelector('#sheet textarea.ir-input'); const at = ta.value.indexOf('gamma') + 2; ta.setSelectionRange(at, at); });
    await page.keyboard.press('Backspace');
    assert.equal(await page.inputValue('#sheet textarea.ir-input'), 'New Alpha beta gamma delta.');
    await page.waitForFunction(() => [...document.querySelectorAll('.toast')].some((t) => t.textContent.includes('请先去除解释节点再进行修改')));
    // Editing after the anchor is fine.
    await page.evaluate(() => { const ta = document.querySelector('#sheet textarea.ir-input'); ta.setSelectionRange(ta.value.length, ta.value.length); });
    await page.keyboard.type(' More **bold**.');
    await page.screenshot({ path: path.join(output, 'editing.png') });
    // 3. Leaving the block re-renders it; the anchor still marks "gamma".
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('#sheet textarea.ir-input'));
    assert.match(await para.innerHTML(), /New Alpha beta <mark class="anchor[^"]*"[^>]*>gamma<\/mark> delta\. More <strong>bold<\/strong>\./);
    // 4. Saved as a new revision; the node now points at it with shifted offsets.
    await page.waitForFunction((rev) => window.TLM_WS.state.doc.current_revision_id !== rev
      && window.TLM_WS.state.nodes.some((n) => n.metadata?.source?.revision_id === window.TLM_WS.state.doc.current_revision_id), ids.rev, { timeout: 15000 });
    const after = await page.evaluate(async (ids) => {
      const S = window.TLM_WS.state, api = window.RuntimeAPI;
      const content = (await api.get(`/api/documents/${ids.doc}/content`)).text;
      const n = S.nodes.find((x) => x.id === ids.node);
      const resolved = (await api.post('/api/sources/resolve', { refs: [n.metadata.source] })).items[0];
      return { content, src: n.metadata.source, resolved };
    }, ids);
    assert.equal(after.content, '# Title\n\nNew Alpha beta gamma delta. More **bold**.\n\nSecond paragraph with $x^2$.');   // created text is stored without its trailing newline
    assert.equal(after.content.slice(after.src.selector.start, after.src.selector.end), 'gamma');
    assert.equal(after.resolved.resolved, true, 'the moved anchor resolves on the server');
    console.log('PASS edit around anchor, refusal inside it, re-render, save as revision, anchor moved');

    // 5. Headings and math blocks are edited the same way; one click moves between blocks.
    await page.click('#sheet .doc h1');
    await page.waitForSelector('#sheet .doc h1 textarea.ir-input');
    assert.equal(await page.inputValue('#sheet textarea.ir-input'), '# Title');
    await page.keyboard.press('End'); await page.keyboard.type(' Two');
    const second = await page.locator('#sheet p', { hasText: 'Second paragraph' }).boundingBox();
    await page.mouse.click(second.x + 3, second.y + second.height / 2);
    await page.waitForSelector('#sheet p textarea.ir-input');
    assert.equal(await page.textContent('#sheet .doc h1'), 'Title Two');
    assert.equal(await page.inputValue('#sheet textarea.ir-input'), 'Second paragraph with $x^2$.');
    await page.keyboard.press('Escape');
    assert(await page.locator('#sheet p .katex').count() >= 1, 'math re-rendered');
    console.log('PASS heading edit, block-to-block click, math re-render');

    // 6. Reload: edits persisted, anchor still on "gamma".
    await page.waitForFunction(() => document.querySelector('#st-state')?.textContent !== '未保存', null, { timeout: 15000 });
    await page.waitForTimeout(1500);
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.TLM_WS?.state.connected);
    await page.click(`[data-doc="${ids.doc}"]`);
    await page.waitForFunction(() => document.querySelector('#sheet mark.anchor')?.textContent === 'gamma');
    assert.equal(await page.textContent('#sheet .doc h1'), 'Title Two');
    console.log('PASS reload keeps edits and anchor');
    assert.deepEqual(errors, []);
    console.log('OK');
  } finally {
    if (docId) await page.evaluate(async (id) => window.RuntimeAPI.del(`/api/documents/${id}`), docId).catch(() => {});
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
