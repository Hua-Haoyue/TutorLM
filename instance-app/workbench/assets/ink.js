/* TutorLM v2 · Freehand ink over the reader (md blocks or PDF pages).
   One SVG layer per surface element. The caller decides how a stroke is anchored
   (md: block-relative in doc-width units; PDF: canonical page fractions) through
   `begin` / `frameOf`, so strokes survive zoom and re-render. Pointer input is
   only accepted while `tool()` returns a pen. */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  /* w: stroke width as a fraction of the surface width. */
  const PENS = {
    pen: { label: '钢笔', w: 0.0028, opacity: 1 },
    marker: { label: '马克笔', w: 0.0075, opacity: 0.9 },
    highlighter: { label: '荧光笔', w: 0.022, opacity: 0.32 },
    eraser: { label: '橡皮', w: 0 },
  };
  const COLORS = [['#1A1811', '墨黑'], ['#C2402F', '朱红'], ['#2F6FB5', '钴蓝'], ['#2F8A57', '松绿'], ['#E0A019', '琥珀'], ['#8A4FBF', '紫']];

  /* Quadratic smoothing through midpoints. */
  function pathD(pts) {
    if (!pts.length) return '';
    if (pts.length < 3) return `M${pts[0][0]} ${pts[0][1]}` + pts.slice(1).map((p) => ` L${p[0]} ${p[1]}`).join('') + (pts.length === 1 ? ` l0.01 0` : '');
    let d = `M${pts[0][0]} ${pts[0][1]}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2;
      d += ` Q${pts[i][0]} ${pts[i][1]} ${mx} ${my}`;
    }
    const last = pts[pts.length - 1];
    return d + ` L${last[0]} ${last[1]}`;
  }
  function segDist(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1], l = dx * dx + dy * dy;
    const t = l ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l)) : 0;
    return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
  }
  const r2 = (v) => Math.round(v * 100) / 100;

  /**
   * attach(el, opts) → { paint(), destroy() }
   * opts.strokes()          strokes to consider for this surface
   * opts.frameOf(stroke)    → toPx([a,b]) → [x,y] in el layout px, or null (not on this surface)
   * opts.begin(x, y)        → { anchor: {...}, toStore(x,y) → [a,b] } or null
   * opts.unit()             surface width in px (stroke widths scale with it)
   * opts.tool()             → { pen, color } | null
   * opts.onAdd(stroke), opts.onErase(stroke)
   */
  function attach(el, opts) {
    const svg = document.createElementNS(NS, 'svg');
    svg.classList.add('ink');
    svg.setAttribute('aria-hidden', 'true');
    el.appendChild(svg);
    let live = null;

    // Layout px of a pointer, independent of CSS zoom on an ancestor.
    function local(e) {
      const r = el.getBoundingClientRect();
      const sx = el.offsetWidth / (r.width || 1), sy = el.offsetHeight / (r.height || 1);
      return [(e.clientX - r.left) * sx, (e.clientY - r.top) * sy];
    }
    function size() { svg.setAttribute('viewBox', `0 0 ${el.offsetWidth} ${el.offsetHeight}`); }
    function strokeEl(s, toPx) {
      const pen = PENS[s.pen] || PENS.pen;
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', pathD(s.pts.map(toPx).map(([x, y]) => [r2(x), r2(y)])));
      path.setAttribute('stroke', s.color);
      path.setAttribute('stroke-width', String(Math.max(1, (s.w ?? pen.w) * opts.unit())));
      path.setAttribute('stroke-opacity', String(pen.opacity));
      if (s.pen === 'highlighter') path.classList.add('hi');
      return path;
    }
    function paint() {
      size();
      svg.textContent = '';
      for (const s of opts.strokes()) {
        const toPx = opts.frameOf(s);
        if (toPx) svg.appendChild(strokeEl(s, toPx));
      }
    }
    function eraseAt(p) {
      const tol = Math.max(6, 0.008 * opts.unit());
      for (const s of opts.strokes()) {
        const toPx = opts.frameOf(s);
        if (!toPx) continue;
        const px = s.pts.map(toPx), w = ((s.w ?? (PENS[s.pen] || PENS.pen).w) * opts.unit()) / 2;
        const hit = px.length === 1 ? Math.hypot(p[0] - px[0][0], p[1] - px[0][1]) < tol + w
          : px.some((q, i) => i && segDist(p, px[i - 1], q) < tol + w);
        if (hit) { opts.onErase(s); return true; }
      }
      return false;
    }
    function down(e) {
      const tool = opts.tool();
      if (!tool || e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();
      svg.setPointerCapture(e.pointerId);
      const p = local(e);
      if (tool.pen === 'eraser') { live = { erase: true }; eraseAt(p); return; }
      const frame = opts.begin(p[0], p[1]);
      if (!frame) return;
      const pen = PENS[tool.pen] || PENS.pen;
      live = { frame, raw: [p], stroke: { ...frame.anchor, pen: tool.pen, color: tool.color, w: pen.w, pts: [] } };
      live.path = strokeEl({ ...live.stroke, pts: [p] }, (q) => q);
      svg.appendChild(live.path);
    }
    function move(e) {
      if (!live) return;
      const p = local(e);
      if (live.erase) { eraseAt(p); return; }
      const last = live.raw[live.raw.length - 1];
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 1.5) return;
      live.raw.push(p);
      live.path.setAttribute('d', pathD(live.raw.map(([x, y]) => [r2(x), r2(y)])));
    }
    function up() {
      if (!live) return;
      const cur = live; live = null;
      if (cur.erase) return;
      cur.stroke.pts = cur.raw.map(([x, y]) => cur.frame.toStore(x, y));
      cur.path.remove();
      opts.onAdd(cur.stroke);
    }
    svg.addEventListener('pointerdown', down);
    svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerup', up);
    svg.addEventListener('pointercancel', up);
    paint();
    return { svg, paint, destroy() { svg.remove(); } };
  }

  window.TLMInk = { PENS, COLORS, attach, pathD };
})();
