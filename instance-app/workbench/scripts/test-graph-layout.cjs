const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = { structuredClone };
for (const file of ['dagre/dagre.min.js', 'graph-layout.js']) {
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets', file), 'utf8'), context);
}
const layout = context.TutorGraphLayout.layout;
const node = (id, parent_id, order_index = 0) => ({ id, parent_id, order_index });
const edge = (id, source_node_id, target_node_id, relation = 'supports') => ({ id, source_node_id, target_node_id, relation });

function nonoverlapping(nodes, positions, sizes = {}) {
  for (const a of nodes) for (const b of nodes) {
    if (a.id >= b.id) continue;
    const p = positions[a.id], q = positions[b.id];
    const A = { width: Math.max(332, sizes[a.id]?.width || 0), height: Math.max(60, sizes[a.id]?.height || 150) };
    const B = { width: Math.max(332, sizes[b.id]?.width || 0), height: Math.max(60, sizes[b.id]?.height || 150) };
    assert(p.x + A.width <= q.x || q.x + B.width <= p.x || p.y + A.height <= q.y || q.y + B.height <= p.y,
      `Overlapping nodes: ${a.id}, ${b.id}`);
  }
}

test('roots are above leaves and variable-size cards never overlap', () => {
  const nodes = [node('root'), node('left', 'root', 1), node('right', 'root', 2),
    node('leaf', 'left', 3), node('other-root', null, 4)];
  const sizes = { root: { width: 332, height: 500 }, left: { width: 450, height: 280 }, right: { height: 600 } };
  const p = layout(nodes, [], sizes);
  nonoverlapping(nodes, p, sizes);
  assert(p.root.y + 500 < p.left.y);
  assert(p.root.y + 500 < p.right.y);
  assert(p.left.y + 280 < p.leaf.y);
});

test('same-rank endpoints reorder to uncross independent connections', () => {
  const nodes = ['a', 'b', 'c', 'd'].map((id, i) => node(id, null, i));
  const p = layout(nodes, [edge('1', 'a', 'd'), edge('2', 'b', 'c')]);
  assert(p.a.y < p.d.y && p.b.y < p.c.y);
  assert((p.a.x - p.b.x) * (p.d.x - p.c.x) >= 0, 'Independent edges must not cross');
  nonoverlapping(nodes, p);
});

test('dependencies order prerequisites first, symmetric edges do not invent ancestry', () => {
  const nodes = [node('root'), node('child'), node('peer')];
  const p = layout(nodes, [edge('dep', 'child', 'root', 'depends_on'), edge('sym', 'child', 'peer', 'related_to')]);
  assert(p.root.y < p.child.y);
  const independent = layout(nodes, [edge('sym', 'child', 'peer', 'contradicts')]);
  assert.equal(independent.child.y, independent.peer.y);
});

test('feedback edges and malformed hierarchy cycles do not break the layout', () => {
  const nodes = [node('a'), node('b', 'a'), node('c', 'b')];
  const p = layout(nodes, [edge('cycle', 'c', 'a')]);
  assert(p.a.y < p.b.y && p.b.y < p.c.y);
  nonoverlapping(nodes, p);
  const cyclic = [node('x', 'y'), node('y', 'x')];
  nonoverlapping(cyclic, layout(cyclic, []));
});

test('fromNode ancestry and long chains are not flattened at depth six', () => {
  const nodes = Array.from({ length: 12 }, (_, i) => ({ ...node('n' + i, null, i),
    metadata: i ? { source: { fromNode: 'n' + (i - 1) } } : {} }));
  const p = layout(nodes, []);
  for (let i = 1; i < nodes.length; i++) assert(p['n' + (i - 1)].y < p['n' + i].y);
  nonoverlapping(nodes, p);
});

test('layout is deterministic, ignores cross-document edges and leaves inputs intact', () => {
  const nodes = [node('r'), node('a', 'r', 1), node('b', 'r', 2)];
  const edges = [edge('bad', 'foreign', 'r'), edge('self', 'a', 'a')];
  const before = JSON.stringify({ nodes, edges });
  const a = JSON.stringify(layout(nodes, edges));
  assert.equal(JSON.stringify(layout([...nodes].reverse(), edges)), a);
  assert.equal(JSON.stringify({ nodes, edges }), before);
});

test('empty graphs and the 80-node preview limit are supported', () => {
  assert.equal(Object.keys(layout([], [])).length, 0);
  const nodes = Array.from({ length: 80 }, (_, i) => node('n' + i, i ? 'n' + Math.floor((i - 1) / 2) : null, i));
  nonoverlapping(nodes, layout(nodes, []));
});
