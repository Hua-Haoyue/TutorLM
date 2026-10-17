/* TutorLM workbench · floating workspace Chat: an icon in the top bar opens a rounded,
   draggable / resizable card with conversation bubbles, a new-conversation button and
   a list of this session's conversations. Messages, tool traces and change proposals
   come from /api/chat/*; nothing is stored in the browser except the open thread id
   (sessionStorage) and the card geometry (localStorage). */
(function () {
  'use strict';
  const api = RuntimeAPI;
  const icon = (name) => TLM.icon(name);
  const esc = TLM.esc;
  const toggle = document.querySelector('#workspace-chat-toggle');
  const panel = document.createElement('aside');
  panel.id = 'workspace-chat'; panel.className = 'workspace-chat'; panel.hidden = true;
  panel.setAttribute('aria-label', '工作区 Chat');
  panel.innerHTML = `<header class="workspace-chat-head" tabindex="0" aria-label="拖动 Chat 卡片">
      <span class="workspace-chat-logo" aria-hidden="true">${icon('chat')}</span>
      <strong class="workspace-chat-title">新对话</strong>
      <span class="workspace-chat-spacer"></span>
      <button class="icon-btn" data-chat-list title="对话列表" aria-label="对话列表" aria-pressed="false">${icon('list')}</button>
      <button class="icon-btn" data-chat-new title="开启新对话" aria-label="开启新对话">${icon('file-plus')}</button>
      <button class="icon-btn" data-chat-close title="关闭 Chat" aria-label="关闭 Chat">${icon('close')}</button></header>
    <section class="workspace-chat-threads" aria-label="对话列表" hidden>
      <div class="workspace-chat-threads-head"><span class="label">对话</span>
        <button type="button" class="btn btn--sm" data-chat-new>${icon('plus')}新对话</button></div>
      <ul class="workspace-chat-thread-list" role="listbox" aria-label="对话"></ul></section>
    <div class="workspace-chat-messages" role="log" aria-live="polite"></div>
    <output class="workspace-chat-status" role="status"></output>
    <form class="workspace-chat-form"><textarea rows="1" maxlength="8000" aria-label="Chat 消息" placeholder="询问当前项目…（Enter 发送，Shift+Enter 换行）" required></textarea>
      <div class="workspace-chat-actions">
        <button type="button" class="icon-btn" data-chat-cancel title="停止生成" aria-label="停止生成" hidden>${icon('stop')}</button>
        <button type="submit" class="workspace-chat-send" title="发送消息" aria-label="发送消息">${icon('send')}</button></div></form>`;
  document.body.appendChild(panel);
  const log = panel.querySelector('[role=log]'), status = panel.querySelector('output');
  const form = panel.querySelector('form'), input = form.querySelector('textarea'), submit = form.querySelector('[type=submit]');
  const cancel = panel.querySelector('[data-chat-cancel]'), title = panel.querySelector('.workspace-chat-title');
  const listBtn = panel.querySelector('[data-chat-list]'), threadsEl = panel.querySelector('.workspace-chat-threads');
  const threadList = panel.querySelector('.workspace-chat-thread-list');
  const markdown = TutorMarkdown.createRenderer({ math: (text, display) => window.katex
    ? katex.renderToString(text, { displayMode: !!display, throwOnError: false, trust: false })
    : text.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])) });
  let threadId = null, busy = false, runId = null, pending = null;
  const key = () => 'tlm.chat.' + (api.lease?.workspace_id || window.TLM_WS.state.project?.id) + '.' + api.session?.id;
  function busyState(value) {
    busy = value; submit.disabled = value; cancel.hidden = !value; cancel.disabled = !value;
    panel.querySelectorAll('[data-chat-new]').forEach((b) => { b.disabled = value; });
  }

  /* ---------- geometry: draggable by the header, resizable, kept on screen ---------- */
  function clamp() {
    const width = Math.min(panel.offsetWidth || 440, innerWidth - 16), height = Math.min(panel.offsetHeight || 600, innerHeight - 24);
    const rect = panel.getBoundingClientRect();
    panel.style.width = Math.max(300, width) + 'px'; panel.style.height = Math.max(260, height) + 'px';
    panel.style.left = Math.max(8, Math.min(rect.left, innerWidth - width - 8)) + 'px';
    panel.style.top = Math.max(8, Math.min(rect.top, innerHeight - Math.min(height, 70))) + 'px';
  }
  try {
    const saved = JSON.parse(localStorage.getItem('tlm.chat.geometry'));
    if (saved) for (const k of ['left', 'top', 'width', 'height']) if (Number.isFinite(saved[k])) panel.style[k] = saved[k] + 'px';
  } catch { /* keep default geometry */ }
  function saveGeometry() {
    if (panel.hidden) return;
    const rect = panel.getBoundingClientRect();
    localStorage.setItem('tlm.chat.geometry', JSON.stringify({ left: rect.left, top: rect.top, width: rect.width, height: rect.height }));
  }
  new ResizeObserver(() => { if (!panel.hidden) { clamp(); saveGeometry(); } }).observe(panel);
  const head = panel.querySelector('header');
  let drag;
  head.onpointerdown = event => {
    if (event.button !== 0 || event.target.closest('button')) return;
    const rect = panel.getBoundingClientRect(); drag = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
    head.setPointerCapture(event.pointerId); event.preventDefault();
  };
  head.onpointermove = event => { if (!drag) return; panel.style.left = drag.left + event.clientX - drag.x + 'px'; panel.style.top = drag.top + event.clientY - drag.y + 'px'; clamp(); };
  head.onpointerup = head.onpointercancel = () => { drag = null; saveGeometry(); };
  head.onkeydown = event => {
    if (!event.key.startsWith('Arrow') || event.target !== head) return;
    event.preventDefault(); const rect = panel.getBoundingClientRect();
    panel.style.left = rect.left + (event.key === 'ArrowRight' ? 12 : event.key === 'ArrowLeft' ? -12 : 0) + 'px';
    panel.style.top = rect.top + (event.key === 'ArrowDown' ? 12 : event.key === 'ArrowUp' ? -12 : 0) + 'px'; clamp(); saveGeometry();
  };
  window.addEventListener('resize', () => { if (!panel.hidden) clamp(); });
  function close() { panel.hidden = true; toggle.setAttribute('aria-pressed', 'false'); toggle.focus(); }
  panel.querySelector('[data-chat-close]').onclick = close;
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !threadsEl.hidden) { e.stopPropagation(); showList(false); } });

  /* ---------- threads ---------- */
  function connected() {
    if (!window.TLM_WS?.state.project || !window.TLM_WS.state.connected) throw new Error('请先连接工作区');
  }
  async function load(id) {
    connected();
    try { return await api.get('/api/chat/threads/' + id); }
    catch (error) { if ([401, 404, 410].includes(error.status)) return null; throw error; }
  }
  async function current() {
    threadId = threadId || sessionStorage.getItem(key());
    const value = threadId && await load(threadId);
    if (value) return value;
    return fresh();
  }
  async function fresh() {
    connected();
    threadId = (await api.post('/api/chat/threads', {})).id; sessionStorage.setItem(key(), threadId);
    return api.get('/api/chat/threads/' + threadId);
  }
  const when = (seconds) => {
    if (!seconds) return '';
    const d = new Date(seconds * 1000), now = new Date();
    return d.toDateString() === now.toDateString()
      ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
  };
  async function renderList() {
    threadList.innerHTML = '<li class="workspace-chat-thread-empty">加载中…</li>';
    const { items } = await api.get('/api/chat/threads');
    threadList.innerHTML = items.length ? items.map((t) => `<li><button type="button" role="option" class="workspace-chat-thread${t.id === threadId ? ' on' : ''}"
        aria-selected="${t.id === threadId}" data-thread="${esc(t.id)}">
        <span class="workspace-chat-thread-title">${esc(t.title || '新对话')}</span>
        <span class="workspace-chat-thread-meta mono">${when(t.updated)}${t.messages ? ` · ${t.messages} 条` : ''}</span></button></li>`).join('')
      : '<li class="workspace-chat-thread-empty">还没有对话</li>';
  }
  function showList(on) {
    threadsEl.hidden = !on; listBtn.setAttribute('aria-pressed', String(on));
    if (on) renderList().catch((e) => { threadList.innerHTML = `<li class="workspace-chat-thread-empty">${esc(e.message)}</li>`; });
    else input.focus();
  }
  listBtn.onclick = () => showList(threadsEl.hidden);
  threadList.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-thread]');
    if (!b || busy) return;
    const value = await load(b.dataset.thread).catch((error) => { status.textContent = error.message; return null; });
    if (!value) { status.textContent = '这条对话已不可用'; renderList().catch(() => {}); return; }
    threadId = value.id; sessionStorage.setItem(key(), threadId);
    showList(false); render(value); status.textContent = '';
  });

  /* ---------- messages ---------- */
  const SUGGESTIONS = ['总结一下这个项目', '解释当前打开的文档', '这些知识点之间有什么联系？'];
  function bubble(role, html, extra = '') {
    const article = document.createElement('article');
    article.className = 'workspace-chat-message ' + role;
    article.innerHTML = `${role === 'assistant' ? `<span class="workspace-chat-avatar" aria-hidden="true">T</span>` : ''}
      <div class="workspace-chat-bubble"><span class="sr-only">${role === 'user' ? '你：' : 'TutorLM：'}</span>${html}${extra}</div>`;
    return article;
  }
  function render(value) {
    log.replaceChildren();
    const firstUser = value.messages.find((m) => m.role === 'user');
    title.textContent = firstUser ? firstUser.body.text.replace(/\s+/g, ' ').slice(0, 40) : '新对话';
    if (!value.messages.length && !pending) {
      const empty = document.createElement('div');
      empty.className = 'workspace-chat-empty';
      empty.innerHTML = `<span class="workspace-chat-empty-icon" aria-hidden="true">${icon('chat')}</span>
        <p>可以询问当前项目里的文档、代码和知识节点。修改内容前会先请你确认。</p>
        <div class="workspace-chat-suggestions">${SUGGESTIONS.map((s) => `<button type="button" class="workspace-chat-suggestion">${esc(s)}</button>`).join('')}</div>`;
      log.appendChild(empty);
    }
    for (const message of value.messages) {
      const tools = message.body.tools?.length
        ? `<div class="workspace-chat-tools">${message.body.tools.map((t) => `<span class="workspace-chat-tool ${t.status === 'ok' ? '' : 'bad'}">${esc(t.tool)} · ${esc(t.status)}</span>`).join('')}</div>` : '';
      const body = message.role === 'user' ? esc(message.body.text).replace(/\n/g, '<br>')
        : `<div class="answer">${markdown.render(message.body.text, { blocks: false })}</div>`;
      const article = bubble(message.role === 'user' ? 'user' : 'assistant', body, tools);
      if (message.created) article.title = new Date(message.created * 1000).toLocaleString('zh-CN');
      log.appendChild(article);
    }
    for (const proposal of value.proposals) {
      const box = document.createElement('section'); box.className = 'workspace-chat-proposal';
      const summary = document.createElement('strong');
      summary.textContent = { pending: '待确认的变更', applied: '已应用的变更', rejected: '已拒绝的变更' }[proposal.state] || '变更 · ' + proposal.state;
      box.appendChild(summary);
      for (const change of proposal.changes) {
        const detail = document.createElement('details'), label = document.createElement('summary'), preview = document.createElement('pre');
        label.textContent = change.kind === 'file' ? change.path : change.kind === 'document' ? change.document_id
          : change.kind === 'explanation' ? '解释 · ' + change.node_id : change.title;
        preview.textContent = change.kind === 'node' ? change.content : change.text; detail.append(label, preview); box.appendChild(detail);
      }
      if (proposal.state === 'pending') for (const [approve, label] of [[true, '应用变更'], [false, '拒绝']]) {
        const button = document.createElement('button'); button.className = 'btn btn--sm' + (approve ? ' btn--primary' : ''); button.type = 'button'; button.textContent = label;
        button.onclick = async () => {
          button.disabled = true;
          try { await api.post('/api/chat/proposals/' + proposal.id, { approve }); render(await current()); if (approve) await window.TLM_WS.refresh(); }
          catch (error) { status.textContent = error.message; } finally { button.disabled = false; }
        };
        box.appendChild(button);
      }
      log.appendChild(box);
    }
    if (pending) {
      log.appendChild(bubble('user', esc(pending).replace(/\n/g, '<br>')));
      const wait = bubble('assistant', '<span class="workspace-chat-typing" aria-label="正在回答"><i></i><i></i><i></i></span>');
      wait.classList.add('waiting'); log.appendChild(wait);
    }
    log.scrollTop = log.scrollHeight;
  }
  log.addEventListener('click', (e) => {
    const s = e.target.closest('.workspace-chat-suggestion');
    if (s) { input.value = s.textContent; grow(); form.requestSubmit(); }
  });

  toggle.onclick = async () => {
    if (!panel.hidden) { close(); return; }
    panel.hidden = false; toggle.setAttribute('aria-pressed', 'true'); clamp(); input.focus();
    try { render(await current()); status.textContent = ''; } catch (error) { status.textContent = error.message; }
  };
  panel.querySelectorAll('[data-chat-new]').forEach((b) => { b.onclick = async () => {
    if (busy) return;
    try { sessionStorage.removeItem(key()); threadId = null; showList(false); render(await fresh()); status.textContent = ''; }
    catch (error) { status.textContent = error.message; }
  }; });
  cancel.onclick = () => { if (runId) api.cancel(runId).catch(error => { status.textContent = error.message; }); };
  form.onsubmit = async event => {
    event.preventDefault();
    const message = input.value.trim();
    if (busy || !message) return;
    busyState(true); status.textContent = '';
    try {
      const value = await current();
      pending = message; input.value = ''; grow(); render(value);
      await api.run('workspace_chat', { thread_id: threadId, message, model: window.TLM_WS.modelOptions() },
        { onRun: run => { runId = run.id; status.textContent = run.state === 'running' ? '检索与生成中…' : run.state === 'queued' ? '排队中…' : ''; } });
      pending = null; render(await current()); status.textContent = '';
    } catch (error) {
      if (pending && !input.value) input.value = pending;
      pending = null; grow(); status.textContent = error.message;
      current().then(render).catch(() => {});
    } finally { runId = null; busyState(false); }
  };
  /* Enter sends, Shift+Enter breaks the line; the box grows with its text. */
  function grow() { input.style.height = 'auto'; input.style.height = Math.min(160, input.scrollHeight) + 'px'; }
  input.addEventListener('input', grow);
  input.onkeydown = event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  };

  const importer = document.querySelector('#code-project-input');
  document.querySelector('#code-import').onclick = () => importer.click();
  importer.onchange = async () => {
    if (!importer.files.length) return;
    try {
      const data = new FormData(); data.append('file', importer.files[0]);
      const result = await api.post('/api/code/import', data);
      await window.TLM_WS.refresh(); if (result.items[0]) await window.TLM_WS.openDocument(result.items[0].id);
      TLM.toast('代码工程已导入并建立索引');
    } catch (error) { TLM.toast(error.message); } finally { importer.value = ''; }
  };
})();
