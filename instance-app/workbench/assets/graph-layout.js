(function (root) {
  'use strict';

  /* Layered left-to-right layout for the card canvas.
     - Columns: every card sits right of everything that leads to it (parent,
       fromNode source, explicit relations). Cards nothing leads to — the concepts
       circled in the document — stay in the leftmost column, next to the document.
       "A is a prerequisite of B" puts A to the RIGHT of B: the card read in the
       document stays nearest to it and its prerequisites unfold away from it.
     - Rows: the leftmost column follows the order of the circled text in the
       document (fewer crossings between the document and its cards); the other
       columns are ordered by their neighbours to reduce crossings.
     - Long links reserve one slot per column they pass and are returned as routes,
       so they run between cards instead of through them. */
  const MIN_W = 332, MIN_H = 60, DEF_H = 150, RANKSEP = 150, NODESEP = 56, EDGESEP = 24, DUMMY_H = 16, MARGIN = 40;

  function sourceKey(node) {
    const sel = node.metadata?.source?.selector || node.metadata?.visual_source?.selector;
    if (sel?.type === 'pdf') {
      const page = sel.pages?.[0], rect = page?.rects?.[0];
      if (page) return [Number(page.page) || 0, Number(rect?.y) || 0, Number(rect?.x) || 0];
    }
    if (sel && Number.isFinite(sel.start)) return [0, sel.start, 0];
    return null;
  }
  function compareSource(a, b) {
    const p = sourceKey(a), q = sourceKey(b);
    if (p && q) for (let i = 0; i < 3; i++) if (p[i] !== q[i]) return p[i] - q[i];
    if (p && !q) return -1;
    if (q && !p) return 1;
    return (a.order_index || 0) - (b.order_index || 0) || String(a.id).localeCompare(String(b.id));
  }

  function layout(nodes, edges, sizes = {}) {
    const ordered = [...nodes].sort(compareSource);
    const byId = new Map(ordered.map(n => [n.id, n]));
    const size = id => ({ width: Math.max(MIN_W, sizes[id]?.width || 0), height: Math.max(MIN_H, sizes[id]?.height || DEF_H) });
    const succ = new Map(ordered.map(n => [n.id, new Map()]));
    // Hierarchy first; a link that would close a cycle is still drawn but cannot rank.
    function connect(from, to, weight) {
      if (from === to || !byId.has(from) || !byId.has(to)) return;
      const pending = [to], seen = new Set();
      while (pending.length) {
        const next = pending.pop();
        if (next === from) return;
        if (seen.has(next)) continue;
        seen.add(next); pending.push(...succ.get(next).keys());
      }
      succ.get(from).set(to, Math.max(weight, succ.get(from).get(to) || 0));
    }
    for (const node of ordered) connect(node.metadata?.source?.fromNode || node.parent_id, node.id, 8);
    const relations = [...edges].sort((a, b) => (a.relation === 'parent_of' ? 0 : 1) - (b.relation === 'parent_of' ? 0 : 1)
      || String(a.id || '').localeCompare(String(b.id || '')));
    for (const edge of relations) {
      if (['related_to', 'contradicts'].includes(edge.relation)) continue;
      // Links point left → right; a prerequisite goes right of what needs it.
      const reverse = ['prerequisite_of', 'derived_from', 'example_of'].includes(edge.relation);
      connect(reverse ? edge.target_node_id : edge.source_node_id,
        reverse ? edge.source_node_id : edge.target_node_id, edge.relation === 'parent_of' ? 8 : 1);
    }

    // Columns: longest path from the cards nothing leads to (they stay in column 0).
    const indeg = new Map(ordered.map(n => [n.id, 0]));
    for (const m of succ.values()) for (const to of m.keys()) indeg.set(to, indeg.get(to) + 1);
    const rank = new Map(), queue = ordered.filter(n => !indeg.get(n.id)).map(n => n.id);
    queue.forEach(id => rank.set(id, 0));
    for (let i = 0; i < queue.length; i++) {
      for (const to of succ.get(queue[i]).keys()) {
        rank.set(to, Math.max(rank.get(to) || 0, rank.get(queue[i]) + 1));
        indeg.set(to, indeg.get(to) - 1);
        if (!indeg.get(to)) queue.push(to);
      }
    }

    // Layer graph: one dummy slot per column a long link passes.
    const V = new Map(), chains = [];
    for (const n of ordered) V.set(n.id, { id: n.id, rank: rank.get(n.id), ...size(n.id), up: [], down: [], node: n });
    const link = (a, b, w) => { V.get(a).down.push([b, w]); V.get(b).up.push([a, w]); };
    for (const [from, m] of succ) for (const [to, w] of m) {
      const chain = [from];
      for (let r = rank.get(from) + 1; r < rank.get(to); r++) {
        const id = `\u0000${from}\u0000${to}\u0000${r}`;
        V.set(id, { id, rank: r, width: 0, height: DUMMY_H, up: [], down: [], dummy: true }); chain.push(id);
      }
      chain.push(to);
      for (let i = 1; i < chain.length; i++) link(chain[i - 1], chain[i], w);
      chains.push({ from, to, chain });
    }
    const maxRank = Math.max(0, ...[...V.values()].map(v => v.rank));
    const layers = Array.from({ length: maxRank + 1 }, () => []);
    // Initial rows: column 0 in document order, then depth-first from it.
    const placed = new Set();
    const visit = id => {
      if (placed.has(id)) return;
      placed.add(id); layers[V.get(id).rank].push(id);
      for (const [to] of V.get(id).down) visit(to);
    };
    for (const n of ordered) if (rank.get(n.id) === 0) visit(n.id);
    for (const n of ordered) visit(n.id);
    const pos = new Map();
    const index = () => layers.forEach(l => l.forEach((id, i) => pos.set(id, i)));
    index();
    const tie = (a, b) => {
      const p = V.get(a), q = V.get(b);
      return p.node && q.node ? compareSource(p.node, q.node) : pos.get(a) - pos.get(b);
    };
    function sweep(r, dir) {
      const bary = new Map(layers[r].map(id => {
        const nb = V.get(id)[dir];
        if (!nb.length) return [id, pos.get(id)];
        let s = 0, t = 0;
        for (const [o, w] of nb) { s += pos.get(o) * w; t += w; }
        return [id, s / t];
      }));
      layers[r].sort((a, b) => bary.get(a) - bary.get(b) || tie(a, b));
      layers[r].forEach((id, i) => pos.set(id, i));
    }
    function crossings() {
      let c = 0;
      for (let r = 0; r < maxRank; r++) {
        const es = [];
        for (const id of layers[r]) for (const [o] of V.get(id).down) es.push([pos.get(id), pos.get(o)]);
        for (let i = 0; i < es.length; i++) for (let j = i + 1; j < es.length; j++) {
          if ((es[i][0] - es[j][0]) * (es[i][1] - es[j][1]) < 0) c++;
        }
      }
      return c;
    }
    // Column 0 keeps the document order; the other columns minimise crossings.
    let best = layers.map(l => [...l]), bestC = crossings();
    for (let it = 0; it < 8 && bestC; it++) {
      if (it % 2 === 0) for (let r = 1; r <= maxRank; r++) sweep(r, 'up');
      else for (let r = maxRank - 1; r >= 1; r--) sweep(r, 'down');
      const c = crossings();
      if (c < bestC) { bestC = c; best = layers.map(l => [...l]); }
    }
    best.forEach((l, r) => { layers[r] = l; });
    index();

    // x: left edges aligned per column, each column as wide as its widest card.
    const colX = [], colW = [];
    for (let r = 0, x = MARGIN; r <= maxRank; r++) {
      colX[r] = x; colW[r] = Math.max(MIN_W, ...layers[r].map(id => V.get(id).width));
      x += colW[r] + RANKSEP;
    }
    // y: stack each column, then pull every slot toward the centre of its neighbours
    // while keeping order and gaps (isotonic regression by pooling adjacent violators).
    const gap = (a, b) => (V.get(a).dummy || V.get(b).dummy ? EDGESEP : NODESEP);
    const top = new Map();
    layers.forEach(l => { let y = 0; l.forEach((id, i) => { if (i) y += gap(l[i - 1], id); top.set(id, y); y += V.get(id).height; }); });
    const center = id => top.get(id) + V.get(id).height / 2;
    function settle(r, dir) {
      const l = layers[r], offset = [], target = [];
      let o = 0;
      l.forEach((id, i) => {
        if (i) o += V.get(l[i - 1]).height + gap(l[i - 1], id);
        offset.push(o);
        const nb = V.get(id)[dir];
        let want = top.get(id);
        if (nb.length) {
          let s = 0, t = 0;
          for (const [other, w] of nb) { s += center(other) * w; t += w; }
          want = s / t - V.get(id).height / 2;
        }
        target.push(want - o);
      });
      const blocks = [];
      for (const value of target) {
        blocks.push({ sum: value, n: 1 });
        while (blocks.length > 1 && blocks.at(-2).sum / blocks.at(-2).n > blocks.at(-1).sum / blocks.at(-1).n) {
          const b = blocks.pop(); blocks.at(-1).sum += b.sum; blocks.at(-1).n += b.n;
        }
      }
      let i = 0;
      for (const b of blocks) for (let k = 0; k < b.n; k++, i++) top.set(l[i], b.sum / b.n + offset[i]);
    }
    for (let it = 0; it < 6; it++) {
      for (let r = 1; r <= maxRank; r++) settle(r, 'up');
      for (let r = maxRank - 1; r >= 0; r--) settle(r, 'down');
    }
    const minTop = Math.min(0, ...top.values());
    const Y = id => Math.round(top.get(id) - minTop + MARGIN);

    const positions = Object.fromEntries(ordered.map(n => [n.id, { x: colX[rank.get(n.id)], y: Y(n.id) }]));
    const routes = [];
    for (const { from, to, chain } of chains) {
      if (chain.length < 3) continue;
      const points = [{ x: positions[from].x + V.get(from).width, y: Y(from) + V.get(from).height / 2 }];
      for (const id of chain.slice(1, -1)) {
        const r = V.get(id).rank, y = Y(id) + DUMMY_H / 2;
        points.push({ x: colX[r], y }, { x: colX[r] + colW[r], y });
      }
      points.push({ x: positions[to].x, y: Y(to) + V.get(to).height / 2 });
      routes.push({ from, to, points: points.map(p => ({ x: Math.round(p.x), y: Math.round(p.y) })),
        source: positions[from], target: positions[to] });
    }
    Object.defineProperty(positions, 'routes', { value: routes });
    return positions;
  }

  root.TutorGraphLayout = { layout };
})(globalThis);
