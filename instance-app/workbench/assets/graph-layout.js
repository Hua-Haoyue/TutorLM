(function (root) {
  'use strict';

  function layout(nodes, edges, sizes = {}) {
    const graph = new root.dagre.graphlib.Graph();
    graph.setGraph({ rankdir: 'LR', ranker: 'network-simplex', nodesep: 56, edgesep: 32, ranksep: 150,
      marginx: 40, marginy: 40 });
    graph.setDefaultEdgeLabel(() => ({}));
    const ordered = [...nodes].sort((a, b) => (a.order_index || 0) - (b.order_index || 0)
      || String(a.id).localeCompare(String(b.id)));
    const successors = new Map(ordered.map(node => [node.id, new Set()]));
    for (const node of ordered) {
      const size = sizes[node.id] || {};
      graph.setNode(node.id, { width: Math.max(332, size.width || 0), height: Math.max(60, size.height || 150) });
    }
    // Roots form the left column and each level of descendants the next column to
    // the right. Hierarchy constraints come first; feedback links are still drawn
    // but cannot force a descendant left of its ancestor or create a ranking cycle.
    function connect(from, to, weight) {
      if (from === to || !successors.has(from) || !successors.has(to)) return;
      const pending = [to], seen = new Set();
      while (pending.length) {
        const next = pending.pop();
        if (next === from) return;
        if (seen.has(next)) continue;
        seen.add(next); pending.push(...successors.get(next));
      }
      successors.get(from).add(to);
      graph.setEdge(from, to, { weight: Math.max(weight, graph.edge(from, to)?.weight || 0) });
    }
    for (const node of ordered) connect(node.metadata?.source?.fromNode || node.parent_id, node.id, 8);
    const relations = [...edges].sort((a, b) => (a.relation === 'parent_of' ? 0 : 1) - (b.relation === 'parent_of' ? 0 : 1)
      || String(a.id || '').localeCompare(String(b.id || '')));
    for (const edge of relations) {
      if (['related_to', 'contradicts'].includes(edge.relation)) continue;
      const reverse = ['depends_on', 'derived_from', 'example_of'].includes(edge.relation);
      connect(reverse ? edge.target_node_id : edge.source_node_id,
        reverse ? edge.source_node_id : edge.target_node_id, edge.relation === 'parent_of' ? 8 : 1);
    }
    root.dagre.layout(graph);
    // dagre centres each card on its column; align the left edges instead so a
    // column reads as one straight line (the column's extent is unchanged).
    const left = new Map();
    for (const node of ordered) {
      const box = graph.node(node.id), column = box.rank ?? Math.round(box.x);
      left.set(column, Math.min(left.get(column) ?? Infinity, box.x - box.width / 2));
    }
    const positions = Object.fromEntries(ordered.map(node => {
      const box = graph.node(node.id);
      return [node.id, { x: Math.round(left.get(box.rank ?? Math.round(box.x))), y: Math.round(box.y - box.height / 2) }];
    }));
    const routes = [];
    for (const edge of graph.edges()) {
      const points = graph.edge(edge).points;
      if (points?.length) routes.push({ from: edge.v, to: edge.w, points: points.map(p => ({ x: Math.round(p.x), y: Math.round(p.y) })),
        source: positions[edge.v], target: positions[edge.w] });
    }
    Object.defineProperty(positions, 'routes', { value: routes });
    return positions;
  }

  root.TutorGraphLayout = { layout };
})(globalThis);
