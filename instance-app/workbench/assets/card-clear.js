/* TutorLM document-card clear and box-selection delete, sharing one undo stack. */
(function (global) {
  'use strict';
  function create({ api, state, layoutPlugin, refreshGraph, loadHistory, confirmModal, sync, toast, fail,
                    surface, nodesEl }) {
    const bar = document.querySelector('.canvas .pane-head.toolbar');
    const reset = document.getElementById('card-layout-reset');
    if (!bar || !reset || !layoutPlugin) return null;
    const button = document.createElement('button');
    button.id = 'card-clear-document';
    button.type = 'button';
    button.className = 'icon-btn card-clear-button';
    button.title = '清除当前文档的全部知识卡片';
    button.setAttribute('aria-label', button.title);
    button.innerHTML = '<svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="3" y="4" width="13" height="10" rx="1"/><path d="M6 7h7M6 10h4M15 14l3 3m0-3l-3 3"/></svg>';
    reset.after(button);
    let inProgress = false;
    let selecting = false;
    let drag = null;
    let selected = [];
    let selectedDocument = null;
    const selectButton = document.createElement('button');
    selectButton.id = 'card-select-delete';
    selectButton.type = 'button';
    selectButton.className = 'card-select-tool';
    selectButton.title = '点击进入框选，画完后立即删除；再次点击可取消';
    selectButton.setAttribute('aria-label', selectButton.title);
    selectButton.innerHTML = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2.5" y="3" width="12" height="11" rx="1" stroke="currentColor" stroke-width="1.6" stroke-dasharray="3 2"/><path d="m11 12 6 5m0-5-6 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span>框选删除</span>';
    button.after(selectButton);
    const marquee = document.createElement('div');
    marquee.className = 'card-select-marquee';
    marquee.hidden = true;
    surface.append(marquee);
    function paintSelection() {
      nodesEl.querySelectorAll('.gnode').forEach((el) =>
        el.classList.toggle('card-select-marked', selected.includes(el.dataset.id)));
      selectButton.classList.toggle('is-active', selecting);
      selectButton.setAttribute('aria-pressed', String(selecting));
      surface.classList.toggle('card-select-mode', selecting);
    }
    function cancelSelection() {
      selecting = false; drag = null; selected = []; selectedDocument = null;
      marquee.hidden = true; paintSelection();
    }
    function rectangle(a, b) {
      return { left: Math.min(a.x, b.x), top: Math.min(a.y, b.y),
        right: Math.max(a.x, b.x), bottom: Math.max(a.y, b.y) };
    }
    function point(event) {
      const bounds = surface.getBoundingClientRect();
      return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
    }
    function stop(event) { event.preventDefault(); event.stopImmediatePropagation(); }
    surface.addEventListener('pointerdown', (event) => {
      if (!selecting || event.button !== 0 || event.target.closest('.minimap, .inspector')) return;
      stop(event);
      selected = []; selectedDocument = state.doc?.id || null;
      drag = { id: event.pointerId, start: point(event), end: point(event) };
      marquee.hidden = false;
      surface.setPointerCapture(event.pointerId);
      paintSelection();
    }, true);
    surface.addEventListener('pointermove', (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      stop(event);
      drag.end = point(event);
      const r = rectangle(drag.start, drag.end);
      Object.assign(marquee.style, { left: `${r.left}px`, top: `${r.top}px`,
        width: `${r.right - r.left}px`, height: `${r.bottom - r.top}px` });
    }, true);
    function finish(event) {
      if (!drag || drag.id !== event.pointerId) return;
      stop(event);
      const r = rectangle(drag.start, point(event));
      drag = null; marquee.hidden = true;
      if (event.type === 'pointercancel' || r.right - r.left < 8 || r.bottom - r.top < 8) return;
      const s = surface.getBoundingClientRect();
      selected = Array.from(nodesEl.querySelectorAll('.gnode')).filter((el) => {
        if (!state.nodes.some((n) => n.id === el.dataset.id && n.document_id === selectedDocument)) return false;
        const b = el.getBoundingClientRect();
        return b.left >= s.left + r.left && b.right <= s.left + r.right &&
          b.top >= s.top + r.top && b.bottom <= s.top + r.bottom;
      }).map((el) => el.dataset.id);
      paintSelection();
      if (!selected.length) { toast('方框内没有完整的卡片，请扩大选区'); return; }
      selecting = false;
      paintSelection();
      deleteSelection().catch((error) => { cancelSelection(); fail(error); });
    }
    surface.addEventListener('pointerup', finish, true);
    surface.addEventListener('pointercancel', finish, true);
    selectButton.addEventListener('click', () => {
      if (selecting) cancelSelection();
      else { selecting = true; selected = []; selectedDocument = state.doc?.id || null; paintSelection(); }
    });
    document.addEventListener('keydown', (event) => {
      if (selecting && event.key === 'Escape') { cancelSelection(); stop(event); }
    }, true);
    function available() {
      return (api.runtime?.plugins || []).some((plugin) =>
        plugin.id === 'card-clear' && plugin.enabled !== false);
    }
    function refreshButton() {
      button.hidden = selectButton.hidden = !available();
      button.disabled = !available() || inProgress || !state.doc ||
        !state.nodes.some((n) => n.document_id === state.doc?.id);
      selectButton.disabled = button.disabled;
      if (selecting && selectedDocument !== state.doc?.id) cancelSelection();
    }
    async function restore(token) {
      await sync(api.post(`/api/plugins/card-clear/clear/${api.part(token)}/undo`, {}));
      try { await refreshGraph(); await loadHistory(); }
      catch (error) { location.reload(); throw error; }
    }
    async function clear() {
      if (inProgress || !state.doc || !state.project) return;
      const documentId = state.doc.id;
      const count = state.nodes.filter((n) => n.document_id === documentId).length;
      if (!count) { toast('当前文档没有知识卡片'); return; }
      if (Object.values(state.runs).some((r) => !api.TERMINAL.has(r.state))) {
        toast('有解释任务仍在生成，请完成后再清除'); return;
      }
      const ok = await confirmModal('清除当前文档卡片',
        `将清除当前文档的 ${count} 张知识卡片及其解释、连线。可通过“撤销”恢复。`, '清除');
      if (!ok || state.doc?.id !== documentId) return;
      cancelSelection();
      inProgress = true; refreshButton();
      let release;
      try {
        release = await layoutPlugin.beginExternal();
        const result = await sync(api.post(`/api/plugins/card-clear/documents/${api.part(documentId)}/clear`, {}));
        if (!result.undoToken) { toast('当前文档没有知识卡片'); return; }
        const token = result.undoToken;
        layoutPlugin.pushExternalUndo({ token, type: 'clear', undo: () => restore(token) });
        await refreshGraph(); await loadHistory();
        toast(`已清除 ${result.cleared} 张知识卡片`, {
          ms: 5000,
          placement: 'right',
          action: { label: '撤销', run: () => layoutPlugin.undoExternal(token).catch(fail) },
        });
      } finally {
        release?.(); inProgress = false; refreshButton();
      }
    }
    async function deleteCards(ids, documentId) {
      if (inProgress || !available() || !ids.length || !state.doc || state.doc.id !== documentId) return;
      if (Object.values(state.runs).some((r) => !api.TERMINAL.has(r.state))) {
        toast('有解释任务仍在生成，请完成后再删除'); cancelSelection(); return;
      }
      inProgress = true; refreshButton();
      let release;
      try {
        release = await layoutPlugin.beginExternal();
        const result = await sync(api.post(
          `/api/plugins/card-clear/documents/${api.part(documentId)}/delete-selection`, { cardIds: ids }));
        const token = result.undoToken;
        if (!token) return;
        layoutPlugin.pushExternalUndo({ token, undo: () => restore(token) });
        cancelSelection();
        await refreshGraph(); await loadHistory();
        toast(`已删除 ${result.cleared} 张卡片`, { ms: 5000, placement: 'right',
          action: { label: '撤销', run: () => layoutPlugin.undoExternal(token).catch(fail) } });
      } finally { release?.(); inProgress = false; refreshButton(); }
    }
    function deleteSelection() {
      if (!selected.length) return;
      return deleteCards([...selected], selectedDocument);
    }
    button.addEventListener('click', () => clear().catch(fail));
    new MutationObserver(refreshButton).observe(document.getElementById('graph-count'),
      { childList: true, characterData: true, subtree: true });
    refreshButton();
    new MutationObserver(() => { if (selected.length) paintSelection(); })
      .observe(nodesEl, { childList: true, subtree: true });
    return { available, refreshButton, cancelSelection, deleteCards };
  }
  global.TLMCardClear = { create };
})(window);
