(function () {
  'use strict';
  const api = RuntimeAPI;
  const toggle = document.querySelector('#workspace-chat-toggle');
  const panel = document.createElement('aside');
  panel.id = 'workspace-chat'; panel.className = 'workspace-chat'; panel.hidden = true;
  panel.setAttribute('aria-label', '工作区 Chat');
  panel.innerHTML = `<header class="workspace-chat-head" tabindex="0" aria-label="拖动 Chat 卡片"><strong>Chat</strong>
    <span class="workspace-chat-spacer"></span><button class="icon-btn" data-chat-new title="新建对话" aria-label="新建对话">+</button>
    <button class="icon-btn" data-chat-close title="关闭 Chat" aria-label="关闭 Chat"><svg class="ic"><use href="#i-close"/></svg></button></header>
    <div class="workspace-chat-messages" role="log" aria-live="polite"></div>
    <output class="workspace-chat-status" role="status"></output>
    <form class="workspace-chat-form"><textarea rows="2" maxlength="8000" aria-label="Chat 消息" placeholder="询问当前项目…" required></textarea>
    <div class="workspace-chat-actions"><button type="button" class="icon-btn" data-chat-cancel title="停止生成" aria-label="停止生成" disabled><svg class="ic"><use href="#i-stop"/></svg></button>
    <button type="submit" class="icon-btn" title="发送消息" aria-label="发送消息"><svg class="ic"><use href="#i-arrow-r"/></svg></button></div></form>`;
  document.body.appendChild(panel);
  const log = panel.querySelector('[role=log]'), status = panel.querySelector('output');
  const form = panel.querySelector('form'), input = form.querySelector('textarea'), submit = form.querySelector('[type=submit]');
  const cancel = panel.querySelector('[data-chat-cancel]'), newButton = panel.querySelector('[data-chat-new]');
  const markdown = TutorMarkdown.createRenderer({ math: text => window.katex ? katex.renderToString(text, { throwOnError: false, trust: false }) : text.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])) });
  let threadId = null, busy = false, runId = null;
  function key() { return 'tlm.chat.' + (api.lease?.workspace_id || window.TLM_WS.state.project?.id) + '.' + api.session?.id; }
  function busyState(value) { busy = value; submit.disabled = value; cancel.disabled = !value; newButton.disabled = value; }
  function clamp() {
    const width = Math.min(panel.offsetWidth || 440, innerWidth - 16), height = Math.min(panel.offsetHeight || 560, innerHeight - 24);
    const rect = panel.getBoundingClientRect();
    panel.style.width = Math.max(280, width) + 'px'; panel.style.height = Math.max(220, height) + 'px';
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
    localStorage.setItem('tlm.chat.geometry', JSON.stringify({left:rect.left,top:rect.top,width:rect.width,height:rect.height}));
  }
  new ResizeObserver(() => { if (!panel.hidden) { clamp(); saveGeometry(); } }).observe(panel);
  const head = panel.querySelector('header');
  let drag;
  head.onpointerdown = event => {
    if (event.button !== 0 || event.target.closest('button')) return;
    const rect = panel.getBoundingClientRect(); drag = { x:event.clientX,y:event.clientY,left:rect.left,top:rect.top };
    head.setPointerCapture(event.pointerId); event.preventDefault();
  };
  head.onpointermove = event => { if (!drag) return; panel.style.left = drag.left + event.clientX - drag.x + 'px'; panel.style.top = drag.top + event.clientY - drag.y + 'px'; clamp(); };
  head.onpointerup = head.onpointercancel = () => { drag = null; saveGeometry(); };
  head.onkeydown = event => { if (!event.key.startsWith('Arrow') || event.target !== head) return;
    event.preventDefault(); const rect = panel.getBoundingClientRect();
    panel.style.left = rect.left + (event.key === 'ArrowRight' ? 12 : event.key === 'ArrowLeft' ? -12 : 0) + 'px';
    panel.style.top = rect.top + (event.key === 'ArrowDown' ? 12 : event.key === 'ArrowUp' ? -12 : 0) + 'px'; clamp(); saveGeometry(); };
  window.addEventListener('resize', () => { if (!panel.hidden) clamp(); });
  function close() { panel.hidden = true; toggle.setAttribute('aria-pressed', 'false'); toggle.focus(); }
  panel.querySelector('[data-chat-close]').onclick = close;
  async function ensureThread() {
    if (!window.TLM_WS?.state.project || !window.TLM_WS.state.connected) throw new Error('请先连接工作区');
    threadId = threadId || sessionStorage.getItem(key());
    if (threadId) {
      try { return await api.get('/api/chat/threads/' + threadId); }
      catch (error) { if (![401,404,410].includes(error.status)) throw error; threadId = null; }
    }
    threadId = (await api.post('/api/chat/threads', {})).id; sessionStorage.setItem(key(), threadId);
    return api.get('/api/chat/threads/' + threadId);
  }
  function render(value) {
    log.replaceChildren();
    for (const message of value.messages) {
      const article = document.createElement('article'); article.className = 'workspace-chat-message ' + message.role;
      const role = document.createElement('strong'); role.textContent = message.role === 'user' ? '你' : 'TutorLM'; article.appendChild(role);
      const body = document.createElement('div'); body.className = 'answer'; body.innerHTML = markdown.render(message.body.text, { blocks:false }); article.appendChild(body);
      if (message.body.tools?.length) { const trace = document.createElement('small'); trace.textContent = message.body.tools.map(t => `${t.tool}: ${t.status}`).join(' · '); article.appendChild(trace); }
      log.appendChild(article);
    }
    for (const proposal of value.proposals) {
      const box = document.createElement('section'); box.className = 'workspace-chat-proposal';
      const summary = document.createElement('strong'); summary.textContent = '变更 · ' + proposal.state; box.appendChild(summary);
      for (const change of proposal.changes) {
        const detail = document.createElement('details'), label = document.createElement('summary'), preview = document.createElement('pre');
        label.textContent = change.kind === 'file' ? change.path : change.kind === 'document' ? change.document_id
          : change.kind === 'explanation' ? '解释 · ' + change.node_id : change.title;
        preview.textContent = change.kind === 'node' ? change.content : change.text; detail.append(label,preview); box.appendChild(detail);
      }
      if (proposal.state === 'pending') for (const [approve, label] of [[true,'应用变更'],[false,'拒绝']]) {
        const button = document.createElement('button'); button.className = 'btn btn--sm'; button.type = 'button'; button.textContent = label;
        button.onclick = async () => { button.disabled = true;
          try { await api.post('/api/chat/proposals/' + proposal.id, {approve}); render(await ensureThread()); if (approve) await window.TLM_WS.refresh(); }
          catch (error) { status.textContent = error.message; } finally { button.disabled = false; }
        }; box.appendChild(button);
      }
      log.appendChild(box);
    }
    log.scrollTop = log.scrollHeight;
  }
  toggle.onclick = async () => {
    if (!panel.hidden) { close(); return; }
    panel.hidden = false; toggle.setAttribute('aria-pressed','true'); clamp(); input.focus();
    try { render(await ensureThread()); status.textContent = ''; } catch(error) { status.textContent = error.message; }
  };
  newButton.onclick = async () => {
    if (busy) return;
    try { threadId = null; sessionStorage.removeItem(key()); render(await ensureThread()); status.textContent = ''; }
    catch (error) { status.textContent = error.message; }
  };
  cancel.onclick = () => { if (runId) api.cancel(runId).catch(error => { status.textContent = error.message; }); };
  form.onsubmit = async event => {
    event.preventDefault(); if (busy || !input.value.trim()) return;
    busyState(true); status.textContent = '处理中…';
    try {
      await ensureThread();
      await api.run('workspace_chat', {thread_id:threadId,message:input.value.trim(),model:window.TLM_WS.modelOptions()},
        {onRun:run => {runId=run.id; status.textContent = run.state === 'running' ? '检索与生成中…' : run.state;}});
      input.value = ''; render(await ensureThread()); status.textContent = '';
    } catch(error) { status.textContent = error.message; }
    finally { runId = null; busyState(false); }
  };
  input.onkeydown = event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.requestSubmit(); } };
  const importer = document.querySelector('#code-project-input');
  document.querySelector('#code-import').onclick = () => importer.click();
  importer.onchange = async () => {
    if (!importer.files.length) return;
    try { const data = new FormData(); data.append('file', importer.files[0]);
      const result = await api.post('/api/code/import', data);
      await window.TLM_WS.refresh(); if (result.items[0]) await window.TLM_WS.openDocument(result.items[0].id);
      TLM.toast('代码工程已导入并建立索引');
    } catch(error) { TLM.toast(error.message); } finally { importer.value=''; }
  };
})();
