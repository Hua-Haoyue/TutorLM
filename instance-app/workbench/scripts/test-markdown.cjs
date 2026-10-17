const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createRenderer } = require('../assets/markdown.js');
const renderer = createRenderer();

test('GFM tables render alignment, inline styles, escaped pipes and individual source ranges', () => {
  const source = '# 表格\r\n\r\n| 名称 | 数量 |\r\n| :--- | ---: |\r\n| **测试** | 123 |\r\n| a\\|b | `code` |\r\n';
  const html = renderer.render(source);
  assert.match(html, /<table>/);
  assert.match(html, /<thead>/);
  assert.match(html, /<tbody>/);
  assert.match(html, /text-align:right/);
  assert.match(html, /<strong>测试<\/strong>/);
  assert.match(html, /a\|b/);
  assert.match(html, /<code>code<\/code>/);
  const cells = [...html.matchAll(/<(?:th|td)\b[^>]*data-start="(\d+)" data-end="(\d+)"[^>]*>/g)];
  assert.equal(cells.length, 6);
  assert.equal(source.slice(Number(cells[3][1]), Number(cells[3][2])).trim(), '123');
  assert.equal(source.slice(Number(cells[4][1]), Number(cells[4][2])).trim(), 'a\\|b');
});

test('heading source range belongs to the heading, not the next paragraph', () => {
  const source = '# 标题\n\n正文\n';
  const html = renderer.render(source);
  assert.match(html, /<h1[^>]*data-start="0" data-end="5"/);
  assert.match(html, /<p data-b="1" data-start="6"/);
});

test('nested lists, ordered starts, task lists, strikethrough and setext headings work', () => {
  const html = renderer.render('Heading\n=======\n\n3. third\n   - nested\n\n- [ ] pending\n- [x] done\n\n~~removed~~');
  assert.match(html, /<h1/);
  assert.match(html, /<ol start="3">/);
  assert.match(html, /nested/);
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.match(html, /disabled checked/);
  assert.match(html, /<s>removed<\/s>/);
});

test('fences retain language and escaping while quotes contain real blocks', () => {
  const html = renderer.render('~~~js\n<script>alert(1)</script>\n~~~\n\n> **quote**\n>\n> second paragraph');
  assert.match(html, /<pre data-b=/);
  assert.match(html, /class="language-js"/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<strong>quote<\/strong>/);
});

test('math uses the existing renderer but never consumes code or escaped dollars', () => {
  const md = createRenderer({ math: value => `<i>${value}</i>` });
  assert.match(md.inline('$x^2$'), /class="math"><i>x\^2<\/i>/);
  assert.match(md.render('$$\na + b\n$$'), /class="math-block"><i>a \+ b<\/i>/);
  assert.equal(md.inline('`$x$`'), '<code>$x$</code>');
  assert.equal(md.inline('\\$x$'), '$x$');
});

test('untrusted HTML and dangerous URLs are inert; images do not cause network requests', () => {
  const html = renderer.render('<img src=x onerror=alert(1)>\n\n[bad](javascript:alert(1))\n\n[bad](data:text/html,evil)\n\n![alt](https://example.test/a.png)\n\n[safe](https://example.test)');
  assert.doesNotMatch(html, /<(?:img|script|iframe)\b/);
  assert.doesNotMatch(html, /href="(?:javascript:|data:)/);
  assert.match(html, /noopener noreferrer/);
  assert.match(html, /\[图片：alt\]/);
  assert.match(html, /&lt;img/);
});

test('generated answers omit source anchors and table lookalikes stay plain text', () => {
  assert.doesNotMatch(renderer.render('|a|b|\n|-|-|\n|c|d|', { blocks: false }), /data-b|data-start/);
  assert.doesNotMatch(renderer.render('a | b\nnot a separator'), /<table>/);
  assert.equal(renderer.inline('[label](https://example.test "title")').includes('title="title"'), true);
});

test('tight list entries retain selectable source ranges', () => {
  const html = renderer.render('- first item\n- [x] second item\n');
  assert.match(html, /<li data-b="0" data-start="0" data-end="13">/);
  assert.match(html, /<li data-b="1"[^>]*class="task-list-item"/);
});
