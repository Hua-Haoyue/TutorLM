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

test('rendered selections spanning Markdown emphasis and parentheses map to exact raw source spans', () => {
  const source = '**自由意志**（free will），在本文中指一种特殊的控制能力。';
  const projected = renderer.project(source);
  const selected = '自由意志（free will），在本文中指一种特殊的控制能力。';
  assert.equal(projected.text, selected);
  assert.equal(source.slice(projected.starts[0], projected.ends[selected.length - 1]),
    '自由意志**（free will），在本文中指一种特殊的控制能力。');
});

test('long selections across formatting, links and code keep the correct UTF-16 offsets', () => {
  const source = '这句包含**重点**、[来源](https://example.test/重点)和`代码`，最后还有😀。';
  const projected = renderer.project(source);
  assert.equal(projected.text, '这句包含重点、来源和代码，最后还有😀。');
  assert.equal(source.slice(projected.starts[0], projected.ends[projected.text.length - 1]), source);
  const later = renderer.project('[入口](https://example.test/重复) 重复');
  const at = later.text.lastIndexOf('重复');
  assert.equal(later.text, '入口 重复');
  assert.equal(later.starts[at], '[入口](https://example.test/重复) 重复'.lastIndexOf('重复'));
});

test('escaped punctuation and HTML entities map to their complete raw spelling', () => {
  const source = '\\*术语\\* &amp; 后续';
  const projected = renderer.project(source);
  assert.equal(projected.text, '*术语* & 后续');
  assert.equal(source.slice(projected.starts[0], projected.ends[projected.text.length - 1]), source);
});

test('line endings and link titles do not shift later visible words', () => {
  const source = '第一行**重点**\r\n第二行[链接](https://example.test/a "含有 ) 的标题")之后。';
  const projected = renderer.project(source);
  assert.equal(projected.text, '第一行重点\n第二行链接之后。');
  const at = projected.text.indexOf('之后');
  assert.equal(projected.starts[at], source.indexOf('之后'));
  assert.equal(source.slice(projected.starts[at], projected.ends[at + 1]), '之后');
});
