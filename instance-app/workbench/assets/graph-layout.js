(function (root) {
  'use strict';

  function layout(nodes, edges, sizes = {}) {
    const graph = new root.dagre.graphlib.Graph();
    graph.setGraph({ rankdir: 'TB', ranker: 'network-simplex', nodesep: 48, ranksep: 80,
      marginx: 40, marginy: 40 });
    graph.setDefaultEdgeLabel(() => ({}));
    const ordered = [...nodes].sort((a, b) => (a.order_index || 0) - (b.order_index || 0)
      || String(a.id).localeCompare(String(b.id)));
    const successors = new Map(ordered.map(node => [node.id, new Set()]));
    for (const node of ordered) {
      const size = sizes[node.id] || {};
      graph.setNode(node.id, { width: Math.max(332, size.width || 0), height: Math.max(60, size.height || 150) });
    }
    // Keep hierarchy constraints first; feedback links are still drawn but cannot
    // force a descendant above its ancestor or introduce a ranking cycle.
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
    return Object.fromEntries(ordered.map(node => {
      const box = graph.node(node.id);
      return [node.id, { x: Math.round(box.x - box.width / 2), y: Math.round(box.y - box.height / 2) }];
    }));
  }

  root.TutorGraphLayout = { layout };
})(globalThis);
