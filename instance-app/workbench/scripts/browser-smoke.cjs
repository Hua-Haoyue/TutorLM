/* End-to-end smoke against the real instance-app through scripts/dev-server.py.
   No route interception. APP_URL (default http://127.0.0.1:8092), APP_INVITE
   (default TLM-DEV-LOCAL), PLAYWRIGHT_MODULE (optional), BROWSER_CHANNEL (default msedge).
   Creates documents/nodes in the target workspace; use a disposable dev backend. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8092';
const invite = process.env.APP_INVITE || 'TLM-DEV-LOCAL';
const output = path.resolve(__dirname, '../.work/browser-smoke');
const wait = (page, fn, arg) => page.waitForFunction(fn, arg, { timeout: 25000 });

async function selectInReader(page, needle) {
  await page.evaluate((needle) => {
    const block = [...document.querySelectorAll('#sheet [data-b]')].find((x) => x.textContent.includes(needle));
    block.scrollIntoView({ block: 'center' });
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    let t; while ((t = walker.nextNode()) && !t.nodeValue.includes(needle));
    const i = t.nodeValue.indexOf(needle);
    const range = document.createRange(); range.setStart(t, i); range.setEnd(t, i + needle.length);
    getSelection().removeAllRanges(); getSelection().addRange(range);
    document.querySelector('#reader-scroll').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  }, needle);
}

/* Minimal text PDF (Helvetica, one text line per row) so the test needs no fixture file. */
function makePdf(pageCount) {
  const streams = Array.from({ length: pageCount }, (_, i) => {
    const n = i + 1, rows = [`Chapter ${n}: topic${n}`, ...Array.from({ length: 20 }, (_, k) => `Line ${k + 1} of page ${n} discusses gradient${n} in detail.`)];
    return ['BT', '/F1 11 Tf', '14 TL', '56 780 Td', ...rows.map((r) => `(${r}) Tj T*`), 'ET'].join('\n');
  });
  const font = 3 + 2 * pageCount;
  const objs = ['<< /Type /Catalog /Pages 2 0 R >>',
    `<< /Type /Pages /Kids [${streams.map((_, i) => `${3 + 2 * i} 0 R`).join(' ')}] /Count ${pageCount} >>`];
  streams.forEach((st, i) => {
    objs.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents ${4 + 2 * i} 0 R /Resources << /Font << /F1 ${font} 0 R >> >> >>`);
    objs.push(`<< /Length ${Buffer.byteLength(st)} >>\nstream\n${st}\nendstream`);
  });
  objs.push('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  let out = '%PDF-1.4\n';
  const offsets = objs.map((body, i) => { const at = Buffer.byteLength(out); out += `${i + 1} 0 obj\n${body}\nendobj\n`; return at; });
  const xref = Buffer.byteLength(out);
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n${offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n \n`).join('')}`;
  out += `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(25000);
  const errors = [], bad = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('response', (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith('/api/') && r.status() >= 400) bad.push(`${r.request().method()} ${u.pathname} ${r.status()}`);
  });
  try {
    // Entry: invite → app Bearer → workspace.
    await page.goto(`${origin}/index.html`);
    await page.waitForFunction(() => document.querySelector('#cap-free').textContent !== '–');
    if (await page.isVisible('#invite-field')) await page.fill('#invite', invite);
    await page.click('#start');
    await page.waitForURL('**/workspace.html');
    await wait(page, () => window.TLM_WS?.state.connected && window.TLM_WS.state.project);
    await page.waitForLoadState('networkidle');   // let the initial document open settle first
    console.log('connected, project', await page.evaluate(() => window.TLM_WS.state.project.id));

    // Upload: the document is named after the file stem; no nodes are derived.
    const stem = `smoke-${Date.now()}`;
    await page.locator('#file-input').setInputFiles({ name: `${stem}.md`, mimeType: 'text/markdown',
      buffer: Buffer.from('# Attention\n\nAttention assigns weights to inputs.\n\n## Context\n\nContext changes interpretation of every token.\n') });
    await wait(page, (n) => window.TLM_WS.state.doc?.title === n && document.querySelector('#sheet').textContent.includes('assigns weights'), stem);
    console.log('uploaded', stem);

    // Selection → ask → node + explanation run.
    await selectInReader(page, 'assigns weights to inputs');
    await page.waitForSelector('#ask:not([hidden])');
    await page.fill('#ask-input', 'What does it weigh?');
    await page.locator('#ask-form button[type=submit]').click();
    await wait(page, () => { const S = window.TLM_WS.state; return S.selected && (S.history[S.selected] || []).length > 0; });
    const parentId = await page.evaluate(() => window.TLM_WS.state.selected);
    console.log('explained node', parentId);

    // A child with its own explanation, then 回讲 from the parent card.
    await page.evaluate(async (parentId) => {
      const api = window.RuntimeAPI, S = window.TLM_WS.state;
      const child = await api.post(`/api/projects/${S.project.id}/nodes`, { document_id: S.doc.id, title: 'Weights', kind: 'concept', content: 'weights', parent_id: parentId, metadata: { source: { quote: 'weights', fromNode: parentId } } });
      await api.run('explanation', { node_id: child.id });
    }, parentId);
    await page.reload();
    await wait(page, () => window.TLM_WS?.state.connected && window.TLM_WS.state.nodes.length);
    await page.waitForLoadState('networkidle');
    await page.evaluate((id) => window.TLM_WS.select(id), parentId);
    await page.click(`.gnode[data-id="${parentId}"] [data-act="node-optimize"]`);
    // First retelling asks for the learner profile (PUT /api/learning/profile).
    if (await page.waitForSelector('#lp-stage', { timeout: 5000 }).then(() => true, () => false)) await page.click('.modal__foot .btn--primary');
    await wait(page, (id) => (window.TLM_WS.state.history[id] || []).length >= 2, parentId);
    console.log('retelling stored as a new explanation version');

    // Narration over the document's nodes.
    const narration = await page.evaluate(async () => {
      const S = window.TLM_WS.state;
      return (await window.RuntimeAPI.run('narration', { project_id: S.project.id, node_ids: S.nodes.filter((n) => n.document_id === S.doc.id).map((n) => n.id), language: 'zh', tone: 'teacher' })).narration;
    });
    assert(narration.segments.length >= 1);
    console.log('narration segments', narration.segments.length);

    await page.screenshot({ path: path.join(output, 'workspace.png') });

    // Free drawing on the rendered markdown: one stroke, persisted locally.
    await page.click('[data-tool="draw"]');
    assert(await page.isVisible('#drawbar'), 'draw bar in the reader top bar');
    const md = await page.locator('#sheet .doc').first().boundingBox();
    await page.mouse.move(md.x + 40, md.y + 120); await page.mouse.down();
    await page.mouse.move(md.x + 260, md.y + 150, { steps: 8 }); await page.mouse.up();
    assert.equal(await page.locator('#sheet .doc svg.ink path').count(), 1, 'md ink stroke');
    await page.keyboard.press('Escape');

    // Clean up the test document (and its nodes).
    await page.evaluate(async () => { const S = window.TLM_WS.state; await window.RuntimeAPI.del(`/api/documents/${S.doc.id}`); });

    // PDF: upload → PDF.js view (instance-app reader) → selection → server-resolvable node → find → ink.
    const pdfStem = `smoke-pdf-${Date.now()}`;
    await page.locator('#file-input').setInputFiles({ name: `${pdfStem}.pdf`, mimeType: 'application/pdf', buffer: makePdf(3) });
    await wait(page, (n) => window.TLM_WS.state.doc?.title === n && window.TLM_WS.state.mode === 'pdf', pdfStem);
    await page.waitForSelector('.pdfv-page[data-page="1"] .textLayer span', { timeout: 30000 });
    assert.equal(await page.locator('.pdfv-page').count(), 3, 'all PDF pages mounted');
    await page.evaluate(() => {
      const span = [...document.querySelectorAll('.pdfv-page[data-page="1"] .textLayer span')].find((s) => s.textContent.includes('Line 2 of page 1'));
      const t = span.firstChild, r = document.createRange();
      r.setStart(t, 0); r.setEnd(t, 'Line 2 of page 1'.length);
      getSelection().removeAllRanges(); getSelection().addRange(r);
      document.querySelector('#reader-scroll').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    });
    await page.waitForSelector('#ask:not([hidden])');
    await page.fill('#ask-input', 'Explain this line');
    await page.locator('#ask-form button[type=submit]').click();
    await wait(page, () => { const S = window.TLM_WS.state; return S.selected && (S.history[S.selected] || []).length > 0; });
    const pdfNode = await page.evaluate(() => window.TLM_WS.state.nodes.find((n) => n.id === window.TLM_WS.state.selected));
    assert.equal(pdfNode.metadata.source.selector.type, 'pdf');
    assert(pdfNode.metadata.source.revision_id, 'server-resolvable pdf SourceRef');
    assert(await page.locator('.pdfv-marks mark.anchor').count() >= 1, 'anchor box on the page');
    console.log('pdf node explained from', JSON.stringify(pdfNode.metadata.source.selector.pages[0]));
    await page.keyboard.press('Control+f');
    await page.fill('#find-input', 'gradient2');
    await wait(page, () => /\/ [1-9]/.test(document.querySelector('#find-count').textContent) && document.querySelector('.pdfv-marks mark.find'));
    await page.keyboard.press('Escape');
    await page.click('[data-tool="draw"]');
    await page.evaluate(() => document.querySelector('.pdfv-page[data-page="1"]').scrollIntoView({ block: 'start' }));
    const pg = await page.locator('.pdfv-page[data-page="1"]').boundingBox();
    await page.mouse.move(pg.x + 80, pg.y + 160); await page.mouse.down();
    await page.mouse.move(pg.x + 300, pg.y + 200, { steps: 8 }); await page.mouse.up();
    assert.equal(await page.locator('.pdfv-page[data-page="1"] svg.ink path').count(), 1, 'pdf ink stroke');
    await page.screenshot({ path: path.join(output, 'pdf.png') });
    console.log('pdf view, find and ink ok');
    await page.evaluate(async () => { const S = window.TLM_WS.state; await window.RuntimeAPI.del(`/api/documents/${S.doc.id}`); });

    // /text answers 409 until the first extraction of a fresh PDF has run.
    const unexpected = bad.filter((x) => !/\/api\/sessions\/me 401/.test(x) && !/\/text 409$/.test(x));
    assert.deepEqual(errors, [], 'page errors');
    assert.deepEqual(unexpected, [], 'failed API calls');
    console.log('OK');
  } finally {
    await browser.close();
  }
}
main().catch((e) => { console.error(e); process.exit(1); });
