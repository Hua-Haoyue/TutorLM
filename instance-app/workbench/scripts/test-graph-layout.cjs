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

test('roots are left of leaves and variable-size cards never overlap', () => {
  const nodes = [node('root'), node('left', 'root', 1), node('right', 'root', 2),
    node('leaf', 'left', 3), node('other-root', null, 4)];
  const sizes = { root: { width: 332, height: 500 }, left: { width: 450, height: 280 }, right: { height: 600 } };
  const p = layout(nodes, [], sizes);
  nonoverlapping(nodes, p, sizes);
  assert(p.root.x + 332 < p.left.x);
  assert(p.root.x + 332 < p.right.x);
  assert(p.left.x + 450 < p.leaf.x);
  assert.equal(p.left.x, p.right.x, 'siblings share a column');
});

test('same-rank endpoints reorder to uncross independent connections', () => {
  const nodes = ['a', 'b', 'c', 'd'].map((id, i) => node(id, null, i));
  const p = layout(nodes, [edge('1', 'a', 'd'), edge('2', 'b', 'c')]);
  assert(p.a.x < p.d.x && p.b.x < p.c.x);
  assert((p.a.y - p.b.y) * (p.d.y - p.c.y) >= 0, 'Independent edges must not cross');
  nonoverlapping(nodes, p);
});

test('dependencies order prerequisites first, symmetric edges do not invent ancestry', () => {
  const nodes = [node('root'), node('child'), node('peer')];
  const p = layout(nodes, [edge('dep', 'child', 'root', 'depends_on'), edge('sym', 'child', 'peer', 'related_to')]);
  assert(p.root.x < p.child.x);
  const independent = layout(nodes, [edge('sym', 'child', 'peer', 'contradicts')]);
  assert.equal(independent.child.x, independent.peer.x);
});

test('feedback edges and malformed hierarchy cycles do not break the layout', () => {
  const nodes = [node('a'), node('b', 'a'), node('c', 'b')];
  const p = layout(nodes, [edge('cycle', 'c', 'a')]);
  assert(p.a.x < p.b.x && p.b.x < p.c.x);
  nonoverlapping(nodes, p);
  const cyclic = [node('x', 'y'), node('y', 'x')];
  nonoverlapping(cyclic, layout(cyclic, []));
});

test('fromNode ancestry and long chains are not flattened at depth six', () => {
  const nodes = Array.from({ length: 12 }, (_, i) => ({ ...node('n' + i, null, i),
    metadata: i ? { source: { fromNode: 'n' + (i - 1) } } : {} }));
  const p = layout(nodes, []);
  for (let i = 1; i < nodes.length; i++) assert(p['n' + (i - 1)].x < p['n' + i].x);
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

test('layout exposes engine routes instead of discarding long-edge crossing reduction', () => {
  const nodes = [node('root'), node('a','root',1), node('b','root',2), node('leaf','a',3)];
  const p = layout(nodes, [edge('long','root','leaf')]);
  assert(p.routes.length >= 4);
  const route = p.routes.find(route => route.from === 'root' && route.to === 'leaf');
  assert(route.points.length >= 3);
  assert(route.points.every(point => Number.isFinite(point.x) && Number.isFinite(point.y)));
  assert.equal(JSON.stringify(p).includes('routes'), false, 'Position persistence remains backward compatible');
  for (const routed of p.routes) for (let i = 1; i < routed.points.length; i++) {
    const a = routed.points[i - 1], b = routed.points[i];
    for (const other of nodes.filter(n => n.id !== routed.from && n.id !== routed.to)) {
      const rect = p[other.id];
      for (let step = 0; step <= 100; step++) {
        const x = a.x + (b.x - a.x) * step / 100, y = a.y + (b.y - a.y) * step / 100;
        assert(!(x > rect.x && x < rect.x + 332 && y > rect.y && y < rect.y + 150),
          `Routed edge must avoid unrelated card ${other.id}`);
      }
    }
  }
});
