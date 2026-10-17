const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const assets = path.resolve(__dirname, '../assets');
const source = fs.readFileSync(path.join(assets, 'runtime-workspace.js'), 'utf8');

function fixture() {
  const context = {
    katex: require('../assets/katex/katex.min.js'),
    markdownit: require('../assets/markdown-it/markdown-it.min.js'),
  };
  context.window = context;
  vm.createContext(context);
  for (const name of ['math-selection.js', 'markdown.js']) {
    vm.runInContext(fs.readFileSync(path.join(assets, name), 'utf8'), context);
  }
  const renderer = context.TutorMarkdown.createRenderer({ math: (value, display) => context.TutorMathSelection.render(context.katex, value, display) });
  context.inline = value => renderer.inline(value);
  vm.runInContext(source.slice(source.indexOf('  const collapse ='), source.indexOf('  function download(')), context);
  vm.runInContext(source.slice(source.indexOf('  const inlineMd ='), source.indexOf('  function splitAnswer(')), context);
  return context;
}

const formula = 'E = \\frac{L + S + C + H}{V} \\times 100\\%';

test('formula titles preserve spaces and balanced fractions when created', () => {
  const context = fixture();
  assert.equal(context.initialNodeTitle(formula), formula);
  assert.match(context.nodeTitle(formula), /frac-line/);
  assert.match(context.nodeTitle(formula), /100/);
});

test('old automatic formula abbreviations recover from their full source', () => {
  const context = fixture();
  const abbreviated = context.guessTitle(formula);
  const node = { metadata: { source: { quote: formula } } };
  assert.notEqual(abbreviated, formula);
  assert.equal(context.formulaTitleSource(abbreviated, node), formula);
  assert.match(context.nodeTitle(abbreviated, node), /frac-line/);
  assert.equal(context.formulaTitleSource('Custom title', node), 'Custom title');
  assert.equal(context.nodeTitle('Custom title', node), 'Custom title');
});

test('long legacy formulas render without changing stored titles or sources', () => {
  const context = fixture();
  for (const length of [34, 80]) {
    const full = '\\frac{' + Array(length).fill('x_i').join('+') + '}{n}';
    const title = context.guessTitle(full);
    const node = { title, metadata: { source: { quote: full } } };
    assert.equal(context.formulaTitleSource(title, node), full);
    assert.match(context.nodeTitle(title, node), /frac-line/);
    assert.equal(node.title, title);
    assert.equal(node.metadata.source.quote, full);
    assert.ok(context.initialNodeTitle(full).length <= 200);
  }
});

test('scripted commands, percentages and explicit math delimiters render', () => {
  const context = fixture();
  for (const value of ['\\sum_{i=1}^n x_i', '\\int_0^1 x dx', '\\alpha_i', 'E=\\frac{1}{10}\\times100%=10%', '$x_i^2$']) {
    assert.match(context.nodeTitle(value), /class="katex"/);
    assert.doesNotMatch(context.nodeTitle(value), /katex-error/);
  }
});

test('incomplete formulas and custom source-like titles stay unchanged', () => {
  const context = fixture();
  const invalid = 'E = \\frac{L + S';
  const node = { metadata: { source: { quote: formula } } };
  assert.equal(context.formulaTitleSource(invalid, node), invalid);
  assert.doesNotMatch(context.nodeTitle(invalid, node), /class="katex"/);
  assert.doesNotMatch(context.nodeTitle('<img src=x onerror=alert(1)>'), /<img/);
});
