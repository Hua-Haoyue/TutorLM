/* Disposable standalone fake backend only. Never runs code or real model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.APP_URL || 'http://127.0.0.1:8131';
const output = path.resolve(__dirname, '../.work/workspace-tools');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  let docId;
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin + '/index.html');
    assert.equal((await page.evaluate(() => RuntimeAPI.get('/api/runtime', { auth:false }))).provider.name, 'fake');
    await page.waitForFunction(() => !document.querySelector('#invite-field').hidden);
    await page.fill('#invite', process.env.APP_INVITE || 'TLM-DEV-LOCAL');
    await page.click('#start'); await page.waitForURL('**/workspace.html');
    await page.waitForFunction(() => window.TLM_WS?.state.project && window.TLM_WS.state.connected);
    await page.locator('#file-input').setInputFiles({ name:'agent-main.py', mimeType:'text/x-python',
      buffer:Buffer.from('def greet_user(name):\n    return "Hello " + name\n\nprint(greet_user("Reader"))\n') });
    await page.waitForSelector('.code-source .hljs-keyword');
    docId = await page.evaluate(() => window.TLM_WS.state.doc.id);
    assert.equal(await page.evaluate(() => window.TLM_WS.state.mode), 'code');
    const indexed = await page.evaluate(() => RuntimeAPI.get('/api/code/search?query=greet_user'));
    assert(indexed.items.length >= 2);
    await page.getByRole('button', { name:'编辑代码', exact:true }).click();
    const editor = page.getByRole('textbox', { name:'代码编辑器', exact:true });
    await editor.fill('def test():'); await editor.press('Enter');
    assert.equal(await editor.inputValue(), 'def test():\n    ');
    await editor.press('Tab'); assert.equal(await editor.inputValue(), 'def test():\n        ');
    await editor.fill('gre'); await editor.press('Tab'); assert.equal(await editor.inputValue(), 'greet_user');
    await editor.fill('def greet_user(name):\n    return "Welcome " + name\n');
    await page.getByRole('button', { name:'保存代码', exact:true }).click();
    await page.waitForFunction(() => window.TLM_WS.state.text.includes('Welcome'));
    await page.waitForSelector('.code-source .hljs-keyword');
    await page.click('#prefs-btn');
    await page.getByLabel('思考强度', {exact:true}).selectOption('high');
    await page.getByLabel('联网搜索', {exact:true}).check();
    await page.getByRole('button', {name:'完成',exact:true}).click();
    const node = await page.evaluate(async id => RuntimeAPI.post('/api/projects/' + window.TLM_WS.state.project.id + '/nodes', {
      document_id:id,title:'greet_user',content:'greet_user',kind:'concept',metadata:{source:{quote:'greet_user'}},
    }), docId);
    await page.evaluate(async () => window.TLM_WS.refresh());
    await page.evaluate(id => window.TLM_WS.select(id), node.id);
    const posted = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
    await page.locator('.gnode.sel [data-act="node-explain"]').click();
    const input = (await posted).request().postDataJSON().input;
    assert.equal(input.preferences.model.effort, 'high'); assert.equal(input.preferences.model.web_search, true);
    await page.waitForFunction(id => window.TLM_WS.state.history[id]?.length > 0, node.id);
    const evidence = await page.evaluate(id => window.TLM_WS.state.history[id][0].learning, node.id);
    assert(evidence.code_citations.length);
    await page.waitForFunction(() => document.querySelector('#st-state').textContent === '已同步');
    await page.waitForTimeout(200);
    // Zoom samples must include intermediate geometry, not just start/end widths.
    await page.evaluate(() => { window.zoomWidths=[];
      function sample() { window.zoomWidths.push(document.querySelector('.gnode.sel').getBoundingClientRect().width); if(window.zoomWidths.length<50) requestAnimationFrame(sample); }
      requestAnimationFrame(sample);
    });
    await page.getByRole('button', {name:'放大卡片',exact:true}).click();
    await page.waitForTimeout(600);
    const widths = await page.evaluate(() => window.zoomWidths);
    assert(new Set(widths.map(v=>Math.round(v))).size > 3, 'Card zoom requires intermediate animation frames');
    await page.evaluate(() => { window.shrinkWidths=[];
      function sample() { window.shrinkWidths.push(document.querySelector('.gnode.sel').getBoundingClientRect().width); if(window.shrinkWidths.length<50) requestAnimationFrame(sample); }
      requestAnimationFrame(sample);
    });
    await page.getByRole('button', {name:'恢复正常大小',exact:true}).click();
    await page.waitForTimeout(600);
    const shrink = await page.evaluate(() => window.shrinkWidths);
    assert(new Set(shrink.map(v=>Math.round(v))).size > 3 && shrink[0] > shrink.at(-1), 'Card shrinking must animate too');
    await page.evaluate(async ({docId, root}) => {
      const base = '/api/projects/' + window.TLM_WS.state.project.id;
      const add = (title, parent_id) => RuntimeAPI.post(base+'/nodes', {document_id:docId,title,content:title,parent_id,kind:'concept'});
      const a = await add('Parse request', root), b = await add('Validate input', root);
      const leaf = await add('Store result', a.id);
      await RuntimeAPI.post(base+'/graph/edges', {source_node_id:b.id,target_node_id:leaf.id,relation:'supports'});
      await RuntimeAPI.post(base+'/graph/edges', {source_node_id:root,target_node_id:leaf.id,relation:'supports'});
      await window.TLM_WS.refresh();
    }, {docId,root:node.id});
    await page.click('[data-act="auto-layout"]');
    await page.waitForFunction(() => window.TLM_WS.state.nodes.filter(n => n.document_id === window.TLM_WS.state.doc.id)
      .every(n => n.metadata.layout_routes));
    await page.waitForTimeout(700);
    assert(await page.locator('#edge-g path[d*="L"]').count() >= 4, 'Automatic layout must render routed dependency paths');
    await page.screenshot({path:path.join(output,'layout.png')});
    await page.click('#workspace-chat-toggle');
    const panel = page.locator('#workspace-chat'); await panel.waitFor();
    await panel.getByRole('textbox', {name:'Chat 消息'}).fill('请概括代码工程');
    await panel.getByRole('button', {name:'发送消息',exact:true}).click();
    await page.waitForSelector('.workspace-chat-message.assistant:not(.waiting)');
    assert((await panel.innerText()).includes('离线 Chat'));
    const before = await panel.boundingBox(), head = await panel.locator('header').boundingBox();
    await page.mouse.move(head.x+55,head.y+15); await page.mouse.down(); await page.mouse.move(head.x-140,head.y+90,{steps:10}); await page.mouse.up();
    const moved = await panel.boundingBox(); assert(Math.abs(moved.x-before.x)>80);
    await page.mouse.move(moved.x+moved.width-3,moved.y+moved.height-3); await page.mouse.down();
    await page.mouse.move(moved.x+moved.width+80,moved.y+moved.height+55,{steps:10}); await page.mouse.up();
    const resized = await panel.boundingBox(); assert(resized.width > moved.width+30);
    await page.waitForTimeout(3000);
    await page.screenshot({path:path.join(output,'desktop.png')});
    await panel.getByRole('button',{name:'关闭 Chat',exact:true}).click();
    await page.click('#workspace-chat-toggle'); assert((await panel.innerText()).includes('离线 Chat'));
    await page.setViewportSize({width:390,height:844}); await page.waitForTimeout(400);
    const mobile = await panel.boundingBox(); assert(mobile.x >= 0 && mobile.x+mobile.width <= 391);
    await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    assert.deepEqual(errors, []);
    console.log('PASS code upload/highlight/index/edit/tab completion/indent; model prefs; code citations; animated card zoom; Chat message/drag/resize/reopen/mobile');
  } finally {
    if(docId) await page.evaluate(id=>RuntimeAPI.del('/api/documents/'+id),docId);
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
