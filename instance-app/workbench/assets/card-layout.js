/* TutorLM card layout controls, wired by runtime-workspace.js. */
(function (global) {
  'use strict';
  const LARGE = 1.16;
  const LIMIT = 20;
  const MERGE_MS = 500;
  const samePos = (a, b) => a && b && Number(a.x) === Number(b.x) && Number(a.y) === Number(b.y);
  const same = (a, b) => samePos(a.position, b.position) && a.scale === b.scale;
  const copy = (s) => ({ id: s.id, position: { x: s.position.x, y: s.position.y }, scale: s.scale });

  function create({ api, state, nodeOf, posOf, elOf, redraw, ensureVisible, world, surface, nodesEl, toast }) {
    const defaults = new Map();
    const history = [];
    let projectId = null;
    let pending = Promise.resolve();
    let busy = false;
    let decorating = false;
    const meta = (n) => n?.metadata || {};
    const scaleOf = (n) => meta(n).layout_scale === LARGE ? LARGE : 1;
    const current = (n, position) => ({
      id: n.id,
      position: { ...(position || posOf(n)) },
      scale: scaleOf(n),
    });
    const snapshot = (nodes) => nodes.map((n) => current(n));
    const changed = (before, after) => before.some((b, i) => !same(b, after[i]));
    const path = (id) => `/api/nodes/${api.part(id)}`;

    function refreshUndo() {
      const button = document.getElementById('card-layout-undo');
      if (button) button.disabled = busy || !history.length;
      const resetButton = document.getElementById('card-layout-reset');
      if (resetButton) resetButton.disabled = busy;
      const autoButton = document.querySelector('.canvas [data-act="auto-layout"]');
      if (autoButton) autoButton.disabled = busy;
    }
    function onRelayout() {
      if (state.project?.id !== projectId) {
        projectId = state.project?.id || null;
        defaults.clear(); history.length = 0; refreshUndo();
      }
      // Legacy nodes have no creation snapshot. Preserve their first observed position.
      state.nodes.forEach((n) => {
        if (!defaults.has(n.id)) defaults.set(n.id, { ...posOf(n), scale: 1 });
      });
      decorate();
    }
    function defaultOf(n, position) {
      const saved = meta(n).layout_default;
      if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) return saved;
      return defaults.get(n.id) || { ...(position || posOf(n)), scale: 1 };
    }
    function ensureDefault(n, position) {
      if (meta(n).layout_default) return;
      const d = defaultOf(n, position);
      n.metadata = { ...meta(n), layout_default: { x: d.x, y: d.y, scale: 1 } };
    }
    function record(type, before, after) {
      if (!before.length || !changed(before, after)) return null;
      const now = Date.now();
      const previous = history[history.length - 1];
      if (type === 'move' && previous?.type === 'move' && previous.projectId === projectId && now - previous.timestamp < MERGE_MS) {
        const first = new Map(previous.before.map((s) => [s.id, s]));
        before.forEach((s) => { if (!first.has(s.id)) first.set(s.id, copy(s)); });
        const last = new Map(previous.after.map((s) => [s.id, s]));
        after.forEach((s) => last.set(s.id, copy(s)));
        previous.before = [...first.values()]; previous.after = [...last.values()]; previous.timestamp = now;
      } else {
        history.push({ type, projectId, timestamp: now, before: before.map(copy), after: after.map(copy) });
        if (history.length > LIMIT) history.shift();
      }
      refreshUndo();
      return history[history.length - 1];
    }
    function renderCard(n) {
      const el = elOf(n);
      if (!el) return;
      const value = scaleOf(n) === LARGE ? 'large' : 'normal';
      if (el.dataset.cardScale !== value) el.dataset.cardScale = value;
      const button = el.querySelector('[data-card-layout="zoom"]');
      if (button) {
        const label = value === 'large' ? '恢复正常大小' : '放大卡片';
        button.title = label; button.setAttribute('aria-label', label);
        const symbol = value === 'large' ? '↙' : '⤢';
        if (button.textContent !== symbol) button.textContent = symbol;
      }
    }
    function decorate() {
      if (decorating) return;
      decorating = true;
      try {
        nodesEl.querySelectorAll('.gnode.sel').forEach((el) => {
          const n = nodeOf(el.dataset.id), head = el.querySelector('.gnode__head');
          if (!n || !head) return;
          if (!head.querySelector('[data-card-layout="zoom"]')) {
            const button = document.createElement('button');
            button.type = 'button'; button.className = 'icon-btn'; button.dataset.cardLayout = 'zoom';
            head.insertBefore(button, head.querySelector('[data-act="node-delete"]'));
          }
          renderCard(n);
        });
      } finally { decorating = false; }
    }
    function frameRedraw(duration = 330) {
      const start = performance.now();
      (function tick() {
        redraw();
        if (performance.now() - start < duration) requestAnimationFrame(tick);
      })();
    }
    function animate(states) {
      world.classList.add('animate');
      states.forEach((s) => {
        const n = nodeOf(s.id);
        if (!n) return;
        n.metadata = { ...meta(n), position: { ...s.position }, layout_scale: s.scale };
        const el = elOf(n);
        if (el) { el.style.left = s.position.x + 'px'; el.style.top = s.position.y + 'px'; }
        renderCard(n);
      });
      frameRedraw(420);
      setTimeout(() => { world.classList.remove('animate'); redraw(); }, 380);
    }
    async function saveOne(n, next, baseMeta, revision = n.revision) {
      const metadata = { ...baseMeta, position: { ...next.position }, layout_scale: next.scale };
      const saved = await api.patch(path(n.id), { metadata, revision });
      const actual = nodeOf(n.id);
      if (actual) Object.assign(actual, saved);
      return saved;
    }
    async function serial(task) {
      const job = pending.then(task);
      pending = job.catch(() => {});
      return job;
    }
    async function commitMove(n, from) {
      const before = current(n, from);
      const after = current(n);
      if (same(before, after)) return;
      ensureDefault(n, from);
      return serial(async () => {
        const live = nodeOf(n.id);
        if (!live) return;
        const metadata = { ...meta(live), position: { ...after.position }, layout_scale: after.scale };
        const saved = await api.patch(path(n.id), { metadata, revision: live.revision });
        // A later drag may already have changed the in-memory position.
        if (samePos(posOf(live), after.position)) Object.assign(live, saved);
        else live.revision = saved.revision;
        record('move', [before], [after]);
      });
    }
    async function beforeAutoLayout(nodes) {
      if (busy) throw new Error('卡片布局正在保存，请稍后重试');
      busy = true; refreshUndo();
      await pending;
      const before = snapshot(nodes);
      nodes.forEach((n, i) => ensureDefault(n, before[i].position));
      return before;
    }
    function afterAutoLayout(before, nodes) { record('move', before, snapshot(nodes)); }
    function endAutoLayout() { busy = false; refreshUndo(); }
    async function toggleZoom(n) {
      if (busy) return;
      busy = true; refreshUndo();
      try {
        await pending;
        const before = current(n);
        ensureDefault(n, before.position);
        const after = { ...before, scale: before.scale === 1 ? LARGE : 1 };
        // Resize on screen first (animated from the old box), then save; a failed
        // save puts the card back.
        const baseMeta = meta(n), old = elOf(n)?.getBoundingClientRect();
        n.metadata = { ...baseMeta, layout_scale: after.scale };
        renderCard(n);
        window.TLMFlipCard?.(elOf(n), old);
        frameRedraw(320);
        try { await saveOne(n, after, baseMeta); }
        catch (error) {
          const back = elOf(n)?.getBoundingClientRect();
          n.metadata = baseMeta; renderCard(n); window.TLMFlipCard?.(elOf(n), back);
          throw error;
        }
        renderCard(n);
        setTimeout(() => { if (state.selected === n.id) ensureVisible(n); }, 300);
        record('resize', [before], [after]);
        toast(after.scale === LARGE ? '已放大卡片' : '已恢复正常大小');
      } finally { busy = false; refreshUndo(); }
    }
    async function applyBatch(states, expected) {
      const graph = await api.get(`/api/projects/${api.part(projectId)}/graph`);
      const remote = new Map((graph.nodes || []).map((n) => [n.id, n]));
      for (const s of expected) {
        const actual = remote.get(s.id);
        if (!actual || !same(current(actual, meta(actual).position || s.position), s))
          throw new Error('卡片布局已被其他操作修改，请刷新后重试');
      }
      const applied = [];
      try {
        for (const s of states) {
          const live = nodeOf(s.id), original = remote.get(s.id);
          ensureDefault(live, s.position);
          await saveOne(live, s, { ...meta(original), layout_default: defaultOf(live, s.position) }, original.revision);
          applied.push(expected.find((x) => x.id === s.id));
        }
      } catch (error) {
        // Existing backend has no batch transaction. Restore already written nodes.
        for (const old of applied.reverse()) {
          const live = nodeOf(old.id);
          if (live) await saveOne(live, old, meta(live)).catch(() => {});
        }
        throw error;
      }
      animate(states);
    }
    async function reset(onlySelected) {
      if (busy || !projectId) return;
      busy = true; refreshUndo();
      try {
        await pending;
        const pool = state.nodes.filter((n) => n.document_id === state.doc?.id && (!onlySelected || n.id === state.selected));
        if (!pool.length) { toast('没有可恢复的卡片'); return; }
        const before = snapshot(pool);
        const after = pool.map((n) => {
          const d = defaultOf(n);
          return { id: n.id, position: { x: d.x, y: d.y }, scale: 1 };
        });
        if (!changed(before, after)) { toast('卡片已在默认位置和大小'); return; }
        await applyBatch(after, before);
        const action = record('reset', before, after);
        toast(onlySelected ? '已重置当前卡片布局' : '已重置布局', { ms: 5000,
          action: { label: '撤销', run: () => {
            if (history[history.length - 1] === action) undo().catch(report);
            else toast('请先撤销之后的卡片操作');
          } },
        });
      } finally { busy = false; refreshUndo(); }
    }
    async function beginExternal() {
      if (busy) throw new Error('卡片操作正在进行，请稍后再试');
      busy = true; refreshUndo();
      try { await pending; } catch (error) { busy = false; refreshUndo(); throw error; }
      return () => { busy = false; refreshUndo(); };
    }
    function pushExternalUndo({ token, undo: restore, type = 'delete' }) {
      history.push({ type, projectId, token, restore, timestamp: Date.now() });
      if (history.length > LIMIT) history.shift();
      refreshUndo();
    }
    async function undoExternal(token) {
      const last = history[history.length - 1];
      if (!last || last.token !== token) {
        toast('请先撤销之后的卡片操作'); return;
      }
      return undo();
    }
    async function undo() {
      if (busy || !history.length || !projectId) return;
      busy = true; refreshUndo();
      try {
        await pending;
        const action = history[history.length - 1];
        if (!action || action.projectId !== projectId) { history.length = 0; return; }
        if (action.restore) await action.restore();
        else await applyBatch(action.before, action.after);
        history.pop(); toast(action.restore ? '已恢复删除的卡片' : '已撤销卡片操作');
      } finally { busy = false; refreshUndo(); }
    }
    function report(error) { toast(error?.message || '卡片布局操作失败'); }

    const canvasBar = document.querySelector('.canvas .pane-head.toolbar');
    const layoutButton = canvasBar?.querySelector('[data-act="auto-layout"]');
    if (layoutButton) {
      const resetButton = document.createElement('button');
      resetButton.id = 'card-layout-reset'; resetButton.type = 'button'; resetButton.className = 'icon-btn';
      resetButton.title = '恢复默认布局（Shift+点击仅恢复当前卡片）';
      resetButton.setAttribute('aria-label', resetButton.title); resetButton.textContent = '⟲';
      layoutButton.before(resetButton);
      resetButton.addEventListener('click', (e) => reset(e.shiftKey).catch(report));
      const undoButton = document.createElement('button');
      undoButton.id = 'card-layout-undo'; undoButton.type = 'button'; undoButton.className = 'icon-btn';
      undoButton.title = '撤销最近一次卡片移动、放大、重置或删除（Ctrl+Z）';
      undoButton.setAttribute('aria-label', undoButton.title); undoButton.textContent = '↶';
      resetButton.before(undoButton); undoButton.addEventListener('click', () => undo().catch(report));
      refreshUndo();
    }
    nodesEl.addEventListener('click', (e) => {
      const button = e.target.closest('[data-card-layout="zoom"]');
      if (!button) return;
      e.preventDefault(); e.stopImmediatePropagation();
      const n = nodeOf(button.closest('.gnode')?.dataset.id);
      if (n) toggleZoom(n).catch(report);
    }, true);
    surface.addEventListener('pointerdown', (e) => {
      if (!busy || !e.target.closest('.gnode')) return;
      e.preventDefault(); e.stopImmediatePropagation();
    }, true);
    document.addEventListener('keydown', (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.key.toLowerCase() !== 'z' || !history.length) return;
      if (state.tool === 'draw' || state.modal || !state.connected ||
          surface.classList.contains('card-select-mode') ||
          e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
      e.preventDefault(); e.stopImmediatePropagation(); undo().catch(report);
    }, true);
    new MutationObserver(() => decorate()).observe(nodesEl, { childList: true, subtree: true });
    return { onRelayout, beforeAutoLayout, afterAutoLayout, endAutoLayout, commitMove, snapshot, record,
      undo, reset, beginExternal, pushExternalUndo, undoExternal };
  }
  global.TLMCardLayout = { create };
})(window);
