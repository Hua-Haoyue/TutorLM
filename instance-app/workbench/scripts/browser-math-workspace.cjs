// Every API request is intercepted; this test only loads application static files.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const output = path.resolve(__dirname, '../.work/math-workspace');
fs.mkdirSync(output, { recursive: true });
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const inputFormat of ['md', 'txt', 'tex', 'zip']) {
      const format = inputFormat === 'zip' ? 'tex' : inputFormat;
      const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
      const errors = [], posted = [], unexpected = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => { if (message.type() === 'error') console.error('Browser:', message.text()); });
      const screenshotFormula = 'E = \\frac{L + S + C + H}{V} \\times 100\\%';
      const text = (format === 'tex' ? '\\documentclass{article}\n\\begin{document}\nFormula $x_i^2+\\alpha=1$.\n\\end{document}' : '# Formula\n\nFirst $x_i^2+\\alpha=1$.\n\nSecond $\\frac{a+b}{c}=d$.') + '\n\n$$\n' + screenshotFormula + '\n$$\n\n$$\nx+\\alpha=1\n$$';
      const doc = { id: 'doc', title: 'Formula.' + inputFormat, source_filename: 'Formula.' + inputFormat, format, current_revision_id: 'rev' };
      let nodes = [];
      await page.route(/\/(api|control|health)\//, async route => {
        const request = route.request(), url = new URL(request.url()), method = request.method();
        let value = {};
        if (url.pathname === '/control/health') return route.fulfill({ status: 404, json: {} });
        if (url.pathname === '/api/runtime') value = { mode: 'local', provider: { name: 'fake' }, learning_features: { visual_selection: true }, plugins: [{ workflows: [{ kind: 'explanation' }, { kind: 'narration' }] }] };
        else if (url.pathname === '/api/sessions') value = { access_token: 'test-only' };
        else if (url.pathname === '/api/sessions/me') value = { session: { id: 'test', expires_at: new Date(Date.now() + 3600000).toISOString() }, limits: {} };
        else if (url.pathname === '/health/ready') value = { ready: true };
        else if (url.pathname === '/api/projects') value = { items: [{ id: 'project', title: 'Formula selection verification' }] };
        else if (url.pathname.endsWith('/documents')) value = { items: [doc] };
        else if (url.pathname === '/api/documents/doc') value = { document: doc };
        else if (url.pathname.endsWith('/content')) value = { text };
        else if (url.pathname === '/api/sources/resolve') value = { items: [{ resolved: true, text, citation: { file: 'main.tex' } }] };
        else if (url.pathname.endsWith('/explanations')) value = { items: [] };
        else if (url.pathname.endsWith('/narrations')) value = { items: [{ id: 'narr-test', title: 'Formula narration', created_at: new Date().toISOString(), segments: [{ text: '在分析内容时，通过 **Markd** 了解排版，再由 **E = \\frac{60+25+10+5}{100}\\times 100% = 10%** 计算结果。\n\n代码强调 `x+\\alpha`，标准公式 $x+\\alpha$。\n\n求和 **\\sum_{i=1}^{n}x_i**，积分 **\\int_0^1 x dx**，下标 **\\alpha_i**。\n\n```tex\n\\frac{a}{b}\n```' }] }] };
        else if (url.pathname.endsWith('/graph')) value = { nodes, edges: [] };
        else if (url.pathname.endsWith('/runs')) value = { items: [] };
        else if (method === 'POST' && url.pathname.endsWith('/nodes')) {
          const payload = request.postDataJSON(); posted.push(payload);
          const node = { ...payload, id: 'node-' + posted.length, metadata: payload.metadata || {}, created_at: new Date().toISOString() };
          nodes.push(node); value = node;
        } else { unexpected.push(method + ' ' + url.pathname); return route.fulfill({ status: 400, json: { message: 'Unexpected test API' } }); }
        if (url.pathname.endsWith('/narrations')) value.items.forEach(nar => nar.segments.forEach(segment => { segment.node_id = 'node-1'; }));
        return route.fulfill({ json: value });
      });
      await page.goto((process.env.APP_URL || 'http://127.0.0.1:8111') + '/workspace.html');
      await page.waitForFunction(() => window.TLM_WS?.state.doc?.id === 'doc' && document.querySelector('[data-math-source]'));
      const token = page.locator('#sheet [data-math-source]').first().locator('[data-tlm-start="6"]');
      const rect = await token.evaluate(el => {
        const range = document.createRange(); range.selectNodeContents(el);
        const box = range.getBoundingClientRect();
        return { left: box.left, right: box.right, y: box.top + box.height / 2 };
      });
      await page.mouse.move(rect.left + .1, rect.y); await page.mouse.down();
      await page.mouse.move(rect.right - .1, rect.y, { steps: 10 }); await page.mouse.up();
      await page.locator('#ask').waitFor({ state: 'visible' });
      await page.locator('[data-act="ask-extract"]').click();
      await page.waitForFunction(() => window.TLM_WS.state.nodes.length === 1);
      assert.equal(posted[0].metadata.source.quote, '\\alpha');
      const source = posted[0].metadata.source;
      assert.equal(text.slice(source.selector.start, source.selector.end), '\\alpha');
      assert.equal(source.selector.type, format === 'tex' ? 'tex-source' : 'text');
      if (format === 'tex') assert.equal(source.selector.file, 'main.tex');
      try { await page.locator('#sheet mark.anchor[data-node="node-1"]').waitFor({ state: 'attached', timeout: 3000 }); }
      catch (error) { console.error(await page.evaluate(() => ({ nodes: TLM_WS.state.nodes, targets: TutorMathSelection.targets(document.querySelector('#sheet'), 24, 30).length, html: document.querySelector('#sheet').innerHTML }))); throw error; }
      await page.locator('#sheet [data-math-source]').first().dblclick();
      await page.locator('#ask').waitFor({ state: 'visible' });
      assert.equal(await page.evaluate(() => TLM_WS.state.ask.quote), 'x_i^2+\\alpha=1');
      await page.waitForTimeout(180);
      await page.screenshot({ path: path.join(output, 'workspace-' + inputFormat + '.png') });
      await page.keyboard.press('Escape');
      await page.evaluate(() => {
        const node = TLM_WS.state.nodes[0];
        node.metadata.pages = ['exp'];
        TLM_WS.state.history[node.id] = [{ id: 'exp', node_id: node.id, answer: 'Follow up $x+\\alpha=1$.', created_at: new Date().toISOString() }];
        TLM_WS.select(node.id);
      });
      const answerToken = page.locator('.gnode.sel .answer [data-tlm-start="2"]').first();
      await answerToken.evaluate(el => {
        const range = document.createRange(); range.selectNodeContents(el);
        getSelection().removeAllRanges(); getSelection().addRange(range);
        el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      });
      await page.locator('.node-sel-bar [data-k="ask"]').click();
      assert.equal(await page.evaluate(() => TLM_WS.state.ask.quote), '\\alpha');
      await page.locator('[data-act="ask-extract"]').click();
      await page.waitForFunction(() => TLM_WS.state.nodes.length === 2);
      assert.equal(posted[1].metadata.source.fromNode, 'node-1');
      assert.equal(posted[1].metadata.source.quote, '\\alpha');
      await page.keyboard.press('Escape');
      await page.evaluate(() => {
        const math = document.querySelector('#sheet [data-math-source]');
        const range = document.createRange();
        const prose = math.previousSibling;
        range.setStart(prose, prose.textContent.lastIndexOf('Formula ') >= 0 ? prose.textContent.lastIndexOf('Formula ') : prose.textContent.lastIndexOf('First '));
        range.setEnd(math.nextSibling, 1);
        getSelection().removeAllRanges(); getSelection().addRange(range);
        math.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      });
      await page.locator('#ask').waitFor({ state: 'visible' });
      await page.locator('[data-act="ask-extract"]').click();
      await page.waitForFunction(() => TLM_WS.state.nodes.length === 3);
      const mixedSource = posted[2].metadata.source;
      const mixedExpected = (format === 'tex' ? 'Formula ' : 'First ') + '$x_i^2+\\alpha=1$.';
      assert.equal(mixedSource.quote, mixedExpected);
      assert.equal(mixedSource.selector.start, text.indexOf(mixedExpected));
      assert.equal(text.slice(mixedSource.selector.start, mixedSource.selector.end), mixedExpected);
      await page.keyboard.press('Escape');
      await page.locator('#sheet [data-math-source]').last().locator('[data-tlm-start="2"]').evaluate(el => {
        const range = document.createRange(); range.selectNodeContents(el);
        getSelection().removeAllRanges(); getSelection().addRange(range);
        el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      });
      await page.locator('#ask').waitFor({ state: 'visible' });
      await page.locator('[data-act="ask-extract"]').click();
      await page.waitForFunction(() => TLM_WS.state.nodes.length === 4);
      assert.equal(posted[3].metadata.source.quote, '\\alpha');
      assert.equal(posted[3].metadata.source.selector.start, text.lastIndexOf('\\alpha'));
      await page.keyboard.press('Escape');
      await page.locator('#sheet [data-math-source]').nth(-2).evaluate(el => {
        const range = document.createRange(); range.selectNodeContents(el);
        getSelection().removeAllRanges(); getSelection().addRange(range);
        el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      });
      await page.locator('#ask').waitFor({ state: 'visible' });
      await page.locator('[data-act="ask-extract"]').click();
      await page.waitForFunction(() => TLM_WS.state.nodes.length === 5);
      assert.equal(posted[4].title, screenshotFormula);
      assert.equal(posted[4].metadata.source.quote, screenshotFormula);
      const recoveredTitle = page.locator('.gnode.sel [data-title-view]');
      await recoveredTitle.locator('.katex .frac-line').waitFor();
      assert.match(await recoveredTitle.textContent(), /L.*S.*C.*H.*100%/);
      const oldTitle = screenshotFormula.split(' ').slice(0, 8).join(' ') + '…';
      if (inputFormat === 'md') {
        nodes[4].title = oldTitle;
        await page.reload();
        await page.waitForFunction(() => TLM_WS.state.nodes.length === 5 && TLM_WS.state.doc?.id === 'doc');
      }
      await page.evaluate(value => {
        TLM_WS.state.nodes[4].title = value;
        TLM_WS.select(TLM_WS.state.nodes[4].id);
      }, oldTitle);
      await recoveredTitle.locator('.katex .frac-line').waitFor();
      await recoveredTitle.click();
      assert.equal(await page.locator('.gnode.sel [data-title]').inputValue(), screenshotFormula);
      await page.locator('.gnode.sel [data-title]').evaluate(el => el.blur());
      await recoveredTitle.locator('.katex .frac-line').waitFor();
      if (inputFormat === 'md') {
        await recoveredTitle.screenshot({ path: path.join(output, 'fixed-engagement-formula.png') });
        const longFormula = '\\frac{' + Array(34).fill('x_i').join('+') + '}{n}';
        await page.evaluate(value => {
          const node = TLM_WS.state.nodes[4];
          node.metadata.source.quote = value;
          node.title = value.slice(0, 120) + '…';
          TLM_WS.select(node.id);
        }, longFormula);
        await recoveredTitle.locator('.katex .frac-line').waitFor();
        await page.evaluate(() => {
          const node = TLM_WS.state.nodes[4]; node.title = 'Custom engagement title';
          TLM_WS.select(node.id);
        });
        assert.equal(await recoveredTitle.textContent(), 'Custom engagement title');
        assert.equal(await recoveredTitle.locator('.katex').count(), 0);
      }
      console.log('Spaced formula creation, legacy truncated title recovery and edit display passed: ' + inputFormat);
      if (inputFormat === 'md') {
        const formulaTitle = 'E = \\frac{60+25+10+5}{100}\\times 100% = 10%';
        await page.evaluate(title => {
          TLM_WS.state.nodes[0].title = title;
          TLM_WS.select(TLM_WS.state.nodes[0].id);
        }, formulaTitle);
        const title = page.locator('.gnode.sel [data-title-view]');
        await title.locator('.katex .frac-line').waitFor();
        assert.match(await title.textContent(), /100%.*10%/);
        await title.click();
        assert.equal(await page.locator('.gnode.sel [data-title]').inputValue(), formulaTitle);
        await page.locator('.gnode.sel [data-title]').evaluate(el => el.blur());
        await title.locator('.katex').waitFor({ state: 'visible' });
        await page.screenshot({ path: path.join(output, 'node-title-desktop.png') });
        await page.setViewportSize({ width: 390, height: 844 });
        await page.waitForTimeout(150);
        const bounds = await title.evaluate(el => ({ width: el.clientWidth, mathWidth: el.querySelector('.node-title-math').getBoundingClientRect().width }));
        assert.ok(bounds.mathWidth <= bounds.width + 1, JSON.stringify(bounds));
        await page.screenshot({ path: path.join(output, 'node-title-mobile.png') });
        await page.setViewportSize({ width: 1440, height: 1000 });
        for (const formula of ['\\int_0^1 x dx', '\\alpha_i', '\\sum_{i=1}^{n}x_i']) {
          await page.evaluate(value => {
            TLM_WS.state.nodes[0].title = value;
            TLM_WS.select(TLM_WS.state.nodes[0].id);
          }, formula);
          await title.locator('.katex').waitFor({ state: 'visible' });
          assert.equal(await title.locator('.katex-error').count(), 0);
          assert.equal(await title.locator('.node-title-math').count(), 1);
          await title.click();
          assert.equal(await page.locator('.gnode.sel [data-title]').inputValue(), formula);
          await page.locator('.gnode.sel [data-title]').evaluate(el => el.blur());
          await title.locator('.katex').waitFor({ state: 'visible' });
        }
        await title.screenshot({ path: path.join(output, 'node-title-sum.png') });
        await page.locator('.gnode.sel [data-act="node-collapse"]').click();
        await page.locator('.gnode[data-id="node-1"] h4 .katex .mop').first().waitFor({ state: 'attached' });
        console.log('Node title: fraction, percentages, edit/blur, collapsed and mobile checks passed');
        await page.locator('[data-act="narrate"]').click();
        const preview = page.locator('#narr-prev');
        await preview.locator('strong .katex .frac-line').waitFor();
        assert.equal(await preview.locator('strong').first().textContent(), 'Markd');
        assert.match(await preview.locator('strong').nth(1).textContent(), /100%.*10%/);
        assert.equal(await preview.locator('pre code').textContent(), '\\frac{a}{b}\n');
        assert.equal(await preview.locator('code .katex').count(), 1);
        assert.equal(await preview.locator('.katex').count(), 6);
        assert.equal(await preview.locator('strong .katex').count(), 4);
        assert.equal(await page.locator('.inspector__narration .katex').count(), 6);
        await preview.screenshot({ path: path.join(output, 'narration-formula-desktop.png') });
        await page.setViewportSize({ width: 390, height: 844 });
        await preview.scrollIntoViewIfNeeded();
        await preview.screenshot({ path: path.join(output, 'narration-formula-mobile.png') });
        console.log('Narration: highlighted formula, inline code, standard math and fenced code checks passed');
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(unexpected, []);
      console.log(JSON.stringify({ inputFormat, source, doubleClick: 'whole formula', answerFollowUp: 'single symbol', errors }));
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
