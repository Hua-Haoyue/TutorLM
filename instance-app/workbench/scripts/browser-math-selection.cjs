const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../assets');
const output = path.resolve(__dirname, '../.work/math-selection');
fs.mkdirSync(output, { recursive: true });
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    await page.setContent('<main id="fixture" style="font-size:24px;padding:30px"></main>');
    await page.addStyleTag({ path: path.join(root, 'katex/katex.min.css') });
    for (const file of ['katex/katex.min.js', 'markdown-it/markdown-it.min.js', 'math-selection.js', 'markdown.js']) await page.addScriptTag({ path: path.join(root, file) });
    const result = await page.evaluate(() => {
      const renderer = TutorMarkdown.createRenderer({ math: (source, display) => TutorMathSelection.render(katex, source, display) });
      const fixture = document.querySelector('#fixture');
      const cases = [];
      function check(source, selected, expected, format = 'md') {
        fixture.innerHTML = format === 'txt' ? renderer.text(source) : renderer.render(source);
        if (fixture.querySelector('.katex-error')) throw new Error('Render error: ' + source);
        const formulas = [...fixture.querySelectorAll('[data-math-source]')];
        const math = formulas.at(-1);
        const tokens = [...math.querySelectorAll('[data-tlm-start]')];
        const chosen = selected === null ? [math] : tokens.filter(el => selected.includes(math.dataset.mathSource.slice(Number(el.dataset.tlmStart), Number(el.dataset.tlmEnd))));
        if (!chosen.length) throw new Error('Missing mapped token: ' + JSON.stringify({ source, selected, math: math.outerHTML }));
        const range = document.createRange();
        range.setStartBefore(chosen[0]); range.setEndAfter(chosen.at(-1));
        const value = TutorMathSelection.selection(range, math.closest('[data-b]') || fixture);
        if (value?.quote !== expected) throw new Error(JSON.stringify({ source, selected, expected, actual: value?.quote, html: math.innerHTML }));
        if (source.slice(value.start, value.end) !== expected) throw new Error('Wrong source offset: ' + JSON.stringify(value));
        cases.push({ source, selected, quote: value.quote, start: value.start, end: value.end });
      }
      check('Greek $x+\\alpha=2$.', ['\\alpha'], '\\alpha');
      check('Power $x_i^2+y$.', ['x', 'i'], 'x_i');
      check('Fraction $\\frac{a+b}{c}=d$.', ['a', '+', 'b'], 'a+b');
      check('Fraction $\\frac{a+b}{c}=d$.', ['a', '+', 'b', 'c'], '\\frac{a+b}{c}');
      check('Subscript $x_{i+1}=d$.', ['x', 'i', '+', '1'], 'x_{i+1}');
      check('Root $\\sqrt{x}$.', ['x'], 'x');
      check('Root $\\sqrt{x}$.', ['\\sqrt'], '\\sqrt');
      check('Style $\\mathbf{x}$.', ['x'], 'x');
      check('Sum $\\sum_{i=1}^n x_i$.', ['\\sum'], '\\sum');
      check('Function $\\sin(x)+\\cos(y)$.', ['\\sin'], '\\sin');
      check('Integral $\\int_0^1 x dx$.', ['\\int'], '\\int');
      check('Equation $x^2+\\alpha=1$.', null, 'x^2+\\alpha=1');
      check('$$\n\\frac{a+b}{c}=d\n$$', null, '\\frac{a+b}{c}=d');
      check('Windows\r\n\r\n$$\r\nx+\r\n\\alpha=1\r\n$$', ['\\alpha'], '\\alpha');
      check('Windows\r\n$$x+\r\n\\alpha=1$$', ['\\alpha'], '\\alpha', 'txt');
      check('`$x+1$`\n\nRepeated $x+1$.\n\nAgain $x+1$.', ['x'], 'x');
      check('`$x+1$` and $x+1$.', ['x'], 'x');
      check('Plain **text** $x+\\alpha$\nnext', ['\\alpha'], '\\alpha', 'txt');
      check('\\documentclass{article}\n\\begin{document}\n\\(x_i+\\alpha\\)\n\\end{document}', ['\\alpha'], '\\alpha', 'txt');
      check('\\begin{equation}x_i+\\alpha=1\\end{equation}', ['\\alpha'], '\\alpha', 'txt');
      check('\\begin{align}x&=1\\\\y&=2\\end{align}', ['y'], 'y', 'txt');
      const mixed = [
        ['Given $x+\\alpha=1$ then $x+\\alpha=1$ done.', 'Given ', ' then ', 'Given $x+\\alpha=1$ then'],
        ['Given $x+\\alpha=1$ then $x+\\alpha=1$ done.', '\\alpha', ' done.', '\\alpha=1$ then $x+\\alpha=1$ done.'],
        ['Use **bold** and $x+\\alpha$ today.', 'bold', '\\alpha', 'bold** and $x+\\alpha'],
        ['Use &amp; and $x+\\alpha$ today.', 'Use & and ', '\\alpha', 'Use &amp; and $x+\\alpha'],
      ];
      for (const [source, from, to, expected] of mixed) {
        fixture.innerHTML = renderer.render(source);
        const block = fixture.querySelector('[data-b]');
        const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT), texts = [];
        while (walker.nextNode()) texts.push(walker.currentNode);
        const find = (value, last) => value === '\\alpha'
          ? [...block.querySelectorAll('[data-tlm-start]')].find(el => el.textContent === 'α').firstChild
          : texts.find(node => node.textContent === value);
        const a = find(from, false), b = find(to, true);
        if (!a || !b) throw new Error('Missing mixed fixture node');
        const range = document.createRange(); range.setStart(a, 0); range.setEnd(b, b.textContent.length);
        const value = TutorMathSelection.selection(range, block, source);
        if (value?.quote !== expected || value.start !== source.indexOf(expected) || value.end !== source.indexOf(expected) + expected.length) throw new Error(JSON.stringify({source, expected, value}));
        cases.push({ source, quote: value.quote, start: value.start, end: value.end });
      }
      const dimensions = [];
      for (const source of ['x_i^2+\\alpha=1', '\\frac{a+b}{c}+d', '\\sum_{i=1}^{n}x_i', '\\sqrt{x+1}', '\\left( x+1 \\right)', '\\text{hello world}+x', '\\begin{aligned}x&=1\\\\y&=2\\end{aligned}']) {
        fixture.innerHTML = `<span id="before" style="display:inline-block">${katex.renderToString(source, { output: 'html', throwOnError: false })}</span><br><span id="after" style="display:inline-block">${TutorMathSelection.render(katex, source)}</span>`;
        const a = document.querySelector('#before').getBoundingClientRect(), b = document.querySelector('#after').getBoundingClientRect();
        dimensions.push({ source, dw: b.width - a.width, dh: b.height - a.height });
        if (fixture.querySelector('.katex-error')) throw new Error('Render regression: ' + source);
      }
      return { cases, dimensions };
    });
    for (const value of result.dimensions) { assert.ok(Math.abs(value.dw) < 2, JSON.stringify(value)); assert.ok(Math.abs(value.dh) < 2, JSON.stringify(value)); }
    console.log(JSON.stringify(result, null, 2));
    await page.screenshot({ path: path.join(output, 'math-test.png') });
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
