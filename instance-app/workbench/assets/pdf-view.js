/* TutorLM v2 · PDF view (ES module, loaded on demand by runtime-workspace.js).
   The engine, canonical coordinate math, search and selection drafts come from
   instance-app/frontend (pdfjs-engine.js + pdf-reader.js), served same-origin at
   /static/reader/ with the locked PDF.js build at /static/pdfjs/. This module only
   mounts pages: canvas + PDF.js text layer + overlays, rendered lazily on scroll.

   Overlay rects are canonical (normalized, unrotated CropBox, origin top-left) and
   placed in % of the page box, so they survive zoom without recomputation. */
const READER_BASE = '/static/reader/';
const PDFJS_BASE = '/static/pdfjs/';
const MAX_RECTS_PER_PAGE = 64;

let libs = null;
function loadLibs() {
  libs ||= Promise.all([
    import(PDFJS_BASE + 'pdf.min.mjs'),
    import(READER_BASE + 'pdfjs-engine.js'),
    import(READER_BASE + 'pdf-reader.js'),
  ]).then(([pdfjsLib, bridge, adapter]) => ({
    pdfjsLib, adapter, engine: bridge.createPDFJSEngine({ pdfjsLib, base: PDFJS_BASE }),
  }));
  return libs.catch((error) => { libs = null; throw error; });
}

const clamp01 = (v) => Math.min(1, Math.max(0, Number.isFinite(v) ? v : 0));
const round = (v) => Math.round(v * 1e5) / 1e5;
const collapse = (s) => String(s || '').replace(/\s+/g, ' ').trim();

/* Merge per-glyph client rects into line boxes (same row, touching horizontally). */
function mergeRects(rects) {
  const sorted = [...rects].sort((a, b) => a.y - b.y || a.x - b.x);
  const out = [];
  for (const r of sorted) {
    const last = out[out.length - 1];
    if (last && Math.abs(last.y - r.y) < Math.min(last.h, r.h) * 0.5 && r.x <= last.x + last.w + 0.01) {
      const x2 = Math.max(last.x + last.w, r.x + r.w), y2 = Math.max(last.y + last.h, r.y + r.h);
      last.x = Math.min(last.x, r.x); last.y = Math.min(last.y, r.y); last.w = x2 - last.x; last.h = y2 - last.y;
    } else out.push({ ...r });
  }
  return out.slice(0, MAX_RECTS_PER_PAGE);
}

/**
 * Open a PDF into `container`.
 * opts: { data: ArrayBuffer (copied, PDF.js detaches it), ref: {document_id, revision_id},
 *         root: scroll element for lazy rendering, width: page CSS width,
 *         pixelScale: extra canvas resolution (reader zoom), onPage(view, n): page rendered }
 */
