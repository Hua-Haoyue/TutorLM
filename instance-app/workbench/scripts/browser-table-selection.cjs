const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(__dirname, '../assets');
(async()=>{
 const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL || 'msedge',headless:true});
 try {for(const width of [1280,390]) {
  const page=await browser.newPage({viewport:{width,height:850}});
  await page.setContent('<div id="sheet"></div>');
  for(const file of ['markdown-it/markdown-it.min.js','markdown.js','md-ir.js','table-selection.js']) await page.addScriptTag({path:path.join(assets,file)});
  await page.evaluate(()=>{
   window.source='| 名称 | 说明 |\n| --- | --- |\n| 梯度 | **局部变化方向** |\n| 学习率 | 每次更新的步长 |\n';
   const renderer=TutorMarkdown.createRenderer();
   window.sheet=document.querySelector('#sheet');
   sheet.innerHTML=renderer.render(source);
   window.editor=TLMMarkdownIR.create({sheet,enabled:()=>true,source:()=>source,protectedIn:()=>[],commit:()=>{},notify:()=>{}});
  });
  await page.locator('td').first().click();
  assert.equal(await page.locator('textarea').count(),0,'Click must not trap selection in editor');
  const firstBox=await page.locator('td').first().boundingBox();
  const lastBox=await page.locator('td').last().boundingBox();
  await page.mouse.move(firstBox.x+2,firstBox.y+firstBox.height/2);
  await page.mouse.down();
  await page.mouse.move(lastBox.x+lastBox.width-2,lastBox.y+lastBox.height/2,{steps:20});
  await page.mouse.up();
  assert(await page.evaluate(()=>!!TLMTableSelection.selection(getSelection().getRangeAt(0),sheet,source)), 'Mouse drag must select across cells');
  const result=await page.evaluate(()=>{
   const cells=sheet.querySelectorAll('td');
   const range=document.createRange();range.setStart(cells[0].firstChild,0);range.setEnd(cells[3].firstChild,4);
   const result=TLMTableSelection.selection(range,sheet,source);
   if(result.quote!==source.slice(result.exactStart,result.exactEnd)) throw Error('Citation mismatch');
   if(!result.tableContext.includes('名称')||!result.tableContext.includes('学习率')||!result.quote.includes('**局部变化方向**')) throw Error('Lost table structure');
   const single=document.createRange();single.selectNodeContents(cells[0]);
   if(TLMTableSelection.selection(single,sheet,source)!==null) throw Error('Single-cell regression');
   return result;
  });
  assert(result.tableContext.includes('| --- | --- |'));
  await page.getByRole('button',{name:'编辑表格'}).click();
  assert.equal(await page.locator('textarea').count(),1);
  await page.evaluate(()=>editor.finish());
  await page.screenshot({path:path.join(process.env.TEMP || process.env.TMPDIR || '/tmp',`table-selection-${width}.png`)});
  await page.close();
 } console.log('PASS table selection: desktop/mobile, cross-cell/row, exact citation, header context, explicit editing');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
