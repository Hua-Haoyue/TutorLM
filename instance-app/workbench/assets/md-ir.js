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

    /* `at` ('start' | 'end') places the caret without a pointer (keyboard navigation). */
    function begin(block, clientX, clientY, at) {
      if (active) finish();
      const s = span(block);
      if (!s) return false;
      const cell = /^T[DH]$/.test(block.tagName);
      // Caret: rendered text before the click point → nearest source offset.
      let caret = at === 'end' ? s.text.length : firstContent(s.text);
      const pos = at ? null : document.caretPositionFromPoint?.(clientX, clientY) || null;
      const range = at ? null : pos ? (() => { const r = document.createRange(); r.setStart(pos.offsetNode, pos.offset); return r; })()
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
      active = { block, start: s.start, end: s.end, original: s.text, last: s.text, ranges, ta, cell,
        code: block.tagName === 'PRE' || block.classList.contains('math-block'), list: block.tagName === 'LI' };
      listen(ta);
      ta.setSelectionRange(caret, caret);
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
    /* Typora-style keys:
       Enter        a new paragraph (a new list item inside a list); a plain newline in code / math
       Shift+Enter  a newline inside the paragraph
       Enter in a table cell: the cell below; from the last row, a new paragraph after the table
       ↑ / ↓ on the first / last line: the previous / next block (the cell above / below in a
       table); ↓ past the last block opens a new paragraph at the end of the document. */
    function onKey(e) {
      const a = active;
      if (!a) return;
      if (e.key === 'Escape' || ((e.ctrlKey || e.metaKey) && e.key === 'Enter')) { e.preventDefault(); e.stopPropagation(); a.ta.blur(); return; }
      if (e.isComposing || e.altKey || e.ctrlKey || e.metaKey) return;
      const ta = a.ta, collapsed = ta.selectionStart === ta.selectionEnd;
      if (e.key === 'Enter' && !e.shiftKey) {
        if (a.cell) { e.preventDefault(); e.stopPropagation(); cellMove(1, true); return; }
        if (a.code) return;
        e.preventDefault(); e.stopPropagation();
        const before = ta.value.slice(0, ta.selectionStart), line = before.slice(before.lastIndexOf('\n') + 1);
        const item = a.list && /^(\s*)([-*+]|(\d+)([.)]))(\s+)(\[[ xX]\]\s+)?/.exec(line);
        const marker = item ? item[1] + (item[3] ? (Number(item[3]) + 1) + item[4] : item[2]) + item[5] + (item[6] ? '[ ] ' : '') : null;
        insert(marker ? '\n' + marker : '\n\n');
        return;
      }
      if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && collapsed && !e.shiftKey) {
        const up = e.key === 'ArrowUp', at = ta.selectionStart;
        if (up ? lineTop(ta, at) > lineTop(ta, 0) : lineTop(ta, at) < lineTop(ta, ta.value.length)) return;
        e.preventDefault(); e.stopPropagation();
        if (a.cell) cellMove(up ? -1 : 1, false); else go(up ? -1 : 1);
      }
    }
    function insert(text) {
      const ta = active.ta;
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
    /* Vertical position of a caret offset (to tell the first / last visual line). */
    function lineTop(ta, offset) {
      const cs = getComputedStyle(ta), mirror = document.createElement('div');
      for (const k of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight', 'letterSpacing', 'paddingLeft', 'paddingRight',
        'borderLeftWidth', 'borderRightWidth', 'boxSizing', 'textIndent', 'tabSize']) mirror.style[k] = cs[k];
      Object.assign(mirror.style, { position: 'absolute', visibility: 'hidden', whiteSpace: 'pre-wrap', overflowWrap: 'break-word',
        width: ta.clientWidth + 'px', left: '-9999px', top: '0' });
      mirror.textContent = ta.value.slice(0, offset);
      const mark = document.createElement('span'); mark.textContent = '​'; mirror.appendChild(mark);
      document.body.appendChild(mirror);
      const top = mark.offsetTop;
      mirror.remove();
      return top;
    }
    /* Where a block that started at `oldStart` starts after the active edit is committed. */
    function shifted(oldStart) {
      const a = active;
      return oldStart >= a.end ? oldStart + (a.last.length - a.original.length) : oldStart;
    }
    function reopen(start, at) {
      const block = opts.sheet.querySelector(`[data-b][data-start="${start}"]`);
      if (block) begin(block, 0, 0, at);
    }
    function blocks() { return [...opts.sheet.querySelectorAll('[data-b][data-start][data-end]')]; }
    /* Previous / next block in reading order; past the end, a new paragraph. */
    function go(dir, from = active.block) {
      const all = blocks(), i = all.indexOf(from), next = i < 0 ? null : all[i + dir];
      if (next) { const start = shifted(Number(next.dataset.start)); finish(); reopen(start, dir < 0 ? 'end' : 'start'); return; }
      if (dir > 0) appendAfter(from);
    }
    function cellMove(dir, enter) {
      const a = active, table = a.block.closest('table'), cell = a.block.closest('td, th');
      const rows = table ? [...table.rows] : [], r = rows.indexOf(cell?.parentElement);
      const target = rows[r + dir]?.cells[Math.min(cell.cellIndex, rows[r + dir].cells.length - 1)];
      if (target?.matches('[data-b][data-start]')) {
        const start = shifted(Number(target.dataset.start)); finish(); reopen(start, 'end'); return;
      }
      const cells = table ? [...table.querySelectorAll('[data-b][data-start]')] : [];
      if (dir > 0 && enter) appendAfter(cells.at(-1) || a.block);
      else go(dir, dir < 0 ? cells[0] || a.block : cells.at(-1) || a.block);
    }
    /* A new, empty paragraph right after `block` (after its table / list as a whole). */
    function appendAfter(block) {
      const src = opts.source(), lineEnd = src.indexOf('\n', Number(block.dataset.end) - 1);
      let at = /^T[DH]$/.test(block.tagName) ? (lineEnd < 0 ? src.length : lineEnd) : Number(block.dataset.end);
      while (at > 0 && (src[at - 1] === '\n' || src[at - 1] === '\r')) at--;
      const where = active ? shifted(at) : at, startOf = Number(block.dataset.start), first = active && shifted(startOf);
      if (active) finish();
      // Re-rendering replaced the DOM: find the block again, then its top-level element.
      let el = opts.sheet.querySelector(`[data-b][data-start="${first ?? startOf}"]`);
      if (!el) return;
      while (el.parentElement && el.parentElement !== opts.sheet && !el.parentElement.classList.contains('doc')) el = el.parentElement;
      const box = document.createElement(el.tagName === 'P' ? 'p' : 'div');
      box.className = 'ir-edit ir-insert';
      el.after(box);
      const ta = document.createElement('textarea');
      ta.className = 'ir-input';
      ta.spellcheck = false;
      ta.setAttribute('aria-label', '新段落的 Markdown 源文本，离开后重新渲染');
      box.appendChild(ta);
      active = { block: box, start: where, end: where, original: '', last: '', ranges: [], ta, cell: false, insert: true };
      listen(ta);
    }
    function listen(ta) {
      fit();
      ta.focus({ preventScroll: true });
      ta.addEventListener('input', onInput);
      ta.addEventListener('keydown', onKey);
      ta.addEventListener('blur', () => { if (active?.ta === ta) finish(); });
      ta.scrollIntoView({ block: 'nearest' });
    }
    /* Leave the block: hand the new source (and the moved protected ranges) back. */
    function finish() {
      const a = active;
      if (!a) return;
      active = null;
      a.ta.removeEventListener('input', onInput);
      if (a.insert) {
        const body = a.last.replace(/^\s+/, '').replace(/\s+$/, '');
        if (!body) a.block.remove();
        opts.commit({ start: a.start, end: a.end, text: body ? '\n\n' + body : '', cancelled: !body, ranges: [] });
        return;
      }
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