export async function openPdfView(container, { data, ref, root, width, pixelScale = 1, onPage = () => {} }) {
  const { pdfjsLib, adapter, engine } = await loadLibs();
  let doc = null;
  // Capture the engine document the adapter opens, so pages are rendered from the
  // same load instead of parsing the file twice.
  const captured = {
    getDocument(params) {
      const task = engine.getDocument(params);
      return { promise: task.promise.then((d) => (doc = d)), destroy: () => task.destroy() };
    },
  };
  const reader = adapter.createPdfReader({ engine: captured });
  const info = await reader.open(ref, { data: new Uint8Array(data.slice(0)) });
  const pages = [];
  for (let n = 1; n <= info.num_pages; n++) {
    const page = await doc.getPage(n);
    const base = page.getViewport({ scale: 1 });
    const scale = width / base.width;
    const el = document.createElement('section');
    el.className = 'pdfv-page';
    el.dataset.page = String(n);
    el.style.width = `${width}px`;
    el.style.height = `${Math.round(base.height * scale)}px`;
    el.style.setProperty('--total-scale-factor', String(scale));
    el.innerHTML = `<canvas aria-hidden="true"></canvas><div class="pdfv-marks"></div><div class="textLayer"></div>
      <div class="pdfv-no mono">${n} / ${info.num_pages}</div>`;
    container.appendChild(el);
    pages.push({ n, el, page, scale, quarters: (((Math.round((page.rotate || 0) / 90)) % 4) + 4) % 4, state: null, textLayer: null, items: null });
  }

  const textCache = new Map();
  function textContent(p) {
    if (!textCache.has(p.n)) textCache.set(p.n, p.page.getTextContent());
    return textCache.get(p.n);
  }
  let destroyed = false;
  async function render(p) {
    if (p.state) return p.state;
    p.state = (async () => {
      const viewport = p.page.getViewport({ scale: p.scale });
      const out = Math.min(window.devicePixelRatio || 1, 2) * pixelScale;
      const canvas = p.el.querySelector('canvas');
      canvas.width = Math.floor(viewport.width * out);
      canvas.height = Math.floor(viewport.height * out);
      const task = p.page.render({ canvasContext: canvas.getContext('2d'), viewport, transform: out !== 1 ? [out, 0, 0, out, 0, 0] : null });
      p.task = task;
      await task.promise;
      const content = await textContent(p);
      if (destroyed) return;
      // Same getTextContent() items the adapter searches (no marked content), so textDivs[i] ↔ items[i].
      const layer = new pdfjsLib.TextLayer({ textContentSource: content, container: p.el.querySelector('.textLayer'), viewport });
      await layer.render();
      p.textLayer = layer; p.items = content.items || [];
      onPage(view, p.n);
    })().catch((error) => {
      if (error?.name !== 'RenderingCancelledException' && !destroyed) console.error('PDF page render failed', p.n, error);
    });
    return p.state;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) render(pages[Number(e.target.dataset.page) - 1]); });
  }, { root, rootMargin: '600px 0px' });
  pages.forEach((p) => observer.observe(p.el));

  /* Canonical ⇄ page-fraction mapping through the adapter's rotation functions. */
  function toCanonicalRect(p, f) {
    const a = adapter.rotateScreenToCanonical({ x: f.x, y: f.y }, p.quarters);
    const b = adapter.rotateScreenToCanonical({ x: f.x + f.w, y: f.y + f.h }, p.quarters);
    const x = clamp01(Math.min(a.x, b.x)), y = clamp01(Math.min(a.y, b.y));
    return { x: round(x), y: round(y), w: round(clamp01(Math.max(a.x, b.x)) - x), h: round(clamp01(Math.max(a.y, b.y)) - y) };
  }
  function toPageRect(p, r) {
    const a = adapter.rotateCanonicalToScreen({ x: r.x, y: r.y }, p.quarters);
    const b = adapter.rotateCanonicalToScreen({ x: r.x + r.w, y: r.y + r.h }, p.quarters);
    return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) };
  }
  const pageOfPoint = (x, y) => pages.find((p) => {
    const r = p.el.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  });
  /* Client rects → canonical rects grouped by page. */
  function clientRectsToPages(list) {
    const byPage = new Map();
    for (const r of list) {
      if (r.width < 1 || r.height < 1) continue;
      const p = pageOfPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (!p) continue;
      const b = p.el.getBoundingClientRect();
      const f = { x: (r.left - b.left) / b.width, y: (r.top - b.top) / b.height, w: r.width / b.width, h: r.height / b.height };
      (byPage.get(p.n) || byPage.set(p.n, []).get(p.n)).push(toCanonicalRect(p, f));
    }
    return [...byPage.entries()].sort((a, b) => a[0] - b[0]).map(([page, rects]) => ({ page, rects: mergeRects(rects) }));
  }

  const view = {
    numPages: info.num_pages,
    pages: pages.map((p) => ({ n: p.n, el: p.el })),
    container,
    async cropSelection(selector) {
      if (selector?.pages?.length !== 1) throw new Error('请在同一页内选择公式');
      const anchor = selector.pages[0], page = pages[anchor.page - 1];
      if (!page || !anchor.rects?.length) throw new Error('PDF 选区不可用，请重新选择');
      const bounds = page.el.getBoundingClientRect(), rects = anchor.rects.map(rect => toPageRect(page, rect));
      const left = Math.min(...rects.map(rect => rect.x)), top = Math.min(...rects.map(rect => rect.y));
      const right = Math.max(...rects.map(rect => rect.x + rect.w)), bottom = Math.max(...rects.map(rect => rect.y + rect.h));
      return view.crop(anchor.page, { left: bounds.left + left * bounds.width - 2, top: bounds.top + top * bounds.height - 2,
        width: (right - left) * bounds.width + 4, height: (bottom - top) * bounds.height + 4 });
    },
    async crop(n, rect) {
      const p = pages[n - 1];
      if (!p) throw new Error('PDF page unavailable');
      await render(p);
      if (!p.textLayer) throw new Error('PDF page has not finished rendering');
      const bounds = p.el.getBoundingClientRect();
      const x = clamp01((rect.left - bounds.left) / bounds.width), y = clamp01((rect.top - bounds.top) / bounds.height);
      const f = { x, y, w: Math.max(0, clamp01((rect.left + rect.width - bounds.left) / bounds.width) - x),
        h: Math.max(0, clamp01((rect.top + rect.height - bounds.top) / bounds.height) - y) };
      if (!f.w || !f.h) throw new Error('PDF 选区为空，请重新选择');
      const source = p.el.querySelector('canvas'), canvas = document.createElement('canvas');
      const scale = Math.min(1, 2400 / Math.max(source.width * f.w, source.height * f.h));
      canvas.width = Math.max(1, Math.round(source.width * f.w * scale)); canvas.height = Math.max(1, Math.round(source.height * f.h * scale));
      canvas.getContext('2d').drawImage(source, f.x * source.width, f.y * source.height, f.w * source.width, f.h * source.height, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      return { blob, selector: { type: 'pdf', coordinate_space: 'normalized-cropbox-unrotated-top-left', pages: [{ page: n, rects: [toCanonicalRect(p, f)] }] } };
    },

    /* Current DOM selection → reference draft from the adapter (null when not in this PDF). */
    selection() {
      const sel = getSelection();
      if (!sel.rangeCount || sel.isCollapsed) return null;
      const range = sel.getRangeAt(0);
      if (!container.contains(range.commonAncestorContainer)) return null;
      const quote = collapse(sel.toString()).slice(0, 2000);
      if (quote.length < 1) return null;
      const anchors = clientRectsToPages(range.getClientRects());
      if (!anchors.length) return null;
      // Selectable text only exists on rendered pages, whose items are loaded: the
      // context comes from the same item join the adapter's getText() uses.
      let prefix = '', suffix = '';
      const items = pages[anchors[0].page - 1].items;
      if (items) {
        const text = collapse(items.map((it) => (typeof it.str === 'string' ? it.str : '')).join(' '));
        const startEl = (range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement).closest('.textLayer > span');
        const itemIndex = pages[anchors[0].page - 1].textLayer?.textDivs.indexOf(startEl);
        const preceding = itemIndex >= 0 ? collapse(items.slice(0, itemIndex).map(it => it.str || '').join(' ')) : '';
        const candidates = [...text.matchAll(new RegExp(quote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g'))];
        const matching = candidates.filter(m => m.index >= preceding.length && m.index <= preceding.length + (items[itemIndex]?.str?.length || 0) + 2);
        const at = matching.length === 1 ? matching[0].index : candidates.length === 1 ? candidates[0].index : -1;
        if (at >= 0) { prefix = text.slice(Math.max(0, at - 32), at); suffix = text.slice(at + quote.length, at + quote.length + 32); }
      }
      const draft = reader.selectPages({ pages: anchors, quote, prefix, suffix });
      return { draft, page: anchors[0].page, rect: range.getBoundingClientRect() };
    },

    /* marks: [{ cls, data: {key: value}, title, pages: [{page, rects}] }] — replaces all boxes. */
    setMarks(marks) {
      pages.forEach((p) => { p.el.querySelector('.pdfv-marks').innerHTML = ''; });
      for (const m of marks) {
        for (const a of m.pages || []) {
          const p = pages[a.page - 1];
          if (!p) continue;
          for (const r of a.rects || []) {
            const s = toPageRect(p, r), box = document.createElement('mark');
            box.className = m.cls;
            Object.entries(m.data || {}).forEach(([k, v]) => { box.dataset[k] = v; });
            if (m.title) box.title = m.title;
            box.style.cssText = `left:${s.x * 100}%;top:${s.y * 100}%;width:${s.w * 100}%;height:${s.h * 100}%`;
            p.el.querySelector('.pdfv-marks').appendChild(box);
          }
        }
      }
    },
    /* Marks sit under the text layer (so text stays selectable): hit-test by point. */
    markAt(x, y, selector) {
      const p = pageOfPoint(x, y);
      if (!p) return null;
      return [...p.el.querySelectorAll(`.pdfv-marks ${selector}`)].find((m) => {
        const r = m.getBoundingClientRect();
        return x >= r.left - 1 && x <= r.right + 1 && y >= r.top - 1 && y <= r.bottom + 1;
      }) || null;
    },

    /* Search via the adapter; each hit is {page, text_range, quote}. */
    search(q) { return reader.search(q, { maxPages: info.num_pages, maxResults: 500 }); },
    /* Canonical rects of a hit: maps the adapter's page-text offsets onto text-layer spans. */
    async hitRects(hit) {
      const p = pages[hit.page - 1];
      if (!p) return [];
      await render(p);
      if (!p.textLayer) return [];
      const strs = p.items.map((it) => (typeof it.str === 'string' ? it.str : ''));
      const joined = strs.join(' '), lead = joined.length - joined.trimStart().length;
      const locate = (offset) => {
        let at = 0;
        for (let i = 0; i < strs.length; i++) {
          if (offset <= at + strs[i].length) return [i, offset - at];
          at += strs[i].length + 1;
        }
        return [strs.length - 1, strs[strs.length - 1].length];
      };
      const [i1, o1] = locate(hit.text_range.start + lead), [i2, o2] = locate(hit.text_range.end + lead);
      const t1 = p.textLayer.textDivs[i1]?.firstChild, t2 = p.textLayer.textDivs[i2]?.firstChild;
      if (!t1 || !t2) return [];
      const range = document.createRange();
      range.setStart(t1, Math.min(o1, t1.length)); range.setEnd(t2, Math.min(o2, t2.length));
      return clientRectsToPages(range.getClientRects()).filter((a) => a.page === p.n).flatMap((a) => a.rects);
    },

    async outline() { try { return await reader.getOutline(); } catch { return []; } },
    goTo(page) {
      const p = pages[page - 1];
      if (!p) return;
      reader.goTo({ page }).catch?.(() => {});
      p.el.scrollIntoView({ block: 'start' });
    },
    currentPage() {
      const top = (root?.getBoundingClientRect().top || 0) + 40;
      let cur = 1;
      pages.forEach((p) => { if (p.el.getBoundingClientRect().top <= top) cur = p.n; });
      return cur;
    },
    /* Ink support: page-fraction ⇄ canonical point. */
    toCanonicalPoint(n, fx, fy) { return rotatePt(n, fx, fy, 'screen'); },
    toPagePoint(n, cx, cy) { return rotatePt(n, cx, cy, 'canonical'); },

    async destroy() {
      destroyed = true;
      observer.disconnect();
      pages.forEach((p) => { try { p.task?.cancel(); } catch { /* finished */ } });
      await reader.close();
    },
  };
  function rotatePt(n, x, y, from) {
    const p = pages[n - 1];
    const fn = from === 'screen' ? adapter.rotateScreenToCanonical : adapter.rotateCanonicalToScreen;
    const r = fn({ x, y }, p ? p.quarters : 0);
    return [round(r.x), round(r.y)];
  }
  return view;
}
