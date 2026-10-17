/* TutorLM workbench · in-place (IR) editing for the md/txt reader, Typora style.
   A click on a rendered block reveals that block's Markdown source in place; the
   rest of the document stays rendered. Leaving the block re-renders it. Text that
   is anchored to an explanation node is protected: any edit that would touch it
   is reverted and reported through `notify`.

   All offsets are UTF-16 indices into the document source (the same unit as the
   reader's data-start / data-end attributes). The pure helpers are exported for
   tests; the DOM part is created per reader with `create(options)`. */
(function (root) {
  'use strict';

  /* Smallest single change turning `before` into `after`: before[start, oldEnd) → after[start, newEnd). */
  function changedRange(before, after) {
    let start = 0;
    const max = Math.min(before.length, after.length);
    while (start < max && before.charCodeAt(start) === after.charCodeAt(start)) start++;
    let oldEnd = before.length, newEnd = after.length;
    while (oldEnd > start && newEnd > start && before.charCodeAt(oldEnd - 1) === after.charCodeAt(newEnd - 1)) { oldEnd--; newEnd--; }
    return { start, oldEnd, newEnd };
  }

  /* Does a change touch a protected range? Replacing or deleting any of its
     characters does; inserting strictly inside it does; inserting at either
     edge does not (the protected text itself is unchanged). */
  function touches(change, range) {
    if (change.oldEnd > change.start) return change.start < range.end && change.oldEnd > range.start;
    return change.start > range.start && change.start < range.end;
  }

  /* Ranges after an allowed change. Ranges never overlap the change, so they only move. */
  function shiftRanges(ranges, change) {
    const delta = (change.newEnd - change.start) - (change.oldEnd - change.start);
    return ranges.map((r) => (r.start >= change.oldEnd ? { ...r, start: r.start + delta, end: r.end + delta } : { ...r }));
  }

  /* Map a rendered caret (text before it inside the block) to a source offset:
     the end of the longest tail of that text found in the source, preferring
     the match nearest the proportional position. */
  function sourceCaret(source, renderedBefore, renderedTotal) {
    const before = String(renderedBefore || '').replace(/\s+/g, ' ');
    if (!before.trim()) return firstContent(source);
    const ratio = renderedTotal ? Math.min(1, renderedBefore.length / renderedTotal) : 1;
    for (let k = Math.min(24, before.length); k >= 1; k--) {
      const tail = before.slice(-k).trim();
      if (!tail) continue;
      const pattern = tail.split(' ').map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('(?:[\\s*_`~]|\\\\)+');
      const hits = [...source.matchAll(new RegExp(pattern, 'g'))];
      if (!hits.length) continue;
      const want = ratio * source.length;
      const best = hits.reduce((a, m) => (Math.abs(m.index + m[0].length - want) < Math.abs(a.index + a[0].length - want) ? m : a));
      return best.index + best[0].length;
    }
    return source.length;
  }
  /* After leading block markup (#, -, 1., >, $$, ```) so a click at the start lands on the text. */
  function firstContent(source) {
    const m = /^(?:\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*|\$\$|```[^\n]*\n))*/.exec(source);
    return m ? m[0].length : 0;
  }

  /**
   * create({ sheet, enabled, source, protectedIn, commit, notify, blockFont })
   *   enabled()                 → may a click start editing now (select tool, md/txt view, connected)
   *   source()                  → the full document source
   *   protectedIn(start, end, blockEl) → [{ start, end, id }] global ranges inside [start, end)
   *   commit({ start, end, text, ranges, cancelled }) → called once when the block is left
   *   notify(message)           → user-visible message (bottom toast)
   */
  function create(opts) {
    let active = null;

    function editableBlock(target) {
      const block = target?.closest?.('[data-b][data-start][data-end]');
      if (!block || !opts.sheet.contains(block) || block.closest('.ir-edit')) return null;
      return block;
    }
    /* Source span of a block, without the trailing newline(s) that belong to the separator. */
    function span(block) {
      const src = opts.source(), start = Number(block.dataset.start);
      let end = Number(block.dataset.end);
      if (!(end >= start)) return null;
      while (end > start && (src[end - 1] === '\n' || src[end - 1] === '\r')) end--;
      return { start, end, text: src.slice(start, end) };
    }

    function begin(block, clientX, clientY) {
      if (active) finish();
      const s = span(block);
      if (!s) return false;
      const cell = /^T[DH]$/.test(block.tagName);
      // Caret: rendered text before the click point → nearest source offset.
      let caret = firstContent(s.text);
      const pos = document.caretPositionFromPoint?.(clientX, clientY) || null;
      const range = pos ? (() => { const r = document.createRange(); r.setStart(pos.offsetNode, pos.offset); return r; })()
        : document.caretRangeFromPoint?.(clientX, clientY);
      if (range && block.contains(range.startContainer)) {
        const pre = document.createRange();
        pre.selectNodeContents(block); pre.setEnd(range.startContainer, range.startOffset);
        caret = sourceCaret(s.text, pre.toString(), block.textContent.length);
      }
      const cs = getComputedStyle(block);
      const ta = document.createElement('textarea');
      ta.className = 'ir-input';
      ta.spellcheck = false;
      ta.setAttribute('aria-label', '编辑这一段的 Markdown 源文本，离开后重新渲染');
      ta.value = s.text;
      for (const k of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight', 'letterSpacing', 'color', 'textAlign'])
        ta.style[k] = cs[k];
      if (block.tagName === 'PRE' || block.classList.contains('math-block')) ta.style.fontFamily = 'var(--mono)';
      const ranges = opts.protectedIn(s.start, s.end, block).map((r) => ({ ...r, start: r.start - s.start, end: r.end - s.start }));
      block.classList.add('ir-edit');
      block.replaceChildren(ta);
      active = { block, start: s.start, end: s.end, original: s.text, last: s.text, ranges, ta, cell };
      fit();
      ta.focus({ preventScroll: true });
      ta.setSelectionRange(caret, caret);
      ta.addEventListener('input', onInput);
      ta.addEventListener('keydown', onKey);
      ta.addEventListener('blur', () => { if (active?.ta === ta) finish(); });
      return true;
    }
    function fit() {
      const ta = active?.ta;
      if (!ta) return;
      ta.style.height = 'auto';
      ta.style.height = ta.scrollHeight + 'px';
    }
    function onInput() {
      const a = active;
      if (!a) return;
      let value = a.ta.value;
      if (a.cell) {
        // A table cell is one line of one cell: newlines become spaces, bare pipes are escaped.
        const fixed = value.replace(/\r?\n/g, ' ').replace(/(^|[^\\])\|/g, '$1\\|');
        if (fixed !== value) { const at = a.ta.selectionStart + (fixed.length - value.length); a.ta.value = value = fixed; a.ta.setSelectionRange(at, at); }
      }
      const change = changedRange(a.last, value);
      const hit = a.ranges.find((r) => touches(change, r));
      if (hit) {
        const caret = change.start;
        a.ta.value = a.last;
        a.ta.setSelectionRange(caret, caret);
        opts.notify('请先去除解释节点再进行修改');
        return;
      }
      a.ranges = shiftRanges(a.ranges, change);
      a.last = value;
      fit();
    }
    function onKey(e) {
      if (e.key === 'Escape' || ((e.ctrlKey || e.metaKey) && e.key === 'Enter')) { e.preventDefault(); e.stopPropagation(); active?.ta.blur(); }
    }
    /* Leave the block: hand the new source (and the moved protected ranges) back. */
    function finish() {
      const a = active;
      if (!a) return;
      active = null;
      a.ta.removeEventListener('input', onInput);
      opts.commit({ start: a.start, end: a.end, text: a.last, cancelled: a.last === a.original,
        ranges: a.ranges.map((r) => ({ ...r, start: r.start + a.start, end: r.end + a.start })) });
    }

    /* Moving from the edited block straight to another one: leave (re-render) first,
       then open the block now under the pointer, so one click is enough. */
    opts.sheet.addEventListener('mousedown', (e) => {
      if (!active || e.button !== 0 || active.block.contains(e.target) || !opts.enabled()) return;
      if (e.target.closest('mark.anchor, a, input, button') || !editableBlock(e.target)) return;
      e.preventDefault();
      // Re-rendering can move blocks (the edit box and the rendered block differ in
      // height, or the edit added text), so find the target again by its source
      // position and keep the pointer's position relative to it.
      const target = editableBlock(e.target), box = target.getBoundingClientRect();
      const rel = { x: e.clientX - box.left, y: e.clientY - box.top };
      const oldStart = Number(target.dataset.start), a = active;
      const delta = a.last.length - a.original.length, start = oldStart >= a.end ? oldStart + delta : oldStart;
      finish();
      const block = opts.sheet.querySelector(`[data-b][data-start="${start}"]`);
      if (!block) return;
      const next = block.getBoundingClientRect();
      begin(block, next.left + Math.min(rel.x, next.width - 1), next.top + Math.min(rel.y, next.height - 1));
    }, true);
    /* A plain click (no text selected) on a block starts editing it. */
    opts.sheet.addEventListener('click', (e) => {
      if (e.defaultPrevented || !opts.enabled() || !getSelection().isCollapsed) return;
      if (e.target.closest('mark.anchor, a, input, button, textarea, .ir-edit, [data-math-source]')) return;
      const block = editableBlock(e.target);
      if (block) begin(block, e.clientX, e.clientY);
    });

    return { begin, finish, get active() { return !!active; } };
  }

  const api = { changedRange, touches, shiftRanges, sourceCaret, firstContent, create };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TLMMarkdownIR = api;
})(typeof window === 'object' ? window : globalThis);
