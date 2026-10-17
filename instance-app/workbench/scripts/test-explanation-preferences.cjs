const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../assets/runtime-workspace.js'), 'utf8');

function fixture(fields) {
  const context = { prefs: { style: 'rigorous', length: 'short', lang: 'en' },
    api: { runtime: { plugins: [{ workflows: [{ kind: 'explanation', input_schema: { properties: fields } }] }] } } };
  const start = source.indexOf('  const LENGTH_HINT =');
  const end = source.indexOf('  function applyPrefs()', start);
  vm.runInNewContext(source.slice(start, end), context);
  return context;
}

test('all style controls are sent as structured preferences, separate from the question', () => {
  const c = fixture({ preferences: {}, response_mode: {} });
  for (const style of ['auto', 'plain', 'rigorous', 'example', 'analogy', 'socratic']) {
    c.prefs.style = style;
    const input = c.explanationInput('node', '为什么可以这样变形？', 'expand');
    assert.equal(input.preferences.style, style);
    assert.equal(input.preferences.length, 'short');
    assert.equal(input.preferences.language, 'en');
    assert.equal(input.question, '为什么可以这样变形？');
    assert.equal(input.response_mode, 'expand');
  }
});

test('regeneration and empty questions still carry the selected style', () => {
  const c = fixture({ preferences: {} });
  const input = c.explanationInput('node', '', 'auto');
  assert.equal(input.preferences.style, 'rigorous');
  assert.equal(input.question, undefined);
  assert.equal(input.response_mode, undefined);
});

test('invalid cached preferences use known defaults', () => {
  const c = fixture({ preferences: {} });
  c.prefs = { style: 'unknown', length: 'huge', lang: 'unknown' };
  const p = c.explanationInput('node').preferences;
  assert.equal(p.style, 'auto'); assert.equal(p.length, 'medium'); assert.equal(p.language, 'auto');
});

test('older servers receive only supported fields and keep the legacy style hint', () => {
  const c = fixture({ response_mode: {} });
  const input = c.explanationInput('node', '问题', 'expand');
  assert.equal(input.preferences, undefined);
  assert.match(input.question, /数学推导/);
  assert.equal(input.response_mode, 'expand');
  assert(input.question.length <= 1000);
});
