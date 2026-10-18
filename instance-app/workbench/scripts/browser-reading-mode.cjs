const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(__dirname, '../assets');
(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 850 } });
      await page.setContent('<style>#reader{height:600px;overflow:auto}#sheet{padding:12px}p{line-height:2;font-size:18px}.icon-btn{width:28px;height:28px}</style><div id="reader"><div id="sheet"></div></div>');
      for (const file of ['markdown-it/markdown-it.min.js', 'markdown.js', 'md-ir.js'])
        await page.addScriptTag({ path: path.join(assets, file) });
      await page.evaluate(() => {
        window.source = 'PageRank 的 **稳态分布** 满足 $x = Ax$，不能把前提与结论颠倒。\n\n第二段用于检验正常阅读。';
        window.sheet = document.querySelector('#sheet');
        const renderer = TutorMarkdown.createRenderer({ math: text => '<span class="formula">' + text + '</span>' });
        window.paint = () => { sheet.innerHTML = renderer.render(source); };
        paint();
        window.commits = [];
        window.editor = TLMMarkdownIR.create({ sheet, enabled: () => true, source: () => source, protectedIn: () => [],
          notify: () => {}, commit: update => {
            commits.push(update);
            source = source.slice(0, update.start) + update.text + source.slice(update.end);
            paint();
          } });
      });
      const paragraph = page.locator('p').first();
      const initial = await paragraph.boundingBox();
      await paragraph.hover();
      await paragraph.click();
      assert.equal(await page.locator('textarea').count(), 0);
      const after = await paragraph.boundingBox();
      assert.equal(after.height, initial.height, 'Hover/click must not reflow paragraphs');
      assert.equal(after.width, initial.width);
      const box = await paragraph.boundingBox();
      await page.mouse.move(box.x + 2, box.y + 12);
      await page.mouse.down();
      await page.mouse.move(box.x + Math.min(box.width - 35, 250), box.y + 12, { steps: 15 });
      await page.mouse.up();
      assert(await page.evaluate(() => !getSelection().isCollapsed), 'Text dragging stays available');
      assert.equal(await page.locator('textarea').count(), 0);
      await paragraph.hover();
      await page.getByRole('button', { name: '编辑这一段', exact: true }).click();
      assert.equal(await page.locator('textarea').count(), 1);
      await page.locator('textarea').fill('修改后的 **PageRank** 内容。');
      await page.locator('textarea').press('Control+Enter');
      assert.equal(await page.locator('textarea').count(), 0);
      assert.equal(await page.locator('p').first().innerText(), '修改后的 PageRank 内容。');
      assert.equal(await page.evaluate(() => commits.length), 1);
      await page.screenshot({ path: path.join(process.env.TEMP || process.env.TMPDIR || '/tmp', `reading-mode-${width}.png`) });
      await page.close();
    }
    console.log('PASS: hover/click stay rendered, stable layout, text selection, explicit edit, save and render; desktop/mobile');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
