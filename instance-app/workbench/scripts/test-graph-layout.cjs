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

test('prerequisites go right of what needs them, symmetric edges do not invent ancestry', () => {
  const nodes = [node('root'), node('child'), node('peer')];
  const p = layout(nodes, [edge('dep', 'child', 'root', 'depends_on'), edge('sym', 'child', 'peer', 'related_to')]);
  assert(p.child.x < p.root.x, 'child needs root: root is its prerequisite, so it sits right');
  const q = layout(nodes, [edge('pre', 'root', 'child', 'prerequisite_of')]);
  assert(q.child.x < q.root.x, 'A prerequisite_of B puts A right of B');
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
  assert(p.routes.length >= 1);
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

const circled = (id, start, extra = {}) => ({ id, parent_id: null, order_index: 0,
  metadata: { source: { quote: id, selector: { type: 'text', start, end: start + 5 } }, ...extra } });

test('circled concepts stay in the leftmost column, unless they are a prerequisite of another', () => {
  const nodes = [circled('a', 10), circled('b', 50), circled('c', 90), node('child', 'a', 1)];
  const p = layout(nodes, [edge('pre', 'c', 'b', 'prerequisite_of')]);
  assert.equal(p.a.x, p.b.x, 'circled concepts share the document-side column');
  assert(p.a.x < p.child.x);
  assert(p.b.x < p.c.x, 'c is a prerequisite of b, so it moves right of b');
  nonoverlapping(nodes, p);
});

test('the leftmost column follows the order of the circled text', () => {
  const starts = [400, 20, 300, 120, 220];
  const nodes = starts.map((start, i) => circled('n' + i, start));
  // Children pull in the opposite direction; the document order still wins in column 0.
  nodes.push(node('k0', 'n1', 9), node('k1', 'n0', 1));
  const p = layout([...nodes].reverse(), []);
  const byY = nodes.filter(n => n.id.startsWith('n')).sort((a, b) => p[a.id].y - p[b.id].y).map(n => n.metadata.source.selector.start);
  assert.deepEqual(byY, [...starts].sort((a, b) => a - b));
  nonoverlapping(nodes, p);
});

test('pdf selections are ordered by page, then by height on the page', () => {
  const pdf = (id, page, y) => ({ id, parent_id: null, metadata: { source: { selector: { type: 'pdf', pages: [{ page, rects: [{ x: 0.1, y, w: 0.2, h: 0.02 }] }] } } } });
  const nodes = [pdf('late', 2, 0.1), pdf('low', 1, 0.8), pdf('high', 1, 0.2)];
  const p = layout(nodes, []);
  assert(p.high.y < p.low.y && p.low.y < p.late.y);
});

test('a parent with several children is centred on them', () => {
  const nodes = [node('r'), node('a', 'r', 1), node('b', 'r', 2), node('c', 'r', 3)];
  const p = layout(nodes, []);
  assert(Math.abs((p.r.y + 75) - (p.b.y + 75)) <= 2);
});
