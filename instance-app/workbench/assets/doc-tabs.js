/* TutorLM workbench · editor-style document tabs (between the reader toolbar and the page).
   Open documents are tabs: click to switch, × or middle-click to close, drag to reorder,
   "+" to open another document. The order is a view preference kept per project in
   localStorage; documents themselves stay on the server. */
(function () {
  'use strict';
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /**
   * create({ host, storageKey(), docs(), current(), title(doc), icon(doc), open(id), closed(nextId|null), add(anchorEl) })
   *   docs()      documents of the project (to drop tabs whose document is gone)
   *   current()   id of the open document, or null
   *   open(id)    show a document (the reader opens it)
   *   closed(id)  the active tab was closed: show `id` (neighbour) or nothing when null
   *   add(el)     the "+" button was pressed (anchor element for a picker)
   */
  function create(opts) {
    const { host } = opts;
    let ids = [], dragId = null;
    const load = () => {
      try { ids = (JSON.parse(localStorage.getItem(opts.storageKey())) || []).filter((x) => typeof x === 'string'); } catch { ids = []; }
    };
    const save = () => { try { localStorage.setItem(opts.storageKey(), JSON.stringify(ids)); } catch { /* storage full */ } };
    const known = () => new Set(opts.docs().map((d) => d.id));

    function render() {
      const docs = new Map(opts.docs().map((d) => [d.id, d]));
      ids = ids.filter((id) => docs.has(id));
      const cur = opts.current();
      host.innerHTML = `<div class="doc-tabs__list" role="tablist" aria-label="已打开的文档">${ids.map((id) => {
        const d = docs.get(id), on = id === cur;
        return `<div class="doc-tab${on ? ' on' : ''}" role="tab" aria-selected="${on}" tabindex="${on ? 0 : -1}" draggable="true" data-tab="${esc(id)}" title="${esc(d.title)}">
          ${opts.icon(d)}<span class="doc-tab__name">${esc(opts.title(d))}</span>
          <button type="button" class="doc-tab__close" data-tab-close="${esc(id)}" tabindex="-1" aria-label="关闭 ${esc(opts.title(d))}" title="关闭">×</button></div>`;
      }).join('')}</div><button type="button" class="icon-btn doc-tabs__add" data-tab-add title="打开文档" aria-label="打开文档">+</button>`;
      host.hidden = !ids.length && !cur;
      save();
      host.querySelector('.doc-tab.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
    /* A document is being shown: make sure it has a tab, placed after `after` (the tab
       that was active before) like an editor does. */
    function show(id, after = null) {
      if (!id) { render(); return; }
      if (!ids.includes(id)) {
        const at = after ? ids.indexOf(after) : -1;
        ids.splice(at >= 0 ? at + 1 : ids.length, 0, id);
      }
      render();
    }
    function close(id) {
      const i = ids.indexOf(id);
      if (i < 0) return;
      ids.splice(i, 1);
      render();
      if (opts.current() === id) opts.closed(ids[i] || ids[i - 1] || null);
    }

    host.addEventListener('click', (e) => {
      const x = e.target.closest('[data-tab-close]');
      if (x) { e.stopPropagation(); close(x.dataset.tabClose); return; }
      if (e.target.closest('[data-tab-add]')) { opts.add(e.target.closest('[data-tab-add]')); return; }
      const tab = e.target.closest('[data-tab]');
      if (tab && tab.dataset.tab !== opts.current()) opts.open(tab.dataset.tab);
    });
    host.addEventListener('auxclick', (e) => {
      const tab = e.button === 1 && e.target.closest('[data-tab]');
      if (tab) { e.preventDefault(); close(tab.dataset.tab); }
    });
    host.addEventListener('keydown', (e) => {
      const tab = e.target.closest('[data-tab]');
      if (!tab) return;
      const all = [...host.querySelectorAll('[data-tab]')], i = all.indexOf(tab);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault(); all[(i + (e.key === 'ArrowRight' ? 1 : -1) + all.length) % all.length]?.focus();
      } else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); opts.open(tab.dataset.tab); }
      else if (e.key === 'Delete') { e.preventDefault(); close(tab.dataset.tab); }
    });
    /* Drag to reorder: the tab is inserted before / after the one under the pointer. */
    host.addEventListener('dragstart', (e) => {
      const tab = e.target.closest('[data-tab]');
      if (!tab) return;
      dragId = tab.dataset.tab;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/x-tlm-tab', dragId);
      tab.classList.add('dragging');
    });
    host.addEventListener('dragover', (e) => {
      if (!dragId) return;
      const tab = e.target.closest('[data-tab]');
      e.preventDefault(); e.dataTransfer.dropEffect = 'move';
      host.querySelectorAll('.drop-before, .drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
      if (!tab || tab.dataset.tab === dragId) return;
      const r = tab.getBoundingClientRect();
      tab.classList.add(e.clientX < r.left + r.width / 2 ? 'drop-before' : 'drop-after');
    });
    host.addEventListener('drop', (e) => {
      if (!dragId) return;
      e.preventDefault();
      const tab = e.target.closest('[data-tab]');
      const from = ids.indexOf(dragId);
      if (from >= 0) {
        ids.splice(from, 1);
        let to = ids.length;
        if (tab && tab.dataset.tab !== dragId) {
          const r = tab.getBoundingClientRect();
          to = ids.indexOf(tab.dataset.tab) + (e.clientX < r.left + r.width / 2 ? 0 : 1);
        }
        ids.splice(to, 0, dragId);
      }
      dragId = null; render();
    });
    host.addEventListener('dragend', () => { dragId = null; render(); });

    return {
      /** Reload the order for the current project (call after switching projects). */
      reset() { load(); render(); },
      show, close, render,
      get ids() { return [...ids]; },
      /** Drop tabs whose document no longer exists. */
      prune() { const k = known(); ids = ids.filter((id) => k.has(id)); render(); },
    };
  }
  window.TLMDocTabs = { create };
})();
