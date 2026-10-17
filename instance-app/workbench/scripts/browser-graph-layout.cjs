/* Use graph_layout_test_app.py, a disposable standalone fake backend only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8094';
const output = path.resolve(__dirname, '../.work/graph-layout');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  const errors = [];
  let docId;
  page.on('pageerror', error => errors.push(error.message));
  async function waitLayout() {
    await page.waitForFunction(() => !document.querySelector('[data-act="auto-layout"]').disabled
      && !document.querySelector('#world').classList.contains('animate') && document.querySelector('#st-state').textContent === '已同步');
  }
  function boxes() {
    return page.locator('.gnode').evaluateAll(nodes => nodes.map(el => ({ id: el.dataset.id,
      x: el.offsetLeft, y: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight })));
  }
  async function assertGeometry() {
    const values = await boxes(), byId = Object.fromEntries(values.map(box => [box.id, box]));
    for (const a of values) for (const b of values) {
      if (a.id >= b.id) continue;
      assert(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y,
        `Cards overlap: ${a.id}, ${b.id}`);
    }
    const nodes = await page.evaluate(() => window.TLM_WS.state.nodes);
    for (const n of nodes) if (n.parent_id && byId[n.parent_id]) {
      const parent = byId[n.parent_id];
      assert(parent.x + parent.width < byId[n.id].x, 'Parent must be left of child');
    }
    return values;
  }
  async function scramble() {
    await page.evaluate(async () => {
      for (const n of window.TLM_WS.state.nodes) {
        await RuntimeAPI.patch('/api/nodes/' + n.id, { metadata: { ...n.metadata, position: { x: 40, y: 40 } } });
      }
    });
    await page.reload(); await page.waitForSelector('.gnode'); await page.waitForLoadState('networkidle');
  }
  try {
    await page.goto(origin + '/index.html');
    assert.equal(await page.evaluate(() => RuntimeAPI.detect()), false);
    assert.equal((await page.evaluate(() => RuntimeAPI.get('/api/runtime', { auth: false }))).provider.name, 'fake');
    await page.waitForFunction(() => !document.querySelector('#invite-field').hidden);
    await page.fill('#invite', process.env.APP_INVITE || 'TLM-DEV-LOCAL');
    await page.click('#start'); await page.waitForURL('**/workspace.html');
    await page.waitForFunction(() => window.TLM_WS?.state.project && window.TLM_WS.state.connected);
    docId = await page.evaluate(async () => {
      const pid = window.TLM_WS.state.project.id;
      const { document } = await RuntimeAPI.post('/api/projects/' + pid + '/documents', { title: 'Relations and layout', text: '# Evidence\n\nRoot, two branches and their dependent concepts.' });
      const ids = [];
      for (const [i, title] of ['Root premise', 'First branch', 'Second branch', 'First leaf', 'Second leaf', 'Independent evidence'].entries()) {
        const n = await RuntimeAPI.post('/api/projects/' + pid + '/nodes', { document_id: document.id,
          title, kind: 'concept', content: (title + ' has deliberately varied card content. ').repeat(i + 1),
          parent_id: i === 1 || i === 2 ? ids[0] : i === 3 ? ids[1] : i === 4 ? ids[2] : null,
          metadata: { position: { x: 40, y: 40 } } });
        ids.push(n.id);
      }
      await RuntimeAPI.post('/api/projects/' + pid + '/graph/edges', {
        source_node_id: ids[5], target_node_id: ids[2], relation: 'related_to' });
      return document.id;
    });
    await page.reload(); await page.waitForSelector('.gnode'); await page.waitForLoadState('networkidle');
    const original = await boxes();
    await page.click('[data-act="ai-relations"]');
    await page.waitForSelector('#apply-relations');
    assert.deepEqual(await boxes(), original, 'Preview must not rearrange cards');
    await page.getByRole('button', { name: '保留原连线', exact: true }).click();
    assert.deepEqual(await boxes(), original, 'Rejecting suggestions must preserve positions');
    await page.evaluate(() => {
      window.layoutFrames = [];
      function sample() {
        const el = document.querySelector('.gnode');
        window.layoutFrames.push({ x: el.offsetLeft, y: el.offsetTop, active: document.querySelector('#world').classList.contains('animate') });
        if (window.layoutFrames.length < 150) requestAnimationFrame(sample);
      }
      requestAnimationFrame(sample);
    });
    await page.click('[data-act="ai-relations"]'); await page.waitForSelector('#apply-relations');
    await page.click('#apply-relations');
    await page.waitForFunction(() => document.querySelector('[data-act="auto-layout"]').disabled);
    await waitLayout();
    const settled = await assertGeometry();
    const frames = await page.evaluate(() => window.layoutFrames.filter(frame => frame.active));
    assert(frames.length >= 3, 'Visible transition must have multiple frames');
    assert(new Set(frames.map(frame => `${frame.x},${frame.y}`)).size > 2, 'Card must move through intermediate positions');
    const saved = await page.evaluate(async () => (await RuntimeAPI.get('/api/projects/' + window.TLM_WS.state.project.id + '/graph')).nodes);
    for (const box of settled) assert.deepEqual(saved.find(n => n.id === box.id).metadata.position, { x: box.x, y: box.y });
    assert(await page.evaluate(() => window.TLM_WS.state.edges.some(edge => edge.relation === 'supports')));
    await page.screenshot({ path: path.join(output, 'desktop.png') });
    await page.reload(); await page.waitForSelector('.gnode'); await page.waitForLoadState('networkidle');
    assert.deepEqual(await boxes(), settled, 'Positions must survive reload');
    await scramble();
    await page.click('[data-act="ai-relations"]');
    await page.getByRole('button', { name: '完成', exact: true }).waitFor();
    await waitLayout(); await assertGeometry();
    await page.getByRole('button', { name: '完成', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.click('[data-act="auto-layout"]'); await waitLayout(); await assertGeometry();
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.click('[data-act="auto-layout"]'); await waitLayout(); await assertGeometry();
    assert.deepEqual(errors, []);
    console.log('PASS: preview/cancel preserve positions; accept and no-change results arrange cards; animation, persistence, no overlaps, left-to-right root-to-leaf order, mobile and reduced motion');
  } finally {
    if (docId) await page.evaluate(id => RuntimeAPI.del('/api/documents/' + id), docId);
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
