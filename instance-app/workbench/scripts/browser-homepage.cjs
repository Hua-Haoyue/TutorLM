/* Public landing-page assets only; no application sessions or model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8111';
const output = path.resolve(__dirname, '../.work/homepage-smoke');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  try {
    for (const width of [1440, 390]) for (const reduce of [false, true]) {
      const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: reduce ? 'reduce' : 'no-preference' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(origin + '/index.html');
      await page.waitForFunction(() => document.querySelector('video')?.readyState >= 2);
      if (!reduce) {
        await page.waitForSelector('#cover-brand svg');
        await page.waitForFunction(() => document.querySelector('video').currentTime > .2);
      }
      await page.evaluate(() => document.fonts.ready);
      const state = await page.evaluate(() => {
        const video = document.querySelector('video');
        return { paused: video.paused, time: video.currentTime, visible: getComputedStyle(video).display,
          poster: video.poster, overflow: document.documentElement.scrollWidth > innerWidth,
          brand: document.querySelector('#cover-brand').getBoundingClientRect().toJSON(),
          tagline: document.querySelector('#cover-tagline').getBoundingClientRect().toJSON() };
      });
      assert.equal(state.overflow, false);
      for (const rect of [state.brand, state.tagline]) {
        assert(rect.left >= 0 && rect.right <= width && rect.width > 0, 'Brand text must fit the viewport');
      }
      assert.notEqual(state.visible, 'none');
      if (reduce) { assert(state.paused); assert.equal(state.time, 0); assert(state.poster); }
      else assert(!state.paused);
      assert.deepEqual(errors, []);
      await page.screenshot({ path: path.join(output, `home-${width}-${reduce ? 'static' : 'video'}.png`) });
      console.log('PASS homepage', width, reduce ? 'reduced motion poster' : 'video + SVG title');
      await page.close();
    }
    const page = await browser.newPage();
    await page.route('**/cover-brand.svg', route => route.abort());
    await page.route('**/cover-tagline.svg', route => route.abort());
    await page.goto(origin + '/index.html');
    assert.equal(await page.locator('#cover-brand').textContent(), 'TutorLM');
    assert((await page.locator('#cover-tagline').textContent()).includes('懂'));
    console.log('PASS missing SVGs retain fallback titles');
    await page.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
