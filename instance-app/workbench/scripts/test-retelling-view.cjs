const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../assets/runtime-workspace.js'), 'utf8');

function fixture() {
  const original = { id: 'original', answer: 'Original', created_at: '2026-10-17T01:00:00Z' };
  const retelling = { id: 'retelling', answer: 'New', created_at: '2026-10-17T02:00:00Z',
    learning: { retelling: { source_explanation_id: original.id } } };
  const context = { S: { history: { node: [retelling, original] } }, meta: node => node.metadata };
  const start = source.indexOf('  const byTime =');
  const end = source.indexOf('  const marksOf', start);
  vm.runInNewContext(source.slice(start, end) + '\nthis.currentPage = currentPage;', context);
  return { context, node: { id: 'node', metadata: {} }, original, retelling };
}

test('retelling is displayed by default without replacing the stored source', () => {
  const { context, node, original, retelling } = fixture();
  assert.equal(context.currentPage(node), retelling);
  assert.equal(context.S.history.node[1], original);
});

test('explicit page order and selected index win over newer chat replies', () => {
  const { context, node, original, retelling } = fixture();
  node.metadata.pages = [original.id, retelling.id];
  node.metadata.page = 0;
  context.S.history.node.unshift({ id: 'chat', answer: 'Chat reply', created_at: '2026-10-18T00:00:00Z' });
  assert.equal(context.currentPage(node), original);
  node.metadata.page = 1;
  assert.equal(context.currentPage(node), retelling);
  node.metadata.retelling_view = { explanation_id: retelling.id, original: true };
  assert.equal(context.currentPage(node), original);
});

test('page indices are clamped after history pruning', () => {
  const { context, node, original } = fixture();
  node.metadata.pages = ['missing', original.id];
  node.metadata.page = 99;
  assert.equal(context.currentPage(node), original);
  node.metadata.page = -5;
  assert.equal(context.currentPage(node), original);
});

test('saved view switches to the exact source explanation and back', () => {
  const { context, node, original, retelling } = fixture();
  node.metadata.retelling_view = { explanation_id: retelling.id, original: true };
  assert.equal(context.currentPage(node), original);
  node.metadata.retelling_view.original = false;
  assert.equal(context.currentPage(node), retelling);
});

test('a new retelling overrides the previous saved original view', () => {
  const { context, node, retelling } = fixture();
  node.metadata.retelling_view = { explanation_id: retelling.id, original: true };
  const next = { ...retelling, id: 'next', created_at: '2026-10-17T03:00:00Z',
    learning: { retelling: { source_explanation_id: retelling.id } } };
  context.S.history.node.unshift(next);
  assert.equal(context.currentPage(node), next);
  node.metadata.retelling_view = { explanation_id: next.id, original: true };
  assert.equal(context.currentPage(node), retelling);
});

test('a missing source or newer ordinary explanation falls back to latest', () => {
  const { context, node, retelling } = fixture();
  node.metadata.retelling_view = { explanation_id: retelling.id, original: true };
  context.S.history.node.pop();
  assert.equal(context.currentPage(node), retelling);
  const ordinary = { id: 'ordinary', answer: 'Regenerated', created_at: '2026-10-17T03:00:00Z' };
  context.S.history.node.unshift(ordinary);
  assert.equal(context.currentPage(node), ordinary);
});

test('empty history is safe and equal timestamps preserve server ordering', () => {
  const { context, node, original, retelling } = fixture();
  retelling.created_at = original.created_at;
  assert.equal(context.currentPage(node), retelling);
  context.S.history.node = [];
  assert.equal(context.currentPage(node), null);
});

test('repeated optimization switches to the first original, not the previous rewrite', () => {
  const { context, node, original, retelling } = fixture();
  const next = { ...retelling, id: 'next', created_at: '2026-10-17T03:00:00Z',
    learning: { retelling: { base_explanation_id: retelling.id, original_explanation_id: original.id,
      source_explanation_id: original.id } } };
  context.S.history.node.unshift(next);
  node.metadata.retelling_view = { explanation_id: next.id, original: true };
  assert.equal(context.currentPage(node), original);
});

test('the stored original snapshot remains available after history is pruned', () => {
  const { context, node, original, retelling } = fixture();
  retelling.learning.retelling.original_explanation_id = original.id;
  retelling.learning.retelling.original_explanation = original;
  context.S.history.node = [retelling];
  node.metadata.retelling_view = { explanation_id: retelling.id, original: true };
  assert.equal(context.currentPage(node), original);
});
