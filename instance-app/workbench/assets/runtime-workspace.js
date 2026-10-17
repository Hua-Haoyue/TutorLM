/* TutorLM v2 · Workspace on the real App Runtime API.
   Server-owned: projects, documents (+ full text), nodes (+ metadata: anchor / status / position),
   edges, explanations, narrations, runs. Browser-owned (localStorage, per project): view prefs and
   reading highlights, which have no server contract and are labelled as local.
   Sections: state · helpers · markdown · reader · selection · find · canvas · nodes · runs
             · inspector · narration · explorer · menus · palette · session · keyboard · boot */
(function () {
  'use strict';
  const api = RuntimeAPI, { esc, icon, toast, fmtClock } = TLM;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const p = api.part;
  const app = $('#app');

  /* ================= State ================= */
  const S = {
    projects: [], project: null, docs: [], doc: null, text: '', nodes: [], edges: [],
    history: {}, runs: {}, pendingChat: {}, selected: null, kinds: new Set(), views: {}, modes: {},
    view: { x: 0, y: 0, z: 0.85 }, layout: {}, mode: 'md', tool: 'select',
    find: { open: false, q: '', hits: [], i: -1 }, ask: null, modal: false, connected: false,
  };
  const KIND = { section: '章节', concept: '概念', example: '例子', exercise: '练习', summary: '小结' };
  const REL = { parent_of: '包含', explains: '解释', supports: '支持', depends_on: '依赖', prerequisite_of: '前置于',
    contradicts: '矛盾', example_of: '例子', summarizes: '总结', derived_from: '派生自', references: '引用', related_to: '相关' };
  const projectPath = () => `/api/projects/${p(S.project.id)}`;
  let revision = 0;
  let pendingShareCode = '';   // set from ?share=..., redeemed once in start()

  /* View prefs + local highlights (never business data) */
  const prefKey = () => `tlm.view.${S.project?.id}`;
  const hlKey = () => `tlm.hl.${S.project?.id}`;
  function readJSON(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } }
  function savePrefs() {
    if (!S.project) return;
    if (S.doc) S.views[S.doc.id] = { ...S.view };
    localStorage.setItem(prefKey(), JSON.stringify({ doc: S.doc?.id, node: S.selected, views: S.views, modes: S.modes }));
  }
  let highlights = [];
  const saveHighlights = () => localStorage.setItem(hlKey(), JSON.stringify(highlights));

  /* ================= Helpers ================= */
  const nodeOf = (id) => S.nodes.find((n) => n.id === id);
  const docOf = (id) => S.docs.find((d) => d.id === id);
  const meta = (n) => (n && n.metadata) || {};
  const childrenOf = (id) => S.nodes.filter((n) => n.parent_id === id);
  /* Each document owns its node space: the canvas shows only the open document's nodes. */
  const inDoc = (n) => !!S.doc && !!n && n.document_id === S.doc.id;
  const docNodes = () => S.nodes.filter(inDoc);
  const elOf = (n) => n && nodesEl.querySelector(`.gnode[data-id="${CSS.escape(n.id)}"]`);
  const collapse = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n) + '…' : s);
  const isEditable = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  const running = (id) => !!S.runs[id] && !api.TERMINAL.has(S.runs[id].state);
  function depthOf(n) {
    let d = 0, cur = n; const seen = new Set();
    while (cur && cur.parent_id && !seen.has(cur.id) && d < 12) { seen.add(cur.id); cur = nodeOf(cur.parent_id); if (cur) d++; }
    return d;
  }
  const depthLabel = (n) => 'L' + depthOf(n);
  function guessTitle(q) {
    const s = collapse(q);
    if (/[㐀-鿿]/.test(s)) return clip(s, 18);
    const w = s.split(' ');
    return clip(w.slice(0, 8).join(' ') + (w.length > 8 ? '…' : ''), 120);
  }
  function download(name, text, type = 'text/markdown') {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: type + ';charset=utf-8' }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); }
    catch { const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  }
  /* Render `text` as a QR SVG string using the bundled qrcode-generator.
     Returns '' when the library is missing so the modal degrades gracefully. */
  function qrSvg(text, size = 180) {
    if (typeof qrcode !== 'function') return '';
    try {
      const qr = qrcode(0, 'M');            // 0 = auto type number
      qr.addData(text);
      qr.make();
      return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true }).replace('<svg ', `<svg width="${size}" height="${size}" `);
    } catch { return ''; }
  }

  /* ================= Preferences =================
     Browser-local (localStorage 'tlm.prefs'); applied instantly. Explanation
     length / style / language use validated workflow fields when available;
     older backends still receive the legacy question hint. */
  const PREF_KEY = 'tlm.prefs';
  const PREF_DEFAULT = { hl: '#F2D58A', follow: true, links: true, minimap: true, length: 'medium', style: 'auto', lang: 'auto', readFont: 'serif', pen: 'pen', inkColor: '#C2402F' };
  const HL_COLORS = [['#F2D58A', '麦黄'], ['#F5B48C', '杏色'], ['#A8D5B5', '薄荷'], ['#A9C8EC', '天蓝'], ['#E8A9C3', '玫瑰'], ['#C8B6EA', '薰衣草']];
  const prefs = { ...PREF_DEFAULT, ...readJSON(PREF_KEY, {}) };
  const LENGTH_HINT = { short: '请用三句话以内简短回答', medium: '', long: '请详细回答，分点说明，并给出推导或例子' };
  const STYLE_HINT = { auto: '', plain: '用通俗易懂的语言', rigorous: '严格说明上下文的逻辑关系、假设条件，并逐步补全原文省略的数学推导', example: '用具体例子展示输入、过程和结果', analogy: '用生活中的类比帮助理解并说明类比的边界', socratic: '用有针对性的问题和提示引导思考，明确要求答案时给出完整解释' };
  const LANG_HINT = { auto: '', zh: '请用中文回答', en: 'Answer in English' };
  function withStyle(question) {
    const hint = [LENGTH_HINT[prefs.length], STYLE_HINT[prefs.style], LANG_HINT[prefs.lang]].filter(Boolean).join('；');
    const base = String(question || '').trim();
    if (!hint) return base.slice(0, 1000);
    return `${(base || '请解释这个知识点。').slice(0, 1000 - hint.length - 4)}\n（${hint}）`;
  }
  function explanationInput(nodeId, question, responseMode) {
    const workflow = (api.runtime?.plugins || []).flatMap(plugin => plugin.workflows || [])
      .find(workflow => workflow.kind === 'explanation');
    const fields = workflow?.input_schema?.properties || {};
    const q = fields.preferences ? String(question || '').trim().slice(0, 1000) : withStyle(question);
    return {
      node_id: nodeId,
      ...(fields.response_mode ? { response_mode: responseMode } : {}),
      ...(q ? { question: q } : {}),
      ...(fields.preferences ? { preferences: {
        style: Object.hasOwn(STYLE_HINT, prefs.style) ? prefs.style : 'auto',
        length: Object.hasOwn(LENGTH_HINT, prefs.length) ? prefs.length : 'medium',
        language: Object.hasOwn(LANG_HINT, prefs.lang) ? prefs.lang : 'auto',
      } } : {}),
    };
  }
  function applyPrefs() {
    const root = document.documentElement;
    root.style.setProperty('--hl', prefs.hl);
    root.style.setProperty('--read-font', prefs.readFont === 'sans' ? 'var(--sans)' : 'var(--serif)');
    app.classList.toggle('no-minimap', !prefs.minimap);
    const fb = $('#follow-btn');
    if (fb) { fb.setAttribute('aria-pressed', String(!!prefs.follow)); fb.title = prefs.follow ? '跟随阅读区滚动：开' : '跟随阅读区滚动：关'; }
  }
  function setPref(k, v) {
    prefs[k] = v;
    localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    applyPrefs();
    if (k === 'links' || k === 'minimap') { drawConnector(); drawMinimap(); }
    if (k === 'follow' && v) followScroll();
    if (k === 'readFont') { updateStatus(); drawConnector(); }
  }
  function openPrefs() {
    closeMenus();
    const opt = (key, items, cur) => `<div class="opt" role="radiogroup" data-pref="${key}">${items.map(([v, l]) =>
      `<button type="button" role="radio" data-val="${v}" aria-checked="${String(cur) === String(v)}">${l}</button>`).join('')}</div>`;
    const sw = (key, label) => `<button type="button" class="switch" role="switch" data-switch="${key}" aria-checked="${!!prefs[key]}" aria-label="${label}"></button>`;
    modal({
      title: '偏好设置',
      body: `<div class="prefs">
        <section><h3>外观</h3>
          <div class="pref-row"><span>主题</span>${opt('theme', [['light', '浅色'], ['dark', '深色'], ['system', '跟随系统']], TLM.theme.mode())}</div>
          <div class="pref-row"><span>原文高亮颜色</span><div class="colors" role="radiogroup" data-pref="hl">${HL_COLORS.map(([c, n]) =>
            `<button type="button" role="radio" data-val="${c}" aria-checked="${prefs.hl === c}" title="${n}" aria-label="${n}" style="background:${c}"></button>`).join('')}</div></div>
          <div class="pref-row"><span>阅读字体</span>${opt('readFont', [['serif', '衬线'], ['sans', '无衬线']], prefs.readFont)}</div>
          <div class="pref-row"><span>正文字号</span>${opt('docScale', [['0.9', '小'], ['1', '标准'], ['1.15', '大'], ['1.3', '特大']], String(Math.round(docScale * 100) / 100))}</div>
        </section>
        <section><h3>解释</h3>
          <div class="pref-row"><span>长度</span>${opt('length', [['short', '简短'], ['medium', '适中'], ['long', '详细']], prefs.length)}</div>
          <div class="pref-row"><span>风格</span>${opt('style', [['auto', '默认'], ['plain', '通俗'], ['rigorous', '严谨'], ['example', '多举例'], ['analogy', '类比'], ['socratic', '引导式']], prefs.style)}</div>
          <div class="pref-row"><span>语言</span>${opt('lang', [['auto', '跟随原文'], ['zh', '中文'], ['en', 'English']], prefs.lang)}</div>
        </section>
        <section><h3>知识图</h3>
          <div class="pref-row"><span>跟随文档滚动</span>${sw('follow', '跟随文档滚动')}</div>
          <div class="pref-row"><span>始终显示原文连线</span>${sw('links', '始终显示原文连线')}</div>
          <div class="pref-row"><span>右侧缩略条</span>${sw('minimap', '右侧缩略条')}</div>
        </section></div>`,
      actions: [{ label: '恢复默认', run: (c) => { Object.assign(prefs, PREF_DEFAULT); localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); applyPrefs(); drawConnector(); drawMinimap(); c(); toast('已恢复默认偏好'); } },
        { label: '完成', primary: true, run: (c) => c() }],
      onOpen: (root) => {
        root.addEventListener('click', (e) => {
          const b = e.target.closest('[data-pref] [data-val]');
          if (b) {
            const key = b.closest('[data-pref]').dataset.pref, v = b.dataset.val;
            $$('[data-val]', b.parentElement).forEach((x) => x.setAttribute('aria-checked', String(x === b)));
            if (key === 'theme') TLM.theme.setMode(v);
            else if (key === 'docScale') { docScale = Number(v); applyDocScale(); }
            else setPref(key, v);
          }
          const s = e.target.closest('[data-switch]');
          if (s) { const on = s.getAttribute('aria-checked') !== 'true'; s.setAttribute('aria-checked', String(on)); setPref(s.dataset.switch, on); }
        });
      },
    });
  }

  /* ================= Status + errors ================= */
  let pendingCount = 0;
  function paintState() {
    const el = $('#st-state');
    const gen = Object.keys(S.runs).filter(running).length;
    el.textContent = gen ? `生成中 · ${gen}` : pendingCount ? '同步中…' : S.connected ? '已同步' : '未连接';
    el.classList.toggle('busy', gen > 0 || pendingCount > 0);
  }
  /* Wrap a server write so the status bar shows it and failures surface instead of vanishing. */
  async function sync(promise) {
    pendingCount++; paintState();
    try { return await promise; } finally { pendingCount--; paintState(); }
  }
  function describe(e) {
    if (e.status === 429) return '已达到本次试用的限额：' + e.message;
    if (e.status === 503) return '服务繁忙，请稍后再试' + (e.retryAfter ? `（约 ${e.retryAfter} 秒）` : '');
    if (e.status === 0) return e.message;
    return e.message || '请求失败';
  }
  function fail(e) {
    if (e?.name === 'AbortError') return;
    console.error(e);
    if ([401, 410].includes(e.status) && S.connected) { S.connected = false; paintState(); reconnect(e); return; }
    toast(describe(e), { ms: 6000 });
  }
  const action = (fn) => (...args) => Promise.resolve().then(() => fn(...args)).catch(fail);

  /* ================= Modals ================= */
  const modalHost = $('#modal-host');
  function modal({ title, body, actions = [], wide = false, locked = false, onOpen }) {
    const prevFocus = document.activeElement;
    modalHost.innerHTML = `<div class="modal-backdrop"><div class="modal ${wide ? 'modal--lg' : ''}" role="dialog" aria-modal="true" aria-labelledby="m-title">
      <div class="modal__head"><h2 class="modal__title" id="m-title">${esc(title)}</h2>${locked ? '' : `<button class="icon-btn" data-close aria-label="关闭">${icon('close')}</button>`}</div>
      <div class="modal__body">${body}</div>
      ${actions.length ? `<div class="modal__foot">${actions.map((a, i) => `<button class="btn ${a.primary ? 'btn--primary' : ''}" data-i="${i}" ${a.id ? `id="${a.id}"` : ''}>${a.label}</button>`).join('')}</div>` : ''}
    </div></div>`;
    S.modal = true;
    const root = modalHost.firstElementChild;
    const close = () => { modalHost.innerHTML = ''; S.modal = false; document.removeEventListener('keydown', onKey, true); setTimeout(() => prevFocus?.isConnected && prevFocus.focus?.(), 0); };
    const onKey = (e) => {
      if (e.key === 'Escape' && !locked) { e.stopPropagation(); close(); }
      if (e.key === 'Tab') {
        const f = $$('button:not(:disabled), input, textarea, select, a[href]', root);
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
    };
    document.addEventListener('keydown', onKey, true);
    root.addEventListener('mousedown', (e) => { if (e.target === root && !locked) close(); });
    root.addEventListener('click', (e) => {
      if (e.target.closest('[data-close]')) close();
      const b = e.target.closest('[data-i]');
      if (b && !b.disabled) Promise.resolve(actions[b.dataset.i].run(close, root, b)).catch(fail);
    });
    onOpen?.(root, close);
    ($('input:not([type=checkbox]), textarea', root) || $('.btn--primary', root) || $('button', root))?.focus();
    return { root, close };
  }
  const confirmModal = (title, text, ok = '确定') => new Promise((res) => {
    modal({ title, body: `<p style="margin:0">${text}</p>`, actions: [
      { label: '取消', run: (c) => { c(); res(false); } },
      { label: ok, primary: true, run: (c) => { c(); res(true); } }] });
  });
  const promptModal = (title, label, value = '', max = 120, allowEmpty = false) => new Promise((res) => {
    const { root } = modal({ title, body: `<label class="field"><span class="label">${label}</span><input class="input" id="pm-in" maxlength="${max}" value="${esc(value)}"></label>`,
      actions: [{ label: '取消', run: (c) => { c(); res(null); } }, { label: '确定', primary: true, run: (c, r) => { const v = $('#pm-in', r).value.trim(); if (!v && !allowEmpty) return; c(); res(v); } }] });
    $('#pm-in', root).addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('.btn--primary', root).click(); } });
  });

  /* ================= Markdown (subset, escape-first) =================
     Top-level blocks get data-b="<index>" so anchors survive re-render. */
  function mathHtml(m) {
    if (window.katex) return katex.renderToString(m, { throwOnError: false, trust: false, strict: 'ignore', output: 'html' });
    return esc(m)
      .replace(/\^(\{[^}]*\}|[\wᵀ]+)/g, (_, x) => '<sup>' + x.replace(/[{}]/g, '') + '</sup>')
      .replace(/_(\{[^}]*\}|[A-Za-z0-9]+)/g, (_, x) => '<sub>' + x.replace(/[{}]/g, '') + '</sub>');
  }
  const markdown = TutorMarkdown.createRenderer({ math: mathHtml });
  const inline = value => markdown.inline(value);
  const renderMd = (raw, options) => markdown.render(raw, options);
  const isMarkdown = (d) => !d || (d.format ? d.format === 'md' : !/\.txt$/i.test(d.source_filename || d.title || ''));
  const renderTxt = (raw) => [...String(raw || '').matchAll(/\S[\s\S]*?(?=\r?\n\s*\r?\n|$)/g)]
    .map((m, i) => `<p data-b="${i}" data-start="${m.index}" data-end="${m.index + m[0].length}">${esc(m[0].trim()).replace(/\r?\n/g, '<br>')}</p>`).join('');
  const wordCount = (raw) => ((raw.match(/[㐀-鿿]/g) || []).length + (raw.match(/[A-Za-z0-9_]+/g) || []).length);

  /* ================= Reader ================= */
  const sheet = $('#sheet');
  const scroller = $('#reader-scroll');

  function docHead(d) {
    const anchored = S.nodes.filter((n) => n.document_id === d.id).length;
    const kb = d.size_bytes ? (d.size_bytes / 1024).toFixed(1) + ' KB' : '';
    const kindLabel = { md: 'MARKDOWN', txt: 'TXT', pdf: 'PDF', tex: 'TEX' }[d.format] || (isMarkdown(d) ? 'MARKDOWN' : 'TXT');
    return `<header class="doc-head"><div class="mono"><span>${kindLabel}${kb ? ' · ' + kb : ''}</span><span>${anchored} 个节点</span></div>
      <h1>${esc(baseName(d.title))}</h1><p>${esc(d.source_filename || '手动创建')} · ${wordCount(S.text).toLocaleString()} 字</p></header>`;
  }
  function syncViewSeg() {
    $$('.seg [data-view]').forEach((b) => {
      b.setAttribute('aria-checked', String(b.dataset.view === S.mode));
      b.disabled = !S.doc;
    });
  }
  function renderReader(keepScroll) {
    const top = scroller.scrollTop;
    resetFollowPosition();
    hideAsk();
    syncViewSeg();
    if (!S.doc) {
      sheet.innerHTML = `<div class="empty-doc"><div class="mono muted">无打开的文档</div>
        <p>${S.docs.length ? '从左侧选择一个文档。' : '导入 .md / .txt，或新建文档粘贴正文，开始阅读。'}</p>
        <div style="display:flex;gap:8px"><button class="btn" data-act="import">${icon('upload')}导入文件</button><button class="btn btn--ghost" data-act="new-file">新建文档</button></div></div>`;
      updateStatus(); drawConnector();
      return;
    }
    if (pdfv && !pdfv.key.startsWith(S.doc.id + ':')) { pdfv.view.destroy().catch(() => {}); pdfv = null; }
    if (isPdfView()) {
      sheet.innerHTML = docHead(S.doc) + '<div class="pdfv" id="pdfv"><p class="mono muted pdfv-msg">正在加载 PDF…</p></div>';
      mountPdf(keepScroll ? top : 0);
      updateStatus(); drawConnector();
      return;
    }
    if (S.mode === 'raw') {
      sheet.innerHTML = `<div class="raw-note"><span class="label">源文本 · 只读</span><span class="mono muted">${S.text.length.toLocaleString()} 字符</span></div>
        <pre class="raw" tabindex="0" aria-label="源文本">${esc(S.text)}</pre>`;
    } else {
      const body = isMarkdown(S.doc) ? renderMd(S.text) : renderTxt(S.text);
      if (S.mode === 'pdf') {
        sheet.innerHTML = `<div class="pdf-pages"><section class="pdf-page"><div class="doc">${docHead(S.doc)}${body}</div></section></div>`;
        paginate();
      } else {
        sheet.innerHTML = docHead(S.doc) + `<div class="doc">${body}</div>`;
      }
      applyMarks();
      paintInk();
      renderVisualOriginal().catch(fail);
    }
    scroller.scrollTop = keepScroll ? top : 0;
    resetFollowPosition();
    updateStatus(); drawConnector();
  }
  let visualObjectURL = null;
  async function renderVisualOriginal() {
    const source = S.nodes.find(n => n.document_id === S.doc?.id && meta(n).visual_source?.selector?.type === 'whole-image')?.metadata.visual_source;
    if (!source) return;
    const docId = S.doc.id;
    const bytes = await api.bytes(`/api/documents/${p(docId)}/artifacts/${p(source.artifact_id)}/source`);
    if (S.doc?.id !== docId || S.mode === 'raw') return;
    const image = document.createElement('img'); image.className = 'visual-original'; image.alt = S.doc.title;
    if (visualObjectURL) URL.revokeObjectURL(visualObjectURL);
    visualObjectURL = URL.createObjectURL(new Blob([bytes], { type: 'image/png' })); image.src = visualObjectURL;
    sheet.querySelector('.visual-original')?.remove(); sheet.appendChild(image);
  }
  /* Layout view: split rendered blocks into fixed-height pages (no PDF parsing involved). */
  function paginate() {
    const wrap = $('.pdf-pages', sheet), PAGE = 900;
    const firstDoc = $('.pdf-page .doc', wrap);
    const blocks = [...firstDoc.children];
    firstDoc.innerHTML = '';
    let cur = firstDoc;
    blocks.forEach((bk) => {
      cur.appendChild(bk);
      if (cur.parentElement.offsetHeight > PAGE && cur.children.length > 1) {
        const page = document.createElement('section');
        page.className = 'pdf-page'; page.innerHTML = '<div class="doc"></div>';
        wrap.appendChild(page); cur = page.firstChild; cur.appendChild(bk);
      }
    });
    const pages = $$('.pdf-page', wrap);
    pages.forEach((pg, i) => pg.insertAdjacentHTML('beforeend', `<div class="pdf-page__no mono">${i + 1} / ${pages.length}</div>`));
  }

  /* Locate q across text nodes inside el → Range */
  function textRange(el, q, from = 0, ci = false) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const texts = []; let all = '', t;
    while ((t = walker.nextNode())) { texts.push([t, all.length]); all += t.nodeValue; }
    const idx = ci ? all.toLowerCase().indexOf(q.toLowerCase(), from) : all.indexOf(q, from);
    if (idx < 0) return null;
    const loc = (pos, end) => {
      for (const [tn, off] of texts) {
        const len = tn.nodeValue.length;
        if (end ? pos > off && pos <= off + len : pos >= off && pos < off + len) return [tn, pos - off];
      }
      return null;
    };
    const s = loc(idx, false), e = loc(idx + q.length, true);
    if (!s || !e) return null;
    const r = document.createRange(); r.setStart(s[0], s[1]); r.setEnd(e[0], e[1]); r.idx = idx;
    return r;
  }
  function wrapRange(r, decorate) {
    const m = document.createElement('mark'); decorate(m);
    m.appendChild(r.extractContents()); r.insertNode(m);
    return m;
  }
  function wrapQuote(block, quote, decorate, selector = '[data-b]') {
    if (!quote) return null;
    const scopes = [];
    if (block != null) { const el = sheet.querySelector(`[data-b="${Number(block)}"]`); if (el) scopes.push(el); }
    scopes.push(...$$(selector, sheet));
    for (const sc of scopes) { const r = textRange(sc, quote); if (r) return wrapRange(r, decorate); }
    return null;
  }

  function unwrap(sel) {
    $$(sel, sheet).forEach((m) => { const parent = m.parentNode; m.replaceWith(...m.childNodes); parent.normalize(); });
  }
  /* Anchors: nodes with metadata.source.quote point at that passage; heading-derived nodes
     (no metadata) point at the heading with the same title. */
  function applyMarks() {
    if (!S.doc || S.mode === 'raw') return;
    if (isPdfView()) { applyPdfMarks(); return; }
    highlights.filter((h) => h.doc === S.doc.id).forEach((h) =>
      wrapQuote(h.block, h.quote, (m) => { m.className = 'hl'; m.dataset.hl = h.id; m.title = '本地高亮（仅保存在此浏览器）'; }));
    S.nodes.filter((n) => n.document_id === S.doc.id).forEach((n) => {
      const src = meta(n).source;
      const deco = (m) => {
        m.className = 'anchor' + (n.id === S.selected ? ' on' : '');
        m.dataset.node = n.id; m.title = '知识节点：' + n.title;
      };
      if (src?.selector?.type === 'text') {
        if (src.revision_id !== S.doc.current_revision_id) return;
        const at = src.selector.unit === 'utf16' ? src.selector.start : Array.from(S.text).slice(0, src.selector.start).join('').length;
        const block = $$('[data-b]', sheet).find(el => Number(el.dataset.start) <= at && Number(el.dataset.end) > at);
        if (!block) return;
        const raw = S.text.slice(Number(block.dataset.start), at), quote = collapse(src.quote);
        const ordinal = [...raw.matchAll(new RegExp(escRe(src.quote), 'g'))].length;
        let from = 0, range = null;
        for (let i = 0; i <= ordinal; i++) { range = textRange(block, quote, from); if (!range) break; from = range.idx + quote.length; }
        if (range) wrapRange(range, deco);
      }
      else if (src && src.quote && !src.fromNode) wrapQuote(src.block, src.quote, deco);
      else if (src && src.selector) return;
      else if (!src) wrapQuote(null, n.title, deco, 'h1[data-b], h2[data-b], h3[data-b], h4[data-b], h5[data-b], h6[data-b]');
    });
    if (S.find.open && S.find.q) runFind(false);
  }
  function refreshMarks() {
    unwrap('mark.find'); unwrap('mark.anchor'); unwrap('mark.hl');
    applyMarks();
    const h = $('.doc-head .mono span:last-child', sheet);
    if (h && S.doc) h.textContent = S.nodes.filter((n) => n.document_id === S.doc.id).length + ' 个节点';
    drawConnector();
  }
  const syncAnchorState = () => $$('mark.anchor', sheet).forEach((m) => m.classList.toggle('on', m.dataset.node === S.selected));
  function sectionOf(block) {
    let label = '';
    $$('h1[data-b], h2[data-b], h3[data-b], h4[data-b], h5[data-b], h6[data-b]', sheet).forEach((h) => { if (Number(h.dataset.b) <= block) label = collapse(h.textContent); });
    return label;
  }
  /* Derived section node that owns a block: the node titled like the nearest preceding heading. */
  function sectionNodeOf(block) {
    const label = sectionOf(block);
    if (!label || !S.doc) return null;
    return S.nodes.find((n) => n.document_id === S.doc.id && !meta(n).source && collapse(n.title) === label.replace(/^\d+(\.\d+)+\s*/, '').trim())
      || S.nodes.find((n) => n.document_id === S.doc.id && !meta(n).source && label.endsWith(collapse(n.title)));
  }
  function updateStatus() {
    const pages = $$('.pdf-page', sheet);
    let cur, total;
    if (isPdfView() && pdfv) { total = pdfv.view.numPages; cur = pdfv.view.currentPage(); }
    else if (pages.length) {
      total = pages.length; cur = 1;
      const top = scroller.getBoundingClientRect().top + 40;
      pages.forEach((pg, i) => { if (pg.getBoundingClientRect().top <= top) cur = i + 1; });
    } else {
      const h = scroller.clientHeight || 1;
      total = Math.max(1, Math.ceil(scroller.scrollHeight / h));
      cur = Math.min(total, Math.floor(scroller.scrollTop / h) + 1);
    }
    $('#st-page').textContent = S.doc ? `第 ${cur} 页，共 ${total} 页` : '未打开文档';
    $('#st-words').textContent = (S.doc ? wordCount(S.text) : 0).toLocaleString() + ' 字';
    let sec = '';
    const y = scroller.getBoundingClientRect().top + 60;
    $$('h1[data-b], h2[data-b], h3[data-b], h4[data-b], h5[data-b], h6[data-b]', sheet).forEach((h) => { if (h.getBoundingClientRect().top <= y) sec = collapse(h.textContent); });
    $('#st-section').textContent = sec ? '§ ' + clip(sec, 36) : (S.doc ? baseName(S.doc.title) : '');
    drawMinimap();
  }

  /* ================= PDF view =================
     format=pdf + view "PDF": the original from /source, rendered by assets/pdf-view.js
     (PDF.js through the instance-app reader adapter). Node anchors are pdf SourceRefs
     with canonical rects; marks, find hits and local highlights are overlay boxes. */
  let pdfv = null, pdfToken = 0, pdfBytes = null;
  const isPdfView = () => !!S.doc && S.doc.format === 'pdf' && S.mode === 'pdf';
  async function mountPdf(scrollTop) {
    const doc = S.doc, rev = doc.current_revision_id, key = `${doc.id}:${rev}`;
    const placeholder = () => $('#pdfv', sheet);
    const done = (el) => {
      placeholder()?.replaceWith(el);
      applyMarks(); paintInk();
      scroller.scrollTop = scrollTop;
      resetFollowPosition();
      updateStatus(); drawConnector();
    };
    if (pdfv?.key === key) { done(pdfv.el); return; }
    const token = ++pdfToken;
    try {
      if (pdfBytes?.rev !== rev) pdfBytes = { rev, data: await sync(api.bytes(`/api/documents/${p(doc.id)}/source?revision_id=${p(rev)}`)) };
      const { openPdfView } = await import(new URL('assets/pdf-view.js?v=obstacles-20261017-r2', document.baseURI).href);
      const el = document.createElement('div');
      el.className = 'pdfv';
      const width = Math.round(Math.min(760, sheet.clientWidth || 760));
      const view = await sync(openPdfView(el, {
        data: pdfBytes.data, ref: { document_id: doc.id, revision_id: rev }, root: scroller, width, pixelScale: docScale,
        onPage: () => { if (S.find.pdf) paintPdfFind(false).catch(() => {}); },
      }));
      if (token !== pdfToken || S.doc?.id !== doc.id || !isPdfView()) { view.destroy().catch(() => {}); return; }
      pdfv = { key, view, el };
      done(el);
    } catch (e) {
      if (token !== pdfToken) return;
      console.error(e);
      const ph = placeholder();
      if (ph) ph.innerHTML = `<p class="error pdfv-msg">无法打开 PDF：${esc(describe(e))}。可以切换到 MD / RAW 视图查看抽取的文本。</p>`;
    }
  }
  function applyPdfMarks() {
    if (!pdfv) return;
    const marks = [];
    highlights.filter((h) => h.doc === S.doc.id && h.pdf).forEach((h) =>
      marks.push({ cls: 'hl', data: { hl: h.id }, title: '本地高亮（仅保存在此浏览器）', pages: h.pdf }));
    S.nodes.filter((n) => n.document_id === S.doc.id && meta(n).source?.selector?.type === 'pdf').forEach((n) =>
      marks.push({ cls: 'anchor' + (n.id === S.selected ? ' on' : ''), data: { node: n.id }, title: '知识节点：' + n.title, pages: meta(n).source.selector.pages }));
    if (S.find.pdf && S.find.rects?.length) marks.push({ cls: 'find cur', pages: [{ page: S.find.hits[S.find.i].page, rects: S.find.rects }] });
    pdfv.view.setMarks(marks);
  }
  async function runPdfFind(jump) {
    if (!pdfv) return;
    const q = findInput.value.trim();
    S.find.q = q; S.find.pdf = true; S.find.rects = [];
    S.find.hits = q ? [...(await pdfv.view.search(q))] : [];
    if (S.find.q !== q) return;
    S.find.i = S.find.hits.length ? 0 : -1;
    await paintPdfFind(jump);
  }
  async function paintPdfFind(jump) {
    const { hits, i } = S.find;
    $('#find-count').textContent = hits.length ? `${i + 1} / ${hits.length}` : '0 / 0';
    const hit = hits[i];
    S.find.rects = hit && pdfv ? await pdfv.view.hitRects(hit) : [];
    if (S.find.hits[S.find.i] !== hit) return;
    applyPdfMarks();
    if (jump && hit) {
      const m = $('.pdfv-marks mark.find', sheet);
      if (m) m.scrollIntoView({ block: 'center' }); else pdfv.view.goTo(hit.page);
    }
  }
  async function paintPdfOutline() {
    const items = pdfv ? await pdfv.view.outline() : [];
    const flat = [];
    const walk = (list, d) => list.forEach((x) => { if (x.page) flat.push({ ...x, d }); walk(x.children || [], d + 1); });
    walk(items, 0);
    $('#outline-menu').innerHTML = `<div class="menu__section" style="padding:8px 12px"><span class="label">${flat.length ? '大纲' : '页面'}</span></div>`
      + (flat.length
        ? flat.map((x) => `<button class="menu__item ${x.d ? 'lvl3' : ''}" role="menuitem" data-page="${x.page}">${esc(clip(x.title, 40))}<span class="mono muted">${x.page}</span></button>`)
        : Array.from({ length: pdfv?.view.numPages || 0 }, (_, k) => `<button class="menu__item" role="menuitem" data-page="${k + 1}">第 ${k + 1} 页</button>`)).join('');
  }

  /* ================= Ink (free drawing) =================
     Browser-local like highlights (localStorage per project + document), never sent
     to the server. md strokes hang on a block (doc-width units, so they follow
     reflow and zoom); PDF strokes are canonical page fractions. */
  const PEN_ICON = { pen: 'pen', marker: 'brush', highlighter: 'highlighter', eraser: 'eraser' };
  let ink = { key: null, strokes: [] }, inkUndo = [];
  const inkKey = () => `tlm.ink.${S.project?.id}.${S.doc?.id}`;
  function inkStrokes() {
    const k = inkKey();
    if (ink.key !== k) { ink = { key: k, strokes: readJSON(k, []) }; inkUndo = []; }
    return ink.strokes;
  }
  function saveInk() {
    try { localStorage.setItem(ink.key, JSON.stringify(ink.strokes)); }
    catch { toast('浏览器存储空间不足，绘图没有保存'); }
  }
  const r4 = (v) => Math.round(v * 1e4) / 1e4;
  function inkSurfaces() {
    if (!S.doc || S.mode === 'raw') return [];
    if (isPdfView()) return pdfv ? pdfv.view.pages.map((pg) => ({ el: pg.el, kind: 'pdf', n: pg.n })) : [];
    return $$('.doc', sheet).map((el) => ({ el, kind: 'md' }));
  }
  function inkOpts(sf) {
    const W = () => sf.el.offsetWidth || 1, H = () => sf.el.offsetHeight || 1;
    const common = {
      unit: W,
      tool: () => (S.tool === 'draw' ? { pen: prefs.pen, color: prefs.inkColor } : null),
      onAdd: (k) => { inkStrokes().push(k); inkUndo.push({ op: 'add', k }); saveInk(); paintInk(); },
      onErase: (k) => {
        const list = inkStrokes(), i = list.indexOf(k);
        if (i < 0) return;
        list.splice(i, 1); inkUndo.push({ op: 'erase', k, i }); saveInk(); paintInk();
      },
    };
    if (sf.kind === 'pdf') {
      return { ...common,
        strokes: () => inkStrokes().filter((k) => k.space === 'pdf' && k.page === sf.n),
        frameOf: (k) => (k.space === 'pdf' && k.page === sf.n ? ([a, b]) => { const [x, y] = pdfv.view.toPagePoint(sf.n, a, b); return [x * W(), y * H()]; } : null),
        begin: () => ({ anchor: { space: 'pdf', page: sf.n }, toStore: (x, y) => pdfv.view.toCanonicalPoint(sf.n, x / W(), y / H()) }),
      };
    }
    const blocks = () => $$(':scope > [data-b]', sf.el);
    return { ...common,
      strokes: () => inkStrokes().filter((k) => k.space === 'md'),
      frameOf: (k) => {
        const b = k.space === 'md' && sf.el.querySelector(`:scope > [data-b="${Number(k.b)}"]`);
        return b ? ([a, c]) => [b.offsetLeft + a * W(), b.offsetTop + c * W()] : null;
      },
      begin: (x, y) => {
        const list = blocks();
        if (!list.length) return null;
        let b = list[0];
        list.forEach((el) => { if (el.offsetTop <= y) b = el; });
        const L = b.offsetLeft, T = b.offsetTop, w = W();
        return { anchor: { space: 'md', b: Number(b.dataset.b) }, toStore: (px, py) => [r4((px - L) / w), r4((py - T) / w)] };
      },
    };
  }
  function paintInk() {
    inkSurfaces().forEach((sf) => {
      if (!sf.el.__ink) sf.el.__ink = TLMInk.attach(sf.el, inkOpts(sf));
      sf.el.__ink.paint();
    });
  }
  function inkUndoLast() {
    const a = inkUndo.pop();
    if (!a) { toast('没有可撤销的笔画'); return; }
    const list = inkStrokes();
    if (a.op === 'add') { const i = list.indexOf(a.k); if (i >= 0) list.splice(i, 1); }
    else list.splice(Math.min(a.i, list.length), 0, a.k);
    saveInk(); paintInk();
  }
  async function inkClear() {
    const list = inkStrokes();
    if (!list.length) { toast('这个文档还没有绘图'); return; }
    if (!(await confirmModal('清除绘图', `删除这个文档上的 ${list.length} 笔绘图？绘图只保存在本浏览器。`, '清除'))) return;
    const old = list.splice(0);
    inkUndo = []; saveInk(); paintInk();
    toast('已清除绘图', { ms: 5000, action: { label: '撤销', run: () => { inkStrokes().push(...old); saveInk(); paintInk(); } } });
  }
  function paintDrawbar() {
    $('#draw-pens').innerHTML = Object.entries(TLMInk.PENS).map(([k, v]) =>
      `<button type="button" class="icon-btn" role="radio" aria-checked="${prefs.pen === k}" data-pen="${k}" title="${v.label}" aria-label="${v.label}">${icon(PEN_ICON[k])}</button>`).join('');
    const cur = String(prefs.inkColor).toLowerCase(), known = TLMInk.COLORS.some(([c]) => c.toLowerCase() === cur);
    $('#draw-colors').innerHTML = TLMInk.COLORS.map(([c, l]) =>
      `<button type="button" class="swatch" role="radio" aria-checked="${c.toLowerCase() === cur}" data-color="${c}" title="${l}" aria-label="${l}" style="--c:${c}"></button>`).join('');
    const custom = $('#draw-custom');
    custom.value = prefs.inkColor;
    custom.closest('.swatch').setAttribute('aria-checked', String(!known));
    custom.closest('.swatch').style.setProperty('--c', prefs.inkColor);
    $('#reader').classList.toggle('ink-erase', prefs.pen === 'eraser');
    $('#draw-colors').classList.toggle('off', prefs.pen === 'eraser');
  }
  $('#drawbar').addEventListener('click', (e) => {
    const pen = e.target.closest('[data-pen]'), color = e.target.closest('[data-color]');
    if (pen) setPref('pen', pen.dataset.pen);
    if (color) { setPref('inkColor', color.dataset.color); if (prefs.pen === 'eraser') setPref('pen', 'pen'); }
    if (pen || color) paintDrawbar();
  });
  $('#draw-custom').addEventListener('input', (e) => { setPref('inkColor', e.target.value); if (prefs.pen === 'eraser') setPref('pen', 'pen'); paintDrawbar(); });

  /* ================= Selection → ask popover ================= */
  const askEl = $('#ask');
  const blockOf = (n) => (n && (n.nodeType === 1 ? n : n.parentElement))?.closest('[data-b]');
  function readerSelection() {
    if (!S.doc || S.mode === 'raw' || S.tool === 'draw') return null;
    if (isPdfView()) {
      const s = pdfv?.view.selection();
      return s && { origin: { kind: 'pdf', quote: s.draft.quote, ref: s.draft, label: `第 ${s.page} 页` }, rect: s.rect };
    }
    const sel = getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    const startB = blockOf(range.startContainer);
    if (!startB || !sheet.contains(startB)) return null;
    let r = range;
    if (blockOf(range.endContainer) !== startB) { r = range.cloneRange(); r.setEnd(startB, startB.childNodes.length); }
    let quote = r.toString().trim();
    if (quote.length < 1) return null;
    if (quote.length > 6000) return null;
    const block = Number(startB.dataset.b);
    const before = r.cloneRange(); before.selectNodeContents(startB); before.setEnd(r.startContainer, r.startOffset);
    const after = r.cloneRange(); after.selectNodeContents(startB); after.setStart(r.endContainer, r.endOffset);
    return { origin: { kind: 'doc', block, quote, prefix: before.toString().slice(-32), suffix: after.toString().slice(0, 32),
      sourceStart: Number(startB.dataset.start), sourceEnd: Number(startB.dataset.end), before: before.toString(), blockText: startB.textContent,
      documentId: S.doc.id, revisionId: S.doc.current_revision_id, label: sectionOf(block) }, rect: range.getBoundingClientRect() };
  }
  const countHits = (q) => countIn(sheet.textContent, q);
  function countIn(text, q) {
    const hay = collapse(text).toLowerCase(), needle = collapse(q).toLowerCase();
    let n = 0, i = 0;
    while (needle && (i = hay.indexOf(needle, i)) >= 0) { n++; i += needle.length; }
    return n;
  }
  function openAsk(origin, rect) {
    hideSelBar();
    S.ask = origin;
    askEl.hidden = false;
    askEl.classList.toggle('from-node', origin.kind === 'node');
    $('#ask-quote').textContent = '“' + clip(collapse(origin.quote), 140) + '”';
    $('#ask-input').value = '';
    $('#ask-object').value = origin.kind === 'visual' ? 'chart' : 'auto';
    const canExplain = S.kinds.has('explanation');
    $('#ask-form').hidden = !canExplain;
    if (origin.kind === 'pdf') {
      $('#ask-hits').textContent = countIn(S.text, origin.quote);
      $('#ask-src').textContent = clip(origin.label, 40);
    } else if (origin.kind === 'doc') {
      $('#ask-hits').textContent = countHits(origin.quote);
      $('#ask-src').textContent = clip(origin.label ? '§ ' + origin.label : S.doc.title, 40);
    } else {
      $('#ask-src').textContent = clip('追问 · ' + (nodeOf(origin.nodeId)?.title || ''), 40);
    }
    const w = askEl.offsetWidth, h = askEl.offsetHeight;
    const x = Math.min(Math.max(8, rect.left), innerWidth - w - 8);
    let y = rect.bottom + 8;
    if (y + h > innerHeight - 8) y = Math.max(8, rect.top - h - 8);
    askEl.style.left = x + 'px'; askEl.style.top = y + 'px';
    (canExplain ? $('#ask-input') : $('[data-act="ask-extract"]', askEl)).focus({ preventScroll: true });
  }
  function hideAsk() { askEl.hidden = true; S.ask = null; }

  $('#ask-form').addEventListener('submit', action(async (e) => {
    e.preventDefault();
    if (!S.ask) return;
    const origin = { ...S.ask, branchType: $('#ask-object').value }, q = $('#ask-input').value.trim();
    hideAsk(); getSelection().removeAllRanges();
    const node = await createNodeFrom(origin);
    await explain(node, q);
  }));
  askEl.addEventListener('click', action(async (e) => {
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (!act || !S.ask) return;
    const origin = { ...S.ask, branchType: $('#ask-object').value };
    hideAsk();
    if (act === 'ask-extract') { const n = await createNodeFrom(origin); toast(`已提取为节点「${clip(n.title, 16)}」`); }
    if (act === 'ask-highlight') addHighlight(origin);
    if (act === 'ask-copy') { await copyText(origin.quote); toast('已复制选中内容'); }
    if (act === 'ask-find') openFind(origin.quote);
    getSelection().removeAllRanges();
  }));
  scroller.addEventListener('mouseup', () => setTimeout(() => {
    if (S.modal || S.tool === 'draw') return;
    const s = readerSelection();
    if (!s) return;
    if (S.tool === 'highlight') { addHighlight(s.origin); getSelection().removeAllRanges(); return; }
    openAsk(s.origin, s.rect);
  }, 0));
  sheet.addEventListener('click', (e) => {
    if (!getSelection().isCollapsed || S.tool === 'draw') return;
    const pv = isPdfView() && pdfv?.view;
    const anchor = pv ? pv.markAt(e.clientX, e.clientY, 'mark.anchor') : e.target.closest('mark.anchor');
    const hl = pv ? pv.markAt(e.clientX, e.clientY, 'mark.hl') : e.target.closest('mark.hl');
    if (S.tool === 'highlight' && hl) { removeHighlight(hl.dataset.hl); return; }
    if (anchor) selectNode(anchor.dataset.node, { center: true });
  });
  function addHighlight(o) {
    if (highlights.some((h) => h.doc === S.doc.id && h.quote === o.quote)) { toast('这段已经高亮'); return; }
    highlights.push({ id: 'h_' + Math.random().toString(36).slice(2, 9), doc: S.doc.id, block: o.block, quote: o.quote,
      ...(o.kind === 'pdf' ? { pdf: o.ref.selector.pages } : {}) });
    saveHighlights(); refreshMarks();
  }
  function removeHighlight(id) {
    const i = highlights.findIndex((h) => h.id === id);
    if (i < 0) return;
    const [h] = highlights.splice(i, 1);
    saveHighlights(); refreshMarks();
    toast('已移除高亮', { ms: 4000, action: { label: '撤销', run: () => { highlights.splice(i, 0, h); saveHighlights(); refreshMarks(); } } });
  }

  /* ================= Find in document ================= */
  const findInput = $('#find-input');
  let findT;
  function openFind(q) {
    if (!S.doc) return;
    if (S.mode === 'raw') { S.mode = 'md'; savePrefs(); renderReader(true); }
    S.find.open = true;
    $('#findbar').hidden = false;
    if (q != null) findInput.value = collapse(q).slice(0, 120);
    runFind(true);
    findInput.focus(); findInput.select();
  }
  function closeFind() {
    S.find = { open: false, q: '', hits: [], i: -1 };
    $('#findbar').hidden = true;
    unwrap('mark.find');
    if (isPdfView()) applyPdfMarks();
  }
  function runFind(jump) {
    if (isPdfView()) { runPdfFind(jump).catch(fail); return; }
    unwrap('mark.find');
    const q = findInput.value.trim();
    S.find.q = q; S.find.hits = []; S.find.pdf = false;
    if (q) {
      $$('[data-b]', sheet).forEach((blk) => {
        const starts = []; let r, from = 0;
        while ((r = textRange(blk, q, from, true))) { starts.push(r.idx); from = r.idx + q.length; }
        const marks = [];
        for (let k = starts.length - 1; k >= 0; k--) {
          const rr = textRange(blk, q, starts[k], true);
          if (rr) marks.unshift(wrapRange(rr, (m) => { m.className = 'find'; }));
        }
        S.find.hits.push(...marks);
      });
    }
    S.find.i = S.find.hits.length ? 0 : -1;
    paintFind(jump);
  }
  function paintFind(jump) {
    if (S.find.pdf) { paintPdfFind(jump).catch(fail); return; }
    const { hits, i } = S.find;
    hits.forEach((m, k) => m.classList.toggle('cur', k === i));
    $('#find-count').textContent = hits.length ? `${i + 1} / ${hits.length}` : '0 / 0';
    if (jump && hits[i]) hits[i].scrollIntoView({ block: 'center' });
  }
  function stepFind(dir) {
    const n = S.find.hits.length;
    if (!n) return;
    S.find.i = (S.find.i + dir + n) % n;
    paintFind(true);
  }
  findInput.addEventListener('input', () => { clearTimeout(findT); findT = setTimeout(() => runFind(true), 120); });
  findInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1); }
    if (e.key === 'Escape') { e.stopPropagation(); closeFind(); }
  });

  /* ================= Canvas: view + layout ================= */
  const surface = $('#surface'), world = $('#world'), nodesEl = $('#nodes');
  const ZMIN = 0.35, ZMAX = 1.6, COL = 372, GAP = 28;
  function applyView() {
    world.style.transform = `translate(${S.view.x}px, ${S.view.y}px) scale(${S.view.z})`;
    $('#zoom-v').textContent = Math.round(S.view.z * 100) + '%';
  }
  function zoomAt(cx, cy, z) {
    const v = S.view;
    z = Math.min(ZMAX, Math.max(ZMIN, z));
    const wx = (cx - v.x) / v.z, wy = (cy - v.y) / v.z;
    v.x = cx - wx * z; v.y = cy - wy * z; v.z = z;
    applyView(); drawConnector(); savePrefs();
  }
  const surfaceCenter = () => [surface.clientWidth / 2, surface.clientHeight / 2];
  const posOf = (n) => meta(n).position || S.layout[n.id] || { x: 40, y: 40 };

  /* Tree order: roots by document/order_index, children depth-first. */
  function treeOrder() {
    const order = [], seen = new Set();
    const byDoc = (a, b) => (a.document_id === b.document_id ? a.order_index - b.order_index : a.document_id < b.document_id ? -1 : 1);
    const visit = (n) => { if (seen.has(n.id)) return; seen.add(n.id); order.push(n); childrenOf(n.id).filter(inDoc).sort(byDoc).forEach(visit); };
    const pool = docNodes();
    pool.filter((n) => !n.parent_id || !inDoc(nodeOf(n.parent_id))).sort(byDoc).forEach(visit);
    pool.forEach((n) => { if (!seen.has(n.id)) order.push(n); });
    return order;
  }
  /* Explicit layout ranks the graph; ordinary refresh preserves manually saved positions. */
  function computeLayout(all) {
    if (all) {
      const pool = docNodes();
      const sizes = Object.fromEntries(pool.map(n => [n.id, {
        width: elOf(n)?.offsetWidth || 332, height: elOf(n)?.offsetHeight || 150,
      }]));
      return TutorGraphLayout.layout(pool, S.edges, sizes);
    }
    const cols = {}, out = {};
    treeOrder().forEach((n) => {
      if (!all && meta(n).position) return;
      const d = Math.min(depthOf(n), 6);
      const c = (cols[d] = cols[d] || { y: 40 });
      out[n.id] = { x: 40 + d * COL, y: c.y };
      c.y += (elOf(n)?.offsetHeight || 150) + GAP;
    });
    return out;
  }
  function fitView() {
    const pool = docNodes();
    if (!pool.length) { S.view = { x: 16, y: 8, z: 0.85 }; applyView(); return; }
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    pool.forEach((n) => {
      const el = elOf(n), q = posOf(n);
      x1 = Math.min(x1, q.x); y1 = Math.min(y1, q.y);
      x2 = Math.max(x2, q.x + (el?.offsetWidth || 248)); y2 = Math.max(y2, q.y + (el?.offsetHeight || 150));
    });
    const pad = 40, sw = surface.clientWidth, sh = surface.clientHeight;
    const z = Math.min(1.1, Math.max(ZMIN, Math.min((sw - pad * 2) / (x2 - x1), (sh - pad * 2) / (y2 - y1))));
    S.view = { x: (sw - (x2 - x1) * z) / 2 - x1 * z, y: pad - y1 * z, z };
    applyView(); drawConnector(); savePrefs();
  }
  function ensureVisible(n) {
    const v = S.view, sw = surface.clientWidth, sh = surface.clientHeight, q = posOf(n);
    if (innerWidth <= 700) {
      const el = elOf(n), width = el?.offsetWidth || 280, height = el?.offsetHeight || 140;
      v.z = Math.min(v.z, 1, (sw - 24) / width, (sh - 24) / height);
      v.x = (sw - width * v.z) / 2 - q.x * v.z;
      v.y = 12 - q.y * v.z;
      applyView(); savePrefs(); return;
    }
    const sx = q.x * v.z + v.x, sy = q.y * v.z + v.y;
    if (sx < 16 || sx + 340 * v.z > sw - 16 || sy < 16 || sy + 140 * v.z > sh - 16) {
      v.x = Math.min(40, sw - 360 * v.z) - q.x * v.z;
      v.y = sh * 0.15 - q.y * v.z;
      applyView(); savePrefs();
    }
  }
  async function persistPosition(n) {
    const q = posOf(n);
    const updated = await api.patch(`/api/nodes/${p(n.id)}`, { metadata: { ...meta(n), position: { x: Math.round(q.x), y: Math.round(q.y) } } });
    Object.assign(n, updated);
  }
  let layoutBusy = false;
  async function autoLayout({ silent = false } = {}) {
    if (layoutBusy) throw new Error('节点正在排列，请稍候');
    const pool = docNodes();
    if (!pool.length) return;
    const projectId = S.project.id, documentId = S.doc.id;
    const current = () => S.project?.id === projectId && S.doc?.id === documentId;
    const button = $('[data-act="auto-layout"]');
    layoutBusy = true;
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    try {
      const target = computeLayout(true);
      const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      world.classList.toggle('animate', !reduced);
      // Flush start geometry before changing positions so even a fresh graph animates.
      void world.offsetWidth;
      pool.forEach((n) => {
        n.metadata = { ...meta(n), position: target[n.id] };
        const el = elOf(n); if (el) { el.style.left = target[n.id].x + 'px'; el.style.top = target[n.id].y + 'px'; }
      });
      await new Promise(resolve => {
        const start = performance.now();
        function frame(now) {
          if (!current()) { resolve(); return; }
          drawEdges(); drawConnector();
          if (reduced || now - start >= 420) { resolve(); return; }
          requestAnimationFrame(frame);
        }
        requestAnimationFrame(frame);
      });
      if (!current()) return;
      world.classList.remove('animate');
      fitView();
      // Persist only this document, with bounded parallelism and no unrelated writes.
      const queue = [...pool];
      await sync(Promise.all(Array.from({ length: 4 }, async () => {
        while (queue.length && current()) await persistPosition(queue.shift());
      })));
      if (!silent && current()) toast('已从根到叶排列并保存布局');
    } finally {
      world.classList.remove('animate'); layoutBusy = false;
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
    }
  }

  /* ================= Edges =================
     Solid = tree (parent_id), dashed + label = explicit relation.
     Ports: when a card has several links on one side, they leave / arrive
     top-to-bottom (ordered by the other end's height) instead of one point. */
  function edgeList() {
    const list = [], tree = new Set();
    docNodes().forEach((n) => {
      const parent = n.parent_id && nodeOf(n.parent_id);
      if (inDoc(parent)) { tree.add(parent.id + '>' + n.id); list.push({ a: parent, b: n, rel: null }); }
    });
    S.edges.forEach((e) => {
      if (e.relation === 'parent_of' && tree.has(e.source_node_id + '>' + e.target_node_id)) return;
      const a = nodeOf(e.source_node_id), b = nodeOf(e.target_node_id);
      if (inDoc(a) && inDoc(b)) list.push({ a, b, rel: e.relation });
    });
    return list;
  }
  function drawEdges() {
    const geo = new Map();
    const box = (n) => {
      if (!geo.has(n.id)) { const el = elOf(n); geo.set(n.id, el ? { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight } : null); }
      return geo.get(n.id);
    };
    const outs = {}, ins = {}, list = [];
    edgeList().forEach((e) => {
      const A = box(e.a), B = box(e.b);
      if (!A || !B) return;
      e.A = A; e.B = B;
      e.dir = B.x >= A.x + A.w ? 1 : B.x + B.w <= A.x ? -1 : 0;
      e.vertical = B.y >= A.y + A.h ? 1 : B.y + B.h <= A.y ? -1 : 0;
      (outs[e.a.id + (e.vertical ? e.vertical === 1 ? 'B' : 'T' : e.dir === -1 ? 'L' : 'R')] ||= []).push(e);
      (ins[e.b.id + (e.vertical ? e.vertical === 1 ? 'T' : 'B' : e.dir === 1 ? 'L' : 'R')] ||= []).push(e);
      list.push(e);
    });
    const spread = (arr, end) => {
      const R = (e) => (end === 'y1' ? e.A : e.B), O = (e) => (end === 'y1' ? e.B : e.A);
      if (arr[0].vertical) {
        arr.sort((a, b) => O(a).x + O(a).w / 2 - O(b).x - O(b).w / 2);
        arr.forEach((e, i) => {
          const r = R(e), span = Math.min(r.w - 32, (arr.length - 1) * 20);
          const x = r.x + r.w / 2 + (arr.length > 1 ? i * span / (arr.length - 1) - span / 2 : 0);
          e[end === 'y1' ? 'x1' : 'x2'] = x;
          const bottom = end === 'y1' ? e.vertical > 0 : e.vertical < 0;
          e[end] = r.y + (bottom ? r.h : 0);
        });
        return;
      }
      arr.sort((p, q) => (O(p).y + O(p).h / 2) - (O(q).y + O(q).h / 2));
      arr.forEach((e, i) => {
        const r = R(e), top = 17, span = Math.min(Math.max(0, r.h - 34), (arr.length - 1) * 16);
        e[end] = r.y + top + (arr.length > 1 ? (i * span) / (arr.length - 1) : 0);
      });
    };
    Object.values(outs).forEach((arr) => spread(arr, 'y1'));
    Object.values(ins).forEach((arr) => spread(arr, 'y2'));
    const parts = list.map((e) => {
      const { A, B, dir } = e;
      const x1 = e.vertical ? e.x1 : dir === -1 ? A.x : A.x + A.w;
      const x2 = e.vertical ? e.x2 : dir === 1 ? B.x : B.x + B.w;
      const dx = dir === 0 ? 48 : Math.max(36, Math.abs(x2 - x1) / 2) * dir;
      const c2 = dir === 0 ? x2 + 48 : x2 - dx;
      const dy = Math.max(30, Math.abs(e.y2 - e.y1) / 2) * e.vertical;
      const d = e.vertical
        ? `M${x1} ${e.y1} C${x1} ${e.y1 + dy}, ${x2} ${e.y2 - dy}, ${x2} ${e.y2}`
        : `M${x1} ${e.y1} C${x1 + dx} ${e.y1}, ${c2} ${e.y2}, ${x2} ${e.y2}`;
      const on = S.selected && (e.a.id === S.selected || e.b.id === S.selected);
      if (!e.rel) return `<path class="${on ? 'on' : ''}" marker-end="url(#arrow)" d="${d}"/>`;
      const label = esc(REL[e.rel] || e.rel);
      return `<path class="rel ${on ? 'on' : ''}" marker-end="url(#arrow)" d="${d}"><title>${label}</title></path>
        <text class="rel-label" x="${(x1 + x2) / 2 + (!e.vertical && dir === 0 ? 40 : 0)}" y="${(e.y1 + e.y2) / 2 - 4}" text-anchor="middle">${label}</text>`;
    });
    $('#edge-g').innerHTML = parts.join('');
  }

  /* ================= Source links (reader ↔ canvas) =================
     Nodes created from the source keep a visible link to their passage; the
     selected node's link is drawn darker. Off-screen nodes clamp to the edge. */
  const connG = $('#connector-g');
  function drawConnector() {
    if (!connG) return;
    if (!S.doc || app.classList.contains('focus') || app.classList.contains('no-canvas') || innerWidth <= 700) { connG.innerHTML = ''; return; }
    const rr = scroller.getBoundingClientRect(), cr = surface.getBoundingClientRect();
    const right = cr.right - 18, parts = [], seen = new Set();
    $$('mark.anchor', sheet).forEach((mark) => {
      const n = nodeOf(mark.dataset.node);
      if (!n || seen.has(n.id)) return;
      const on = n.id === S.selected;
      if (!on && !(prefs.links && meta(n).source)) return;
      const rects = mark.getClientRects(), last = rects[rects.length - 1];
      if (!last || last.bottom < rr.top + 4 || last.top > rr.bottom - 4) return;
      const el = elOf(n);
      if (!el) return;
      seen.add(n.id);
      const nr = el.getBoundingClientRect();
      let x2 = nr.left - 2, y2 = nr.top + 17 * S.view.z;
      const off = x2 < cr.left || x2 > right || y2 < cr.top || y2 > cr.bottom;
      x2 = Math.min(right, Math.max(cr.left + 4, x2)); y2 = Math.min(cr.bottom - 6, Math.max(cr.top + 6, y2));
      const x1 = Math.min(last.right, rr.right - 8) + 3, y1 = last.top + last.height / 2;
      const dx = Math.max(48, (x2 - x1) * 0.45);
      const cls = `${on ? 'on' : ''} ${off ? 'off' : ''}`;
      parts.push(`<path class="${cls}" d="M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}"/>
        <circle class="${on ? 'on' : ''}" cx="${x1}" cy="${y1}" r="2.5"/>${off ? '' : `<circle class="${on ? 'on' : ''}" cx="${x2}" cy="${y2}" r="2.5"/>`}`);
    });
    connG.innerHTML = parts.join('');
  }

  /* ================= Overview ruler =================
     Like an editor's scrollbar markers: one tick per node anchored in the open
     document, at its position in the text; tick weight = explanation depth. */
  const mmEl = $('#minimap'), mmMarks = $('#minimap-marks'), mmView = $('#minimap-view');
  const explChildren = (n) => S.nodes.filter((c) => meta(c).source?.fromNode === n.id);
  function explDepth(n, seen = new Set()) {
    if (!n || seen.has(n.id)) return 0;
    seen.add(n.id);
    const own = (S.history[n.id] || []).length ? 1 : 0;
    const kids = S.nodes.filter((c) => c.parent_id === n.id || meta(c).source?.fromNode === n.id);
    return own + (kids.length ? Math.max(...kids.map((k) => explDepth(k, seen))) : 0);
  }
  function anchorTrack() {
    const sr = scroller.getBoundingClientRect(), seen = new Set(), out = [];
    $$('mark.anchor', sheet).forEach((m) => {
      const n = nodeOf(m.dataset.node);
      if (!n || seen.has(n.id)) return;
      seen.add(n.id);
      out.push({ y: m.getBoundingClientRect().top - sr.top + scroller.scrollTop, n, m });
    });
    return out.sort((a, b) => a.y - b.y);
  }
  function drawMinimap() {
    if (!mmEl) return;
    if (!prefs.minimap || !S.doc || S.mode === 'raw') { mmMarks.innerHTML = ''; mmView.style.display = 'none'; return; }
    const H = Math.max(1, scroller.scrollHeight), h = Math.max(1, mmEl.clientHeight - 8);
    mmView.style.display = '';
    mmView.style.top = 4 + (scroller.scrollTop / H) * h + 'px';
    mmView.style.height = Math.max(8, (scroller.clientHeight / H) * h) + 'px';
    mmMarks.innerHTML = anchorTrack().map(({ y, n }) => {
      const d = Math.min(3, explDepth(n));
      return `<button class="mm d${d} ${n.id === S.selected ? 'on' : ''}" style="top:${(y / H) * h}px" data-mm="${esc(n.id)}" title="${esc(clip(n.title, 40))} · 解释深度 ${d}" aria-label="${esc(n.title)}，解释深度 ${d}"></button>`;
    }).join('');
  }
  mmEl?.addEventListener('pointerdown', (e) => e.stopPropagation());
  mmEl?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-mm]');
    if (b) { selectNode(b.dataset.mm, { center: true }); revealSource(nodeOf(b.dataset.mm)); return; }
    const r = mmEl.getBoundingClientRect();
    scroller.scrollTop = ((e.clientY - r.top - 4) / (r.height - 8)) * scroller.scrollHeight - scroller.clientHeight / 2;
  });
  mmEl?.addEventListener('wheel', (e) => { e.preventDefault(); e.stopPropagation(); scroller.scrollTop += e.deltaY; }, { passive: false });

  /* Scroll in screen pixels so every card keeps its offset from the source,
     including beyond either edge. Manual canvas movement establishes a new offset. */
  let followPosition = { documentId: null, top: 0 };
  function resetFollowPosition() {
    followPosition = { documentId: S.doc?.id, top: scroller.scrollTop };
  }
  const touchCanvas = resetFollowPosition;
  function followScroll() {
    const previous = followPosition;
    resetFollowPosition();
    if (!prefs.follow || drag || app.classList.contains('no-canvas') || !S.doc) return;
    if (previous.documentId !== S.doc.id) return;
    S.view.y -= scroller.scrollTop - previous.top;
    applyView(); drawConnector(); savePrefs();
  }

  /* Pointer: drag node / pan surface / click-select */
  let drag = null;
  const NO_DRAG = 'button, input, textarea, select, form, a, details, .answer, .bubble, .chat__log, .xpl__body, .minimap';
  surface.addEventListener('pointerdown', (e) => {
    if (layoutBusy) return;
    if (e.button !== 0 || e.target.closest('.inspector')) return;
    touchCanvas();
    const nEl = e.target.closest('.gnode');
    if (nEl && e.target.closest(NO_DRAG)) return;
    if (!nEl && e.target.closest('.minimap')) return;
    const n = nEl && nodeOf(nEl.dataset.id);
    const q = n ? posOf(n) : null;
    drag = { n, el: nEl, sx: e.clientX, sy: e.clientY, ox: n ? q.x : S.view.x, oy: n ? q.y : S.view.y, moved: false };
    surface.setPointerCapture(e.pointerId);
    if (!n) surface.classList.add('panning');
  });
  surface.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    drag.moved = true;
    if (drag.n) {
      drag.el.classList.add('dragging');
      const x = Math.round(drag.ox + dx / S.view.z), y = Math.round(drag.oy + dy / S.view.z);
      drag.n.metadata = { ...meta(drag.n), position: { x, y } };
      drag.el.style.left = x + 'px'; drag.el.style.top = y + 'px';
      drawEdges();
    } else {
      S.view.x = drag.ox + dx; S.view.y = drag.oy + dy;
      applyView();
    }
    drawConnector();
  });
  const endDrag = () => {
    if (!drag) return;
    const d = drag; drag = null;
    touchCanvas();
    surface.classList.remove('panning');
    if (d.el) d.el.classList.remove('dragging');
    if (d.moved) {
      if (d.n) sync(persistPosition(d.n)).catch(fail); else savePrefs();
      return;
    }
    if (d.n) { if (S.selected !== d.n.id) selectNode(d.n.id); }
    else if (S.selected) selectNode(null);
  };
  surface.addEventListener('pointerup', endDrag);
  surface.addEventListener('pointercancel', endDrag);
  surface.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && e.target.closest('.gnode__scroll, .chat__log, .xpl__body, .inspector, textarea, .minimap')) return;
    e.preventDefault();
    touchCanvas();
    if (e.ctrlKey || e.metaKey) {
      const r = surface.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, S.view.z * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
    } else {
      S.view.x -= e.deltaX; S.view.y -= e.deltaY;
      applyView(); drawConnector(); savePrefs();
    }
  }, { passive: false });
  nodesEl.addEventListener('keydown', (e) => {
    const nEl = e.target.closest('.gnode:not(.sel)');
    if (nEl && e.target === nEl && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectNode(nEl.dataset.id); }
  });

  /* ================= Nodes ================= */
  function srcLabel(n) {
    const src = meta(n).source;
    if (src?.fromNode) { const parent = nodeOf(src.fromNode); return '追问 · ' + (parent ? parent.title : '已删除节点'); }
    if (src?.label) return '§ ' + src.label;
    if (src?.selector?.type === 'pdf') return `第 ${src.selector.pages?.[0]?.page || '?'} 页`;
    return docOf(n.document_id)?.title || '';
  }
  const plainMd = (t) => collapse(String(t || '').replace(/^#+\s+.*$/gm, '').replace(/[*_`>#$]/g, ''));
  const shownQuestion = (q) => String(q || '').replace(/\n?（[^（）]*）\s*$/, '').trim() || '请解释这个知识点';
  function runLine(run) {
    const label = { queued: '排队中', running: '生成中' }[run.state] || run.state;
    return `<div class="gen" role="status">${label}${run.progress ? ` · ${run.progress}%` : ''}
      <button class="btn btn--sm" data-act="node-cancel" style="margin-left:auto">${icon('stop')}取消</button></div>`;
  }

  /* Explanation pages vs chat.
     metadata.pages = explanation ids in page order, metadata.page = page shown.
     Every other explanation of the node is a chat turn. Legacy nodes without
     metadata.pages use their oldest explanation as page 1. */
  const byTime = (a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0);
  function pagesOf(n) {
    const hist = S.history[n.id] || [];
    const ids = meta(n).pages;
    if (Array.isArray(ids)) return ids.map((id) => hist.find((h) => h.id === id)).filter(Boolean);
    const oldest = [...hist].sort(byTime)[0];
    return oldest ? [oldest] : [];
  }
  function chatOf(n) {
    const pg = new Set(pagesOf(n).map((h) => h.id));
    return (S.history[n.id] || []).filter((h) => !pg.has(h.id)).sort(byTime);
  }
  function pageIndex(n) {
    const len = pagesOf(n).length, i = meta(n).page;
    return len ? Math.min(len - 1, Math.max(0, Number.isInteger(i) ? i : len - 1)) : -1;
  }
  function latestExplanation(n) {
    // History comes from the server in created_at DESC, rowid DESC order.
    // Stable sorting preserves that explicit record order for equal timestamps.
    return [...(S.history[n.id] || [])].filter((e) => typeof e.answer === 'string' && e.answer.trim())
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())[0] || null;
  }
  function retellingVersions(n) {
    const latest = latestExplanation(n);
    const sourceId = latest?.learning?.retelling?.source_explanation_id;
    const original = sourceId && (S.history[n.id] || []).find((exp) => exp.id === sourceId);
    return original ? { original, current: latest } : null;
  }
  function currentPage(n) {
    const versions = retellingVersions(n), view = meta(n).retelling_view;
    if (versions && view?.explanation_id === versions.current.id && view.original) return versions.original;
    return latestExplanation(n);
  }
  const marksOf = (n, expId) => (meta(n).marks || []).filter((m) => m.e === expId);

  /* Brief / full split. The backend appends a short overview after the full
     explanation; accepted forms (first match wins):
       explanation.summary (DTO field)
       <!-- summary: … -->                       (HTML comment, anywhere)
       a final paragraph starting 概况 / 概要 / 摘要 / 一句话总结 / TL;DR / Summary + ：/:
     Without one, the collapsed card falls back to the first sentence. */
  const SUMMARY_RE = /(?:^|\n)[ \t>*_]*(?:\*\*)?(?:概况|概要|摘要|一句话总结|TL;?DR|Summary)(?:\*\*)?[ \t]*[:：][ \t]*((?:(?!\n[ \t]*\n)[\s\S])+?)\s*$/i;
  const AUTO_Q = /^(请解释这个知识点|请结合我圈出的疑点)/;
  const userAsked = (q) => !AUTO_Q.test(shownQuestion(q));
  function splitAnswer(exp, n) {
    const text = String(exp.answer || '').trim();
    let brief = (exp.summary || '').trim();
    if (!brief) brief = plainMd(text).split(/(?<=[。！？.!?])\s*/)[0] || '';
    return { full: text, brief: clip(collapse(brief), 120) };
  }

  function diagnosticHtml(n, exp, run) {
    const diagnostic = exp?.learning?.obstacle?.diagnostic;
    if (!diagnostic || !Array.isArray(diagnostic.options)) return '';
    return `<div class="obstacle-check"><p>${esc(diagnostic.question)}</p><div class="obstacle-choices">
      ${diagnostic.options.map((o, i) => `<button class="btn btn--sm" data-diagnostic="${i}" ${run ? 'disabled' : ''}>${esc(o.label)}</button>`).join('')}
      <button class="btn btn--sm" data-diagnostic="expand" ${run ? 'disabled' : ''}>${icon('arrow-r')}直接展开</button>
      </div></div>`;
  }

  /* Answer HTML with the user's marks (bold / italic / highlight) and the words
     already asked about (underlined, click to jump to that child node). */
  function answerHtml(n, exp) {
    const box = document.createElement('div');
    box.innerHTML = renderMd(splitAnswer(exp, n).full, { blocks: false });
    marksOf(n, exp.id).forEach((mk) => {
      const r = textRange(box, mk.q);
      if (r) wrapRange(r, (el) => { el.className = 'u-' + mk.s; });
    });
    explChildren(n).filter((c) => meta(c).source.fromExp === exp.id).forEach((c) => {
      const r = textRange(box, meta(c).source.quote);
      if (r) wrapRange(r, (el) => { el.className = 'u-q'; el.dataset.child = c.id; el.title = '已追问 → ' + c.title; });
    });
    return box.innerHTML;
  }

  function nodeHtml(n) {
    const sel = n.id === S.selected, m = meta(n);
    const run = running(n.id) ? S.runs[n.id] : null;
    const pages = pagesOf(n), chat = chatOf(n), page = currentPage(n);
    const src = srcLabel(n);
    const q = posOf(n);
    const attrs = `data-id="${esc(n.id)}" style="left:${q.x}px;top:${q.y}px"`;
    const branchLabel = { formula: '公式步骤', reasoning: '推理步骤', chart: '图表', question: '疑问', concept: '概念' }[m.branch_type];
    const kindLine = `<span class="kind ${sel ? 'ink' : ''}">${branchLabel || KIND[n.kind] || n.kind} · ${depthLabel(n)}${m.status === 'mastered' ? ' · 已掌握' : ''}</span>`;
    if (!sel) {
      const sum = page ? splitAnswer(page, n).brief : (n.summary || n.content);
      const dots = pages.length ? `<span class="dots" title="${pages.length} 版解释">${pages.slice(-5).map(() => '<i class="on"></i>').join('')}</span>` : '';
      return `<div class="gnode ${m.status === 'mastered' ? 'mastered' : ''}" ${attrs} tabindex="0" role="button" aria-label="知识节点：${esc(n.title)}">
        <div class="gnode__head">${kindLine}<span class="src" title="${esc(src)}">${esc(src)}</span></div>
        <div class="gnode__body"><h4>${esc(n.title)}</h4><div class="gnode__sum">${esc(clip(collapse(sum), 160))}</div>
        ${pages.length || chat.length ? `<div class="gnode__meta">${dots}${pages.length ? `<span>${pages.length} 版解释</span>` : ''}${chat.length ? `<span>${chat.length} 条对话</span>` : ''}</div>` : ''}
        ${run ? runLine(run) : ''}</div></div>`;
    }
    const canExplain = S.kinds.has('explanation');
    const deeper = explChildren(n).length;
    const pi = pageIndex(n);
    const pager = pages.length > 1 ? `<span class="pager" role="group" aria-label="解释版本">
        <button class="icon-btn" data-act="page-prev" ${pi <= 0 ? 'disabled' : ''} aria-label="上一版">${icon('chev-l')}</button>
        <span>${pi + 1} / ${pages.length}</span>
        <button class="icon-btn" data-act="page-next" ${pi >= pages.length - 1 ? 'disabled' : ''} aria-label="下一版">${icon('chev-r')}</button></span>` : '';
    const kids = childrenOf(n.id).length;
    const retell = S.kinds.has('retelling') && page && kids ? `<button class="btn btn--sm" data-act="node-retell" ${run ? 'disabled' : ''}
        title="把已解释的 ${kids} 个子节点串回这个父节点，生成一版回讲">${icon('narrate')}回讲</button>` : '';
    const versions = retellingVersions(n);
    const showingOriginal = versions && page?.id === versions.original.id;
    const versionSwitch = versions ? `<button class="btn btn--sm" data-act="retelling-toggle" ${run ? 'disabled' : ''}
        title="${showingOriginal ? '切换到回讲生成的新解释' : '切换到本次回讲前的原解释'}" aria-pressed="${!!showingOriginal}">
        ${icon(showingOriginal ? 'arrow-r' : 'arrow-l')}${showingOriginal ? '查看新解释' : '查看原解释'}</button>` : '';
    const pageBody = page
      ? `<div class="xpl__body"><div class="answer" data-answer-node="${esc(n.id)}" data-exp="${esc(page.id)}">${answerHtml(n, page)}</div></div>${diagnosticHtml(n, page, run)}`
      : `<div class="xpl__empty"><span>${canExplain ? '还没有解释。' : '当前镜像未启用解释工作流。'}</span>${canExplain ? `<button class="btn btn--sm btn--primary" data-act="node-explain" ${run ? 'disabled' : ''}>${icon('sparkle')}生成解释</button>` : ''}</div>`;
    return `<div class="gnode sel ${m.status === 'mastered' ? 'mastered' : ''}" ${attrs} role="group" aria-label="知识节点：${esc(n.title)}">
      <div class="gnode__head">
        <button class="icon-btn" data-act="node-reveal" title="定位原文" ${m.source?.fromNode ? 'disabled' : ''}>${icon('search')}</button>
        <button class="icon-btn" data-act="node-link" title="连接节点">${icon('arrow-r')}</button>
        <button class="icon-btn" data-act="node-delete" title="删除节点 (Delete)">${icon('trash')}</button>
        <button class="icon-btn" data-act="node-collapse" title="收起 (Esc)">${icon('close')}</button></div>
      <div class="gnode__scroll">
        <div class="gnode__body">
          <input class="title-input" data-title value="${esc(n.title)}" maxlength="160" aria-label="节点标题">
        </div>
        <div class="xpl">
          ${run ? runLine(run) : ''}
          ${!run && ['failed', 'cancelled', 'interrupted'].includes(S.runs[n.id]?.state) ? '<p class="obstacle-error" role="alert">本次生成未完成，已保留上一份解释。可重新生成。</p>' : ''}
          ${pageBody}
          ${page || m.source?.fromNode ? `<div class="node-learning-actions">
            ${page && canExplain ? `<button class="icon-btn" data-act="node-explain" title="重新生成" aria-label="重新生成" ${run ? 'disabled' : ''}>${icon('sparkle')}</button>` : ''}
            ${retell}${versionSwitch}${m.source?.fromNode ? '<button class="btn btn--sm" data-act="node-parent">↑ 返回上层</button>' : ''}
          </div>` : ''}
        </div>
        ${canExplain ? `<div class="chat">
          <div class="explanation-depth" role="group" aria-label="解释深度">${[['auto', '自动'], ['black_box', '知道用途'], ['mechanism', '机制拆解']].map(([value, label]) => `<button type="button" data-depth="${value}" aria-pressed="${(m.explanation_depth || 'auto') === value}" ${run ? 'disabled' : ''}>${label}</button>`).join('')}</div>
          <form class="chat__form" data-chat><input class="input" name="q" maxlength="900" placeholder="继续问这个节点…" aria-label="向节点提问" autocomplete="off" ${run ? 'disabled' : ''}>
            <button class="btn btn--primary" type="submit" aria-label="发送" ${run ? 'disabled' : ''}>${icon('send')}</button></form>
        </div>` : ''}
      </div></div>`;
  }
  const scrollChats = (root = nodesEl) => $$('.chat__log', root).forEach((l) => { l.scrollTop = l.scrollHeight; });
  function renderCanvas() {
    const pool = docNodes();
    nodesEl.innerHTML = pool.map(nodeHtml).join('');
    scrollChats();
    $('#empty-canvas').hidden = pool.length > 0;
    $('#empty-canvas-msg').innerHTML = !S.docs.length ? '先导入一个 Markdown / TXT 文档。'
      : !S.doc ? '从左侧打开一个文档，<br>每个文档都有自己的节点空间。'
      : '这个文档还没有节点。<br>选中一段文字，点击「解释」生成第一个。';
    $('#graph-count').textContent = `${pool.length} 节点 · ${edgeList().length} 连线`;
    applyView(); drawEdges(); renderInspector(); drawConnector(); drawMinimap();
  }
  function renderNode(id) {
    const n = nodeOf(id), el = elOf(n);
    if (!n || !el) return;
    if (el.contains(document.activeElement) && isEditable(document.activeElement) && n.id === S.selected) {
      // Keep the user's caret: refresh only the generated parts.
      const tmp = document.createElement('div'); tmp.innerHTML = nodeHtml(n);
      ['.xpl', '.chat__log', '.gnode__head'].forEach((s) => { const a = $(s, el), b = $(s, tmp); if (a && b) a.replaceWith(b); });
      const form = $('.chat__form', el), nf = $('.chat__form', tmp);
      if (form && nf) $$('input, button', form).forEach((x, i) => { x.disabled = $$('input, button', nf)[i]?.disabled; });
    } else el.outerHTML = nodeHtml(n);
    scrollChats(elOf(n) || nodesEl);
    drawEdges(); drawConnector(); renderInspector(); drawMinimap();
  }

  function selectNode(id, opts = {}) {
    const target = id && nodeOf(id);
    if (target && !inDoc(target)) { openDocument(target.document_id).then(() => selectNode(id, opts)).catch(fail); return; }
    S.selected = target ? id : null;
    hideSelBar();
    renderCanvas(); syncAnchorState();
    const n = nodeOf(S.selected);
    if (n && opts.center) ensureVisible(n);
    if (n && opts.reveal) revealSource(n);
    drawConnector(); savePrefs();
  }
  function placeNew(parent) {
    if (parent) {
      const q = posOf(parent), sibs = S.nodes.filter((c) => c.parent_id === parent.id);
      const last = sibs.reduce((mx, s) => Math.max(mx, posOf(s).y + (elOf(s)?.offsetHeight || 150)), q.y - GAP);
      return { x: q.x + COL, y: sibs.length ? last + GAP : q.y };
    }
    const v = S.view;
    return { x: Math.round((40 - v.x) / v.z), y: Math.round((surface.clientHeight * 0.2 - v.y) / v.z) };
  }
  /* A pdf SourceRef is resolved by the server against ITS extraction text, matching
     quote/prefix/suffix exactly and checking extractor_version. The reader's quote
     comes from the PDF.js text layer (different whitespace), so re-cut quote and
     context from the server text (whitespace-tolerant) and stamp its version. With
     no matching extraction the anchor stays display-only: without document_id /
     revision_id the server stores it as-is and explains from the quote. */
  const escRe = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function pdfAnchor(draft) {
    const display = { quote: draft.quote, selector: draft.selector };
    const words = collapse(draft.quote).split(' ').filter(Boolean);
    if (!S.textVersion || !words.length) return display;
    const hits = [...S.text.matchAll(new RegExp(words.map(escRe).join('\\s+'), 'g'))];
    if (!hits.length) return display;
    const want = collapse(draft.prefix).slice(-16);
    const candidates = hits.filter((m) => (!want || collapse(S.text.slice(Math.max(0, m.index - 64), m.index)).endsWith(want)) &&
      (!draft.suffix || collapse(S.text.slice(m.index + m[0].length, m.index + m[0].length + 64)).startsWith(collapse(draft.suffix).slice(0, 16))));
    if (candidates.length !== 1) throw new Error('选区在原文中无法唯一定位，请扩大选区或使用图像框选。');
    const hit = candidates[0];
    const start = hit.index, end = start + hit[0].length;
    return { document_id: draft.document_id, revision_id: draft.revision_id, selector: draft.selector,
      quote: S.text.slice(start, end), prefix: S.text.slice(Math.max(0, start - 32), start),
      suffix: S.text.slice(end, end + 32), extractor_version: S.textVersion };
  }
  /* Selection → server node. Reader selections hang under the section node that
     contains them; selections inside an explanation become follow-up children. */
  function textAnchor(origin) {
    const raw = S.text.slice(origin.sourceStart, origin.sourceEnd);
    const pattern = collapse(origin.quote).split(' ').map(escRe).join('\\s+');
    const hits = [...raw.matchAll(new RegExp(pattern, 'g'))];
    const renderedHits = [...origin.blockText.matchAll(new RegExp(pattern, 'g'))];
    const ordinal = [...origin.before.matchAll(new RegExp(pattern, 'g'))].length;
    if (hits.length !== renderedHits.length || !hits[ordinal]) throw new Error('无法把这段排版文字准确对应到原文，请缩小选区或使用图像。');
    const start = origin.sourceStart + hits[ordinal].index, end = start + hits[ordinal][0].length;
    return { document_id: origin.documentId, revision_id: origin.revisionId,
      selector: { type: 'text', start, end, unit: 'utf16' }, quote: S.text.slice(start, end),
      prefix: S.text.slice(Math.max(0, start - 32), start), suffix: S.text.slice(end, end + 32) };
  }
  async function createNodeFrom(origin) {
    let parent, document_id, source, visualSource;
    if (origin.kind === 'visual') {
      if (!api.runtime?.learning_features?.visual_selection) throw new Error('当前会话后端不支持图像，请保存工作后重新进入。');
      parent = null; document_id = origin.documentId;
      const form = new FormData(); form.append('file', origin.blob, 'selection.png'); form.append('revision_id', origin.revisionId);
      const { visual } = await api.post(`/api/documents/${p(document_id)}/visuals`, form);
      source = { quote: origin.quote };
      visualSource = { artifact_id: visual.id, document_id, revision_id: visual.revision_id, selector: origin.selector || { type: 'whole-image' } };
    } else if (origin.kind === 'pdf') {
      parent = null; document_id = S.doc.id; source = pdfAnchor(origin.ref);
    } else if (origin.kind === 'node') {
      parent = nodeOf(origin.nodeId);
      document_id = parent.document_id;
      source = { quote: origin.quote, prefix: origin.prefix || '', suffix: origin.suffix || '', fromNode: parent.id, ...(origin.expId ? { fromExp: origin.expId } : {}) };
    } else {
      parent = sectionNodeOf(origin.block) || null;
      document_id = S.doc.id;
      source = textAnchor(origin);
    }
    const node = await sync(api.post(`${projectPath()}/nodes`, {
      document_id, title: guessTitle(origin.quote), kind: 'concept',
      content: origin.quote.slice(0, 20000), parent_id: parent?.id || null,
      metadata: { source, branch_type: origin.branchType || 'auto', ...(visualSource ? { visual_source: visualSource } : {}), status: 'learning', position: placeNew(parent), pages: [] },
    }));
    S.nodes.push(node);
    if (visualSource?.selector?.type === 'whole-image') renderVisualOriginal().catch(fail);
    selectNode(node.id); ensureVisible(node); refreshMarks(); renderTree();
    if (parent) renderNode(parent.id);
    return node;
  }
  async function deleteNode(id) {
    const n = nodeOf(id);
    if (!n) return;
    const kids = childrenOf(id).length;
    const ok = await confirmModal('删除节点', `删除「${esc(clip(n.title, 30))}」以及它的解释与连接？${kids ? `<br>${kids} 个子节点会保留，并变为顶层节点。` : ''}此操作无法撤销。`, '删除');
    if (!ok) return;
    if (running(id)) await api.cancel(S.runs[id].id).catch(() => {});
    await sync(api.del(`/api/nodes/${p(id)}`));
    if (S.selected === id) S.selected = null;
    delete S.history[id];
    await refreshGraph();
    toast(`已删除「${clip(n.title, 16)}」`);
  }
  function revealSource(n) {
    if (!n) return;
    const src = meta(n).source;
    if (src?.fromNode) { toast('该节点来自追问，没有原文锚点'); return; }
    const open = () => {
      if (S.mode === 'raw') { S.mode = 'md'; renderReader(); }
      const m = sheet.querySelector(`mark.anchor[data-node="${CSS.escape(n.id)}"]`);
      if (!m) { toast('原文中未找到这段内容'); return; }
      m.scrollIntoView({ block: 'center' });
      m.classList.remove('flash'); void m.offsetWidth; m.classList.add('flash');
      setTimeout(drawConnector, 30);
    };
    if (S.doc?.id !== n.document_id) openDocument(n.document_id).then(open).catch(fail); else open();
  }
  async function patchMeta(n, changes) {
    Object.assign(n, await sync(api.patch(`/api/nodes/${p(n.id)}`, { metadata: { ...meta(n), ...changes } })));
    return n;
  }

  /* Node card events (delegated) */
  const saveTimers = {};
  function queueSave(n, field, value, el) {
    const saved = $('[data-saved]', el);
    if (saved) saved.textContent = '· 编辑中';
    clearTimeout(saveTimers[n.id + field]);
    saveTimers[n.id + field] = setTimeout(action(async () => {
      if (field === 'title' && !value.trim()) { if (saved) saved.textContent = '· 标题不能为空'; return; }
      const updated = await sync(api.patch(`/api/nodes/${p(n.id)}`, { [field]: field === 'title' ? value.trim() : value }));
      Object.assign(n, updated);
      if (saved && el.isConnected) saved.textContent = '· 已保存';
      if (field === 'title') { renderInspector(); const mk = sheet.querySelector(`mark.anchor[data-node="${CSS.escape(n.id)}"]`); if (mk) mk.title = '知识节点：' + n.title; }
      drawEdges();
    }), 600);
  }
  nodesEl.addEventListener('input', (e) => {
    const nEl = e.target.closest('.gnode'), n = nEl && nodeOf(nEl.dataset.id);
    if (!n) return;
    if (e.target.matches('[data-title]')) queueSave(n, 'title', e.target.value, nEl);
    if (e.target.matches('[data-content]')) queueSave(n, 'content', e.target.value, nEl);
  });
  nodesEl.addEventListener('toggle', () => drawEdges(), true);
  nodesEl.addEventListener('click', action(async (e) => {
    const child = e.target.closest('.u-q[data-child]');
    if (child && getSelection().isCollapsed) { selectNode(child.dataset.child, { center: true }); return; }
    const nEl = e.target.closest('.gnode'), n = nEl && nodeOf(nEl.dataset.id);
    if (!n) return;
    const act = e.target.closest('[data-act]')?.dataset.act;
    const depth = e.target.closest('[data-depth]')?.dataset.depth;
    if (depth && !running(n.id)) { await patchMeta(n, { explanation_depth: depth }); renderNode(n.id); return; }
    const choice = e.target.closest('[data-diagnostic]')?.dataset.diagnostic;
    if (choice !== undefined) {
      const exp = currentPage(n), diagnostic = exp?.learning?.obstacle?.diagnostic;
      if (!diagnostic || running(n.id)) return;
      const question = choice === 'expand' ? '请直接展开完整解释，补全必要步骤和适用条件，不再提诊断问题。' : diagnostic.options[Number(choice)]?.question;
      if (question) await explain(n, question, { responseMode: 'expand' });
      return;
    }
    if (act === 'node-parent') { selectNode(meta(n).source?.fromNode, { center: true }); return; }
    if (act === 'node-delete') await deleteNode(n.id);
    if (act === 'node-collapse') selectNode(null);
    if (act === 'node-cancel' && running(n.id)) { await api.cancel(S.runs[n.id].id); toast('已请求取消'); }
    if (act === 'node-link') linkNode(n);
    if (act === 'node-reveal') revealSource(n);
    if (act === 'node-explain') await explain(n, '', { page: true });
    if (act === 'node-optimize') await optimize(n);
    if (act === 'node-retell') await retell(n);
    if (act === 'retelling-toggle' && !running(n.id)) {
      const versions = retellingVersions(n);
      if (!versions) return;
      await patchMeta(n, { retelling_view: {
        explanation_id: versions.current.id, original: currentPage(n)?.id !== versions.original.id,
      } });
      renderNode(n.id); drawEdges(); drawMinimap();
    }
    if (act === 'page-prev' || act === 'page-next') {
      const i = pageIndex(n) + (act === 'page-next' ? 1 : -1);
      if (i < 0 || i >= pagesOf(n).length) return;
      n.metadata = { ...meta(n), page: i, pages: pagesOf(n).map((h) => h.id) };
      renderNode(n.id);
      await patchMeta(n, {});
    }
  }));
  nodesEl.addEventListener('submit', action(async (e) => {
    const form = e.target.closest('[data-chat]');
    if (!form) return;
    e.preventDefault();
    const n = nodeOf(form.closest('.gnode').dataset.id), q = form.q.value.trim();
    if (!q) { form.q.focus(); return; }
    form.q.value = '';
    await explain(n, q, { page: false });
    elOf(n)?.querySelector('[data-chat] input')?.focus();
  }));

  /* Selecting words inside an explanation (page or chat answer):
     追问 · 加粗 · 斜体 · 高亮 · 查找. Notes text: 提取节点 · 追问. */
  let selBar = null;
  function hideSelBar() { if (selBar) { selBar.remove(); selBar = null; } }
  function nodeSelection() {
    const sel = getSelection(), ta = document.activeElement;
    if (ta && ta.matches?.('.gnode.sel [data-content]') && ta.selectionEnd > ta.selectionStart) {
      return { quote: ta.value.slice(ta.selectionStart, ta.selectionEnd).trim(), nodeId: ta.closest('.gnode').dataset.id, rect: ta.getBoundingClientRect(), notes: true };
    }
    if (!sel.rangeCount || sel.isCollapsed) return null;
    const host = (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement)?.closest('.gnode.sel .answer');
    if (!host || !host.contains(sel.focusNode)) return null;
    const range = sel.getRangeAt(0), before = range.cloneRange(), after = range.cloneRange();
    before.selectNodeContents(host); before.setEnd(range.startContainer, range.startOffset);
    after.selectNodeContents(host); after.setStart(range.endContainer, range.endOffset);
    return { quote: sel.toString().trim(), prefix: before.toString().slice(-64), suffix: after.toString().slice(0, 64), nodeId: host.dataset.answerNode, expId: host.dataset.exp, rect: range.getBoundingClientRect() };
  }
  function showSelBar(s, at) {
    if (!s || s.quote.length < 1) return;
    hideSelBar();
    const n = nodeOf(s.nodeId);
    const has = (st) => !s.notes && marksOf(n, s.expId).some((m) => m.q === s.quote && m.s === st);
    const canAsk = S.kinds.has('explanation');
    selBar = document.createElement('div');
    selBar.className = 'node-sel-bar';
    selBar.setAttribute('role', 'toolbar');
    selBar.innerHTML = s.notes
      ? `<button data-k="extract">${icon('node')}提取节点</button>${canAsk ? `<button data-k="ask">${icon('sparkle')}追问</button>` : ''}`
      : `${canAsk ? `<button data-k="ask">${icon('sparkle')}追问</button><span class="sep"></span>` : ''}
         <button data-k="b" aria-pressed="${has('b')}" title="加粗"><b>B</b></button>
         <button data-k="i" aria-pressed="${has('i')}" title="斜体"><i>I</i></button>
         <button data-k="h" aria-pressed="${has('h')}" title="高亮">${icon('marker')}</button>
         <span class="sep"></span><button data-k="find" title="在原文中查找">${icon('search')}查找</button>`;
    const r = at || s.rect;
    document.body.appendChild(selBar);
    selBar.style.left = Math.max(8, Math.min(r.left, innerWidth - selBar.offsetWidth - 8)) + 'px';
    selBar.style.top = Math.max(8, r.top - selBar.offsetHeight - 8) + 'px';
    selBar.addEventListener('mousedown', (ev) => ev.preventDefault());
    selBar.addEventListener('click', action(async (ev) => {
      const k = ev.target.closest('[data-k]')?.dataset.k;
      if (!k) return;
      const origin = { kind: 'node', nodeId: s.nodeId, expId: s.expId, quote: s.quote.slice(0, 6000), prefix: s.prefix, suffix: s.suffix };
      hideSelBar();
      if (k === 'extract') { const c = await createNodeFrom(origin); toast(`已提取「${clip(c.title, 16)}」`); }
      if (k === 'ask') openAsk(origin, s.rect);
      if (k === 'find') {
        getSelection().removeAllRanges();
        if (n.document_id && S.doc?.id !== n.document_id) await openDocument(n.document_id);
        openFind(s.quote);
      }
      if (['b', 'i', 'h'].includes(k)) await toggleMark(n, s.expId, s.quote, k);
    }));
  }
  async function toggleMark(n, expId, quote, style) {
    const q = quote.slice(0, 80);
    let marks = [...(meta(n).marks || [])];
    const i = marks.findIndex((m) => m.e === expId && m.q === q && m.s === style);
    if (i >= 0) marks.splice(i, 1); else marks.push({ e: expId, q, s: style });
    if (marks.length > 40) marks = marks.slice(-40);   // keeps node metadata well under the 8 KB server cap
    getSelection().removeAllRanges();
    n.metadata = { ...meta(n), marks };
    renderNode(n.id);
    await patchMeta(n, {});
  }
  nodesEl.addEventListener('mouseup', () => setTimeout(() => showSelBar(nodeSelection()), 0));
  nodesEl.addEventListener('contextmenu', (e) => {
    const s = nodeSelection();
    if (!s) return;
    e.preventDefault();
    showSelBar(s, { left: e.clientX, top: e.clientY });
  });

  function linkNode(n) {
    const others = docNodes().filter((x) => x.id !== n.id);
    if (!others.length) { toast('至少需要两个节点才能连接'); return; }
    modal({
      title: '连接节点',
      body: `<div style="display:grid;gap:12px">
        <p class="muted" style="margin:0;font-size:13px">从「${esc(clip(n.title, 30))}」出发</p>
        <label class="field"><span class="label">关系</span><select class="input" id="ln-rel">${Object.entries(REL).filter(([k]) => k !== 'parent_of').map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label>
        <label class="field"><span class="label">目标节点</span><select class="input" id="ln-target">${others.map((x) => `<option value="${esc(x.id)}">${esc(clip(x.title, 50))}</option>`).join('')}</select></label>
        <p class="gate__msg" id="ln-msg" role="alert"></p></div>`,
      actions: [{ label: '取消', run: (c) => c() }, { label: '连接', primary: true, run: async (c, r, b) => {
        b.disabled = true;
        try {
          await sync(api.post(`${projectPath()}/graph/edges`, { source_node_id: n.id, target_node_id: $('#ln-target', r).value, relation: $('#ln-rel', r).value }));
          c(); await refreshGraph(); toast('已建立连接');
        } catch (e) { $('#ln-msg', r).textContent = e.status === 409 || e.status === 422 ? '无法建立：' + e.message : describe(e); b.disabled = false; }
      } }],
    });
  }

  /* ================= Runs ================= */
  /* Runs that should land as a new explanation page (vs a chat turn) are
     remembered per tab, so a reload while generating still files them right. */
  const PAGE_RUNS = 'tlm.page-runs';
  const pageRunIds = () => { try { return JSON.parse(sessionStorage.getItem(PAGE_RUNS) || '[]'); } catch { return []; } };
  function markPageRun(id, on) {
    const s = new Set(pageRunIds());
    if (on) s.add(id); else s.delete(id);
    sessionStorage.setItem(PAGE_RUNS, JSON.stringify([...s].slice(-50)));
  }
  function trackRun(nodeId, run) {
    S.runs[nodeId] = run;
    paintState();
    const el = elOf(nodeOf(nodeId));
    const line = el && $('.gen', el);
    if (line && !api.TERMINAL.has(run.state)) line.firstChild.textContent = ({ queued: '排队中', running: '生成中' }[run.state] || run.state) + (run.progress ? ` · ${run.progress}%` : '');
    else renderNode(nodeId);
  }
  async function addExplanation(node, exp, asPage) {
    (S.history[node.id] ||= []).unshift(exp);
    if (asPage) {
      const ids = [...pagesOf(node).map((h) => h.id).filter((id) => id !== exp.id), exp.id];
      node.metadata = { ...meta(node), pages: ids, page: ids.length - 1 };
      await patchMeta(node, {});
    } else if (!Array.isArray(meta(node).pages)) {
      // Pin the legacy page assignment so a chat turn never becomes page 1.
      node.metadata = { ...meta(node), pages: pagesOf(node).map((h) => h.id).filter((id) => id !== exp.id) };
      await patchMeta(node, {});
    }
  }
  /* page=true → a new explanation version (pager); page=false → a chat turn. */
  async function explain(node, question, { page = true, responseMode = 'auto' } = {}) {
    if (!S.kinds.has('explanation')) throw new Error('当前镜像未启用解释工作流');
    if (running(node.id)) throw new Error('这个节点正在生成，请稍候');
    const input = explanationInput(node.id, question, responseMode);
    if (!page) { S.pendingChat[node.id] = question; renderNode(node.id); }
    let runId = null;
    try {
      const result = await api.run('explanation', input, { onRun: (r) => {
        if (!runId) { runId = r.id; if (page) markPageRun(r.id, true); }
        trackRun(node.id, r);
      } });
      delete S.pendingChat[node.id];
      await addExplanation(node, result.explanation, page);
      toast('已更新解释');
    } finally {
      delete S.pendingChat[node.id];
      if (runId) markPageRun(runId, false);
      paintState();
      if (nodeOf(node.id)) renderNode(node.id);
      drawMinimap();
    }
  }
  /* 优化解释: walk this node's follow-up tree, gather every word the user marked
     or asked about plus the deeper explanations, and request a new version. */
  function optimizePrompt(n) {
    const lines = [], seen = new Set();
    const visit = (node, depth) => {
      if (seen.has(node.id) || depth > 3) return;
      seen.add(node.id);
      const page = currentPage(node);
      const marked = page ? marksOf(node, page.id).map((m) => m.q) : [];
      const kids = explChildren(node);
      const words = [...new Set([...marked, ...kids.map((c) => meta(c).source.quote)].map((x) => clip(collapse(x), 30)))];
      const pad = '  '.repeat(depth);
      if (words.length) lines.push(`${pad}在「${clip(node.title, 24)}」里我圈出的词：${words.join('、')}`);
      kids.forEach((c) => {
        const cp = currentPage(c);
        if (cp) lines.push(`${pad}  对「${clip(collapse(meta(c).source.quote), 24)}」的深入解释：${clip(plainMd(splitAnswer(cp, c).full), 120)}`);
        visit(c, depth + 1);
      });
    };
    visit(n, 0);
    return clip('请结合我圈出的疑点，并把下面这些更深层的解释融合进来，重新完整地解释一遍，让我不需要再追问：\n' + lines.join('\n'), 860);
  }
  async function optimize(n) {
    if (!explChildren(n).length) { toast('先在解释里圈词并追问，生成更深的解释后再优化'); return; }
    await explain(n, optimizePrompt(n), { page: true });
  }
  /* 回讲 (retelling): the server folds the explained direct children back into
     this parent and stores the result as a new explanation version. It needs a
     learner profile; when none exists yet we ask for the education stage once. */
  const STAGES = { primary_secondary: '中小学', undergraduate: '本科', graduate: '研究生' };
  async function ensureProfile() {
    const profile = await sync(api.get('/api/learning/profile'));
    if (profile.revision > 0) return true;
    const stage = await new Promise((res) => {
      modal({ title: '设置学习档案',
        body: `<div style="display:grid;gap:12px"><p class="muted small" style="margin:0">回讲会按你的学习阶段组织讲法。档案保存在这个工作区的服务器上，只需设置一次。</p>
          <label class="field"><span class="label">学习阶段</span><select class="input" id="lp-stage">
          ${Object.entries(STAGES).map(([k, v]) => `<option value="${k}" ${k === 'undergraduate' ? 'selected' : ''}>${v}</option>`).join('')}</select></label></div>`,
        actions: [{ label: '取消', run: (c) => { c(); res(null); } }, { label: '保存', primary: true, run: (c, r) => { const v = $('#lp-stage', r).value; c(); res(v); } }] });
    });
    if (!stage) return false;
    await sync(api.put('/api/learning/profile', { expected_revision: profile.revision, profile: { education_stage: stage } }));
    return true;
  }
  let relationsBusy = false;
  async function organizeRelations() {
    if (relationsBusy) return;
    if (layoutBusy) throw new Error('节点正在排列，请稍候');
    if (!S.doc || S.nodes.filter(inDoc).length < 2) throw new Error('当前文档至少需要两个节点才能梳理关系');
    const projectId = S.project.id, documentId = S.doc.id;
    const button = $('[data-act="ai-relations"]');
    relationsBusy = true;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    let runId, resultReceived = false;
    const pending = modal({ title: 'AI 梳理关系', body: '<p role="status">正在梳理…</p>',
      actions: [{ label: '取消', run: (close) => { close(); if (runId) api.cancel(runId).catch(fail); } }] });
    try {
      const result = await api.run('graph_relations', { document_id: documentId }, { onRun: (run) => {
        runId = run.id;
        if (!pending.root.isConnected && !api.TERMINAL.has(run.state)) api.cancel(run.id).catch(() => {});
      } });
      if (!pending.root.isConnected) return;
      resultReceived = true;
      pending.close();
      if (S.project?.id !== projectId || S.doc?.id !== documentId) return;
      const suggestions = result.suggestions || [];
      if (!suggestions.length) await autoLayout({ silent: true });
      if (S.project?.id !== projectId || S.doc?.id !== documentId) return;
      const name = (id) => nodeOf(id)?.title || id;
      const relation = (item) => `<span>${esc(name(item.source_node_id))}</span><span class="relation-review__arrow"> → ${esc(REL[item.relation] || item.relation)} → </span><span>${esc(name(item.target_node_id))}</span>`;
      modal({ title: suggestions.length ? `关系建议 · ${suggestions.length} 项` : '关系梳理完成', wide: true,
        body: suggestions.length ? `<div class="relation-review">${suggestions.map((s) => `<label class="relation-review__item">
          <input type="checkbox" value="${esc(s.id)}" checked>
          <span class="relation-review__content"><strong>${esc({ add: '新增', update: '调整', remove: '移除' }[s.action])}</strong>
          ${s.previous ? `<span class="relation-review__before">${relation(s.previous)}</span>` : ''}
          ${s.action !== 'remove' ? `<span class="relation-review__after">${relation(s)}</span>` : ''}
          <span class="muted">${esc(s.reason)}</span></span></label>`).join('')}</div>` : '<p>当前关系无需调整。</p>',
        actions: [ { label: suggestions.length ? '保留原连线' : '完成', run: (close) => close() },
          ...(suggestions.length ? [{ label: '保留选中建议', primary: true, id: 'apply-relations', run: async (close, root, applyButton) => {
            const ids = $$('input[type="checkbox"]:checked', root).map((c) => c.value);
            if (!ids.length) return;
            if (S.project?.id !== projectId || S.doc?.id !== documentId) throw new Error('当前文档已切换，请重新梳理');
            applyButton.disabled = true;
            try {
              const applied = await sync(api.post(`/api/projects/${api.part(projectId)}/graph/relations/apply`, { run_id: runId, suggestion_ids: ids }));
              if (root.isConnected) close();
              if (S.project?.id === projectId) {
                await refreshGraph();
                if (S.doc?.id === documentId) await autoLayout({ silent: true });
              }
              toast(`已保留 ${applied.applied} 项关系建议`);
            } catch (err) {
              if (err.status === 409) {
                if (root.isConnected) close();
                await refreshGraph();
              }
              throw err;
            } finally { applyButton.disabled = false; }
          } }] : []) ],
        onOpen: (root) => {
          const update = () => { const b = $('#apply-relations', root); if (b) b.disabled = !root.querySelector('input:checked'); };
          root.addEventListener('change', update);
        },
      });
    } catch (err) {
      if (resultReceived) throw err;
      if (pending.root.isConnected) { pending.close(); throw err; }
    } finally {
      relationsBusy = false;
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }

  async function retell(n) {
    if (running(n.id)) throw new Error('这个节点正在生成，请稍候');
    if (!(await ensureProfile())) return;
    let runId = null;
    try {
      const result = await api.run('retelling', { node_id: n.id }, { onRun: (r) => {
        if (!runId) { runId = r.id; markPageRun(r.id, true); }
        trackRun(n.id, r);
      } });
      if (result.status === 'blocked') {
        const names = (result.blocked_child_ids || []).map((id) => nodeOf(id)?.title).filter(Boolean);
        toast(`子节点还没有解释，先解释它们再回讲${names.length ? '：' + clip(names.join('、'), 60) : ''}`, { ms: 6000 });
        return;
      }
      await addExplanation(n, result.explanation, true);
      toast(result.status === 'partial' ? `已生成部分回讲（${new Set([...(result.blocked_child_ids || []), ...(result.unresolved_descendant_ids || [])]).size} 个分支节点尚未解释）` : '已生成回讲');
    } finally {
      if (runId) markPageRun(runId, false);
      paintState();
      if (nodeOf(n.id)) renderNode(n.id);
    }
  }
  /* Resume runs still in flight after a reload. */
  async function resumeRuns() {
    const runs = (await api.get('/api/runs')).items || [];
    $('#usage-v').textContent = String(runs.length);
    const pageSet = new Set(pageRunIds());
    runs.filter((r) => !api.TERMINAL.has(r.state) && ['explanation', 'retelling'].includes(r.kind) && nodeOf(r.input?.node_id)).forEach((r) => {
      const id = r.input.node_id, asPage = pageSet.has(r.id);
      trackRun(id, r);
      api.follow(r, { onRun: (v) => trackRun(id, v) })
        .then((res) => res.explanation && addExplanation(nodeOf(id), res.explanation, asPage))
        .catch((e) => { if (e.run?.state !== 'cancelled') fail(e); })
        .finally(() => { markPageRun(r.id, false); paintState(); renderNode(id); });
    });
    return runs;
  }

  /* ================= Inspector ================= */
  const inspector = $('#inspector');
  function placeInspector() {
    const target = innerWidth <= 700 ? $('#canvas') : surface;
    if (inspector.parentElement !== target) target.appendChild(inspector);
  }
  function renderInspector() {
    const n = nodeOf(S.selected);
    const pool = docNodes(), total = pool.length, mastered = pool.filter((x) => meta(x).status === 'mastered').length;
    const open = localStorage.getItem('tlm.insp') === '1';
    inspector.classList.toggle('open', open);
    const head = `<div class="inspector__head"><span class="label">检查器</span><span class="mono muted" style="margin-left:auto">${mastered} / ${total} 已掌握</span>
      <button class="icon-btn" data-act="insp-toggle" aria-expanded="${open}" title="${open ? '收起' : '展开'}检查器">${icon('chev-ud')}</button></div>`;
    let body;
    if (n) {
      const rels = S.edges.filter((e) => e.source_node_id === n.id || e.target_node_id === n.id);
      body = `<dl class="kv">
          <div><dt>概念</dt><dd title="${esc(n.title)}">${esc(n.title)}</dd></div>
          <div><dt>类型</dt><dd><select class="kind-select" data-act-change="kind" aria-label="节点类型">${Object.entries(KIND).map(([k, v]) => `<option value="${k}" ${k === n.kind ? 'selected' : ''}>${v}</option>`).join('')}</select></dd></div>
          <div><dt>深度</dt><dd>${depthLabel(n)}${n.parent_id && nodeOf(n.parent_id) ? ' · 属于「' + esc(clip(nodeOf(n.parent_id).title, 10)) + '」' : ''}</dd></div>
          <div><dt>来源</dt><dd title="${esc(srcLabel(n))}">${esc(srcLabel(n)) || '—'}</dd></div>
        </dl>
        ${rels.length ? `<ul class="rel-list">${rels.map((e) => {
          const out = e.source_node_id === n.id, other = nodeOf(out ? e.target_node_id : e.source_node_id);
          return `<li><span class="mono">${out ? '→' : '←'} ${esc(REL[e.relation] || e.relation)}</span><span class="t" title="${esc(other?.title || '')}">${esc(other?.title || '已删除')}</span><button class="icon-btn" data-edge-delete="${esc(e.id)}" title="删除连接" aria-label="删除连接">${icon('trash')}</button></li>`;
        }).join('')}</ul>` : ''}
        <div class="inspector__actions">
          <button class="btn btn--sm" data-act="node-reveal" ${meta(n).source?.fromNode ? 'disabled' : ''}>定位原文</button>
          <button class="btn btn--sm" data-act="node-master" aria-pressed="${meta(n).status === 'mastered'}">${meta(n).status === 'mastered' ? '标为学习中' : '标为已掌握'}</button>
        </div>`;
    } else {
      body = `<p class="muted insp-empty" style="margin:0;padding:10px 12px;font-size:12px">${total ? '点击节点查看详情；拖动空白处平移画布。' : '还没有节点。'}</p>`;
    }
    const canNarrate = S.kinds.has('narration') && total > 0;
    inspector.innerHTML = head + body + `<div class="inspector__cta"><button class="btn btn--primary" data-act="narrate" ${canNarrate ? '' : 'disabled'} title="${S.kinds.has('narration') ? '' : '当前镜像未启用串讲工作流'}">${icon('narrate')}生成串讲</button></div>`;
  }
  inspector.addEventListener('click', action(async (e) => {
    const act = e.target.closest('[data-act]')?.dataset.act;
    const n = nodeOf(S.selected);
    if (act === 'insp-toggle') { localStorage.setItem('tlm.insp', inspector.classList.contains('open') ? '0' : '1'); renderInspector(); return; }
    if (act === 'node-reveal' && n) revealSource(n);
    if (act === 'node-master' && n) {
      const status = meta(n).status === 'mastered' ? 'learning' : 'mastered';
      Object.assign(n, await sync(api.patch(`/api/nodes/${p(n.id)}`, { metadata: { ...meta(n), status } })));
      renderCanvas();
      toast(status === 'mastered' ? '已标为掌握' : '已标为学习中');
    }
    if (act === 'narrate') await openNarration();
    const edge = e.target.closest('[data-edge-delete]');
    if (edge) { await sync(api.del(`${projectPath()}/graph/edges/${p(edge.dataset.edgeDelete)}`)); await refreshGraph(); toast('已删除连接'); }
  }));
  inspector.addEventListener('change', action(async (e) => {
    const n = nodeOf(S.selected);
    if (n && e.target.matches('[data-act-change="kind"]')) {
      Object.assign(n, await sync(api.patch(`/api/nodes/${p(n.id)}`, { kind: e.target.value })));
      renderCanvas();
    }
  }));

  /* ================= Narration ================= */
  const MAX_NARRATION_NODES = 10;
  const narrationMd = (nar) => [`# ${nar.title}`, '', `> ${nar.segment_count ?? nar.segments.length} 段 · 约 ${Math.round(nar.total_duration_seconds || 0)} 秒 · ${nar.provider}/${nar.model}`, '',
    ...nar.segments.flatMap((s, i) => [`## ${i + 1}. ${s.title}`, '', s.text, ''])].join('\n');
  async function openNarration() {
    if (!S.kinds.has('narration') || !docNodes().length) return;
    const saved = (await sync(api.get(`${projectPath()}/narrations`))).items || [];
    // Default order: prerequisites (deeper) first, then tree order.
    const tree = treeOrder();
    let order = docNodes().sort((a, b) => depthOf(b) - depthOf(a) || tree.indexOf(a) - tree.indexOf(b))
      .map((n, i) => ({ n, on: i < MAX_NARRATION_NODES }));
    let current = saved[0] || null, busy = false;
    const { root } = modal({
      title: '生成串讲', wide: true,
      body: `<div class="narr">
        <div><label class="field" style="margin-bottom:8px"><span class="label">标题（可选）</span><input class="input" id="narr-title" maxlength="200" placeholder="${esc(S.project.title)} 串讲"></label>
          <div class="label" id="narr-count" style="margin:8px 0 4px"></div>
          <ul class="narr__list" id="narr-list" aria-label="节点与顺序"></ul></div>
        <div style="min-width:0;padding-left:16px">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><span class="label">已生成</span>
            <select class="input" id="narr-history" style="height:26px;flex:1" aria-label="历史串讲"></select></div>
          <div class="gen" id="narr-run" hidden></div>
          <pre class="narr__preview" id="narr-prev" aria-live="polite"></pre></div></div>`,
      actions: [
        { label: '复制', run: async () => { if (current) { await copyText(narrationMd(current)); toast('讲稿已复制'); } } },
        { label: '下载 .md', run: () => current && download(`${current.title}.md`, narrationMd(current)) },
        { label: '保存为文档', run: async (c) => { if (!current) return; c(); await createDocument(current.title, narrationMd(current)); } },
        { label: '生成串讲', primary: true, id: 'narr-go', run: async (c, r, b) => {
          const ids = order.filter((o) => o.on).map((o) => o.n.id);
          if (!ids.length) { toast('请至少选择 1 个节点'); return; }
          busy = true; b.disabled = true; paint();
          const title = $('#narr-title', r).value.trim();
          try {
            const res = await api.run('narration', { project_id: S.project.id, node_ids: ids, ...(title ? { title } : {}) }, {
              onRun: (v) => { const el = $('#narr-run', r); if (el) { el.hidden = false; el.textContent = `${{ queued: '排队中', running: '生成中' }[v.state] || v.state}${v.progress ? ' · ' + v.progress + '%' : ''}`; } },
            });
            current = res.narration; saved.unshift(current);
            toast('串讲已生成');
          } finally { busy = false; if (b.isConnected) { b.disabled = false; $('#narr-run', r).hidden = true; paint(); } }
        } },
      ],
    });
    function paint() {
      const picked = order.filter((o) => o.on).length;
      $('#narr-count', root).textContent = `已选 ${picked} / ${MAX_NARRATION_NODES} · 按讲述顺序`;
      $('#narr-list', root).innerHTML = order.map((o, i) => `<li>
        <label class="check"><input type="checkbox" data-on="${i}" ${o.on ? 'checked' : ''} ${!o.on && picked >= MAX_NARRATION_NODES ? 'disabled' : ''} aria-label="包含 ${esc(o.n.title)}"></label>
        <span class="t" title="${esc(o.n.title)}">${esc(o.n.title)}</span>
        <button class="icon-btn" data-up="${i}" ${i ? '' : 'disabled'} aria-label="上移"><svg class="ic" style="transform:rotate(-90deg)"><use href="#i-chev-r"/></svg></button>
        <button class="icon-btn" data-down="${i}" ${i < order.length - 1 ? '' : 'disabled'} aria-label="下移"><svg class="ic" style="transform:rotate(90deg)"><use href="#i-chev-r"/></svg></button></li>`).join('');
      $('#narr-history', root).innerHTML = saved.length ? saved.map((x, i) => `<option value="${i}" ${x === current ? 'selected' : ''}>${esc(x.title)} · ${new Date(x.created_at).toLocaleString('zh-CN', { hour12: false })}</option>`).join('') : '<option>尚无串讲</option>';
      $('#narr-prev', root).textContent = current ? narrationMd(current) : '选择节点并点击「生成串讲」。';
      $$('.modal__foot .btn:not(.btn--primary)', root).forEach((b) => { b.disabled = !current || busy; });
    }
    $('#narr-list', root).addEventListener('click', (e) => {
      const up = e.target.closest('[data-up]'), down = e.target.closest('[data-down]');
      const swap = (i, j) => { [order[i], order[j]] = [order[j], order[i]]; paint(); };
      if (up) swap(+up.dataset.up, +up.dataset.up - 1);
      if (down) swap(+down.dataset.down, +down.dataset.down + 1);
    });
    $('#narr-list', root).addEventListener('change', (e) => { const c = e.target.closest('[data-on]'); if (c) { order[c.dataset.on].on = c.checked; paint(); } });
    $('#narr-history', root).addEventListener('change', (e) => { current = saved[+e.target.value] || current; paint(); });
    paint();
  }

  /* ================= Explorer / documents ================= */
  const treeEl = $('#tree'), filterEl = $('#filter');
  function hiName(name, q) {
    const i = name.toLowerCase().indexOf(q);
    return i < 0 ? esc(name) : esc(name.slice(0, i)) + '<mark>' + esc(name.slice(i, i + q.length)) + '</mark>' + esc(name.slice(i + q.length));
  }
  /* ---------- File tree: folders, multi-select, cut / copy / paste / move ----------
     Folders are path prefixes of the document title ("资料/第三章/attention.md"),
     persisted through PATCH /api/documents/{id} {title}. Empty folders have no
     server representation yet and are kept per project in localStorage until a
     document is placed inside. */
  const normPath = (s) => String(s || '').split('/').map((x) => x.trim()).filter(Boolean).join('/');
  const baseName = (title) => { const t = normPath(title); return t.slice(t.lastIndexOf('/') + 1) || t; };
  const dirOf = (title) => { const t = normPath(title); const i = t.lastIndexOf('/'); return i < 0 ? '' : t.slice(0, i); };
  const joinPath = (dir, name) => normPath(dir ? `${dir}/${name}` : name);
  const folderKey = () => `tlm.folders.${S.project?.id}`;
  const T = { sel: new Set(), anchor: null, clip: null, open: null };   // sel keys: 'd:<id>' | 'f:<path>'
  const localFolders = () => readJSON(folderKey(), []);
  function saveLocalFolders(list) { localStorage.setItem(folderKey(), JSON.stringify([...new Set(list.map(normPath).filter(Boolean))].sort())); }
  function openFolders() {
    if (!T.open) T.open = new Set(readJSON(folderKey() + '.open', null) ?? allFolders());
    return T.open;
  }
  const saveOpen = () => localStorage.setItem(folderKey() + '.open', JSON.stringify([...openFolders()]));
  function allFolders() {
    const set = new Set();
    const add = (path) => { const parts = normPath(path).split('/').filter(Boolean); for (let i = 1; i <= parts.length; i++) set.add(parts.slice(0, i).join('/')); };
    S.docs.forEach((d) => add(dirOf(d.title)));
    localFolders().forEach(add);
    return [...set].sort((a, b) => a.localeCompare(b, 'zh'));
  }
  /* Flat, ordered row list (folders first, then files) honouring collapsed folders and the filter. */
  function treeRows() {
    const q = filterEl.value.trim().toLowerCase();
    const folders = allFolders(), open = openFolders(), rows = [];
    const docsIn = (dir) => S.docs.filter((d) => dirOf(d.title) === dir).sort((a, b) => baseName(a.title).localeCompare(baseName(b.title), 'zh'));
    const match = (d) => !q || d.title.toLowerCase().includes(q);
    const hasMatch = (dir) => S.docs.some((d) => (dirOf(d.title) === dir || dirOf(d.title).startsWith(dir + '/')) && match(d));
    const walk = (dir, depth) => {
      folders.filter((f) => dirOf(f) === dir).forEach((f) => {
        if (q && !hasMatch(f)) return;
        rows.push({ kind: 'f', key: 'f:' + f, path: f, name: baseName(f), depth, open: q ? true : open.has(f) });
        if (q || open.has(f)) walk(f, depth + 1);
      });
      docsIn(dir).filter(match).forEach((d) => rows.push({ kind: 'd', key: 'd:' + d.id, doc: d, name: baseName(d.title), depth }));
    };
    walk('', 0);
    return rows;
  }
  function renderTree() {
    const q = filterEl.value.trim().toLowerCase();
    const rows = treeRows();
    const cut = T.clip?.mode === 'cut' ? new Set(T.clip.keys) : new Set();
    treeEl.innerHTML = rows.map((r) => {
      const pad = 10 + r.depth * 14, picked = T.sel.has(r.key) ? 'picked' : '', dim = cut.has(r.key) ? 'cut' : '';
      if (r.kind === 'f') {
        return `<div class="t-item ${picked} ${dim}" data-key="${esc(r.key)}"><button class="t-row" role="treeitem" aria-expanded="${r.open}" aria-selected="${!!picked}" data-folder="${esc(r.path)}" draggable="true" style="padding-left:${pad}px" title="${esc(r.path)}">
          ${icon('chev-r', 'chev')}${icon('folder')}<span class="name">${q ? hiName(r.name, q) : esc(r.name)}</span></button></div>`;
      }
      const d = r.doc, active = S.doc?.id === d.id;
      const count = S.nodes.filter((n) => n.document_id === d.id).length;
      return `<div class="t-item ${active ? 'active' : ''} ${picked} ${dim}" data-key="${esc(r.key)}"><button class="t-row ${active ? 'active' : ''}" role="treeitem" aria-selected="${!!picked || active}" data-doc="${esc(d.id)}" draggable="true" style="padding-left:${pad + 16}px" title="${esc(d.title)}">
        ${icon(d.format === 'pdf' ? 'file-pdf' : isMarkdown(d) ? 'file' : 'file-code')}<span class="name">${q ? hiName(r.name, q) : esc(r.name)}</span><span class="mono t-count">${count || ''}</span></button>
        <button class="icon-btn t-del" data-doc-delete="${esc(d.id)}" title="删除文档" aria-label="删除 ${esc(d.title)}">${icon('trash')}</button></div>`;
    }).join('') || `<div class="tree-empty">${q ? '没有匹配的文档' : '还没有文档。导入 .md / .txt，或把文件拖到这里。'}</div>`;
    // Re-rendering drops focus; keep it on the picked row so Ctrl+C/X/V and arrows keep working.
    const ae = document.activeElement;
    if (T.focusKey && (!ae || ae === document.body || ae.id === 'app' || !ae.isConnected)) focusRow(T.focusKey);
  }
  /* Folder that new / pasted / imported items go into. */
  function targetDir() {
    const keys = [...T.sel];
    const last = keys[keys.length - 1];
    if (last?.startsWith('f:')) return last.slice(2);
    if (last?.startsWith('d:')) return dirOf(docOf(last.slice(2))?.title);
    return S.doc ? dirOf(S.doc.title) : '';
  }
  /* Documents covered by a selection (folders expand to everything inside). */
  function docsFor(keys) {
    const out = new Map();
    keys.forEach((k) => {
      if (k.startsWith('d:')) { const d = docOf(k.slice(2)); if (d) out.set(d.id, d); }
      else { const f = k.slice(2); S.docs.filter((d) => dirOf(d.title) === f || dirOf(d.title).startsWith(f + '/')).forEach((d) => out.set(d.id, d)); }
    });
    return [...out.values()];
  }
  function uniqueTitle(title, taken) {
    if (!taken.has(title)) return title;
    const dir = dirOf(title), name = baseName(title), m = name.match(/^(.*?)(\.[^.]+)?$/);
    for (let i = 2; ; i++) { const t = joinPath(dir, `${m[1]} ${i}${m[2] || ''}`); if (!taken.has(t)) return t; }
  }
  async function retitle(d, title) {
    const v = await sync(api.patch(`/api/documents/${p(d.id)}`, { title: title.slice(0, 200) }));
    Object.assign(d, v.document || v);
  }
  /* Move a selection into `dest` keeping each item's relative layout (folder → folder/…). */
  async function moveKeys(keys, dest) {
    dest = normPath(dest);
    const taken = new Set(S.docs.map((d) => d.title));
    const plan = [];
    keys.forEach((k) => {
      if (k.startsWith('f:')) {
        const f = k.slice(2), parent = dirOf(f);
        if (dest === f || dest.startsWith(f + '/')) throw new Error('不能把文件夹移动到它自己里面');
        S.docs.filter((d) => dirOf(d.title) === f || dirOf(d.title).startsWith(f + '/'))
          .forEach((d) => plan.push([d, joinPath(dest, normPath(d.title).slice(parent ? parent.length + 1 : 0))]));
        const lf = localFolders();
        saveLocalFolders(lf.map((x) => (x === f || x.startsWith(f + '/') ? joinPath(dest, x.slice(parent ? parent.length + 1 : 0)) : x)));
      } else { const d = docOf(k.slice(2)); if (d) plan.push([d, joinPath(dest, baseName(d.title))]); }
    });
    let moved = 0;
    for (const [d, title] of plan) {
      if (normPath(d.title) === title) continue;
      taken.delete(d.title);
      const t = uniqueTitle(title, taken);
      taken.add(t);
      await retitle(d, t); moved++;
    }
    if (dest) openFolders().add(dest), saveOpen();
    return moved;
  }
  async function copyKeys(keys, dest) {
    dest = normPath(dest);
    const taken = new Set(S.docs.map((d) => d.title));
    const plan = [];
    keys.forEach((k) => {
      if (k.startsWith('f:')) {
        const f = k.slice(2), parent = dirOf(f);
        S.docs.filter((d) => dirOf(d.title) === f || dirOf(d.title).startsWith(f + '/'))
          .forEach((d) => plan.push([d, joinPath(dest, normPath(d.title).slice(parent ? parent.length + 1 : 0))]));
      } else { const d = docOf(k.slice(2)); if (d) plan.push([d, joinPath(dest, baseName(d.title))]); }
    });
    // Copies are text documents; a PDF/TeX original cannot be duplicated client-side.
    const skipped = plan.filter(([d]) => !TEXT_FORMATS.has(d.format)).length;
    for (const [d, title] of plan.filter(([d]) => TEXT_FORMATS.has(d.format))) {
      const text = (await sync(api.get(`/api/documents/${p(d.id)}/content`))).text || '';
      const t = uniqueTitle(title, taken); taken.add(t);
      await sync(api.post(`${projectPath()}/documents`, { title: t.slice(0, 200), text, derive_nodes: false }));
    }
    if (skipped) toast(`${skipped} 个 PDF/TeX 文档不能复制，已跳过`);
    if (dest) openFolders().add(dest), saveOpen();
    return plan.length - skipped;
  }
  function treeCopy(mode) {
    if (!T.sel.size) { toast('先选择文件或文件夹'); return; }
    T.clip = { mode, keys: [...T.sel] };
    renderTree();
    toast(`${mode === 'cut' ? '已剪切' : '已复制'} ${T.sel.size} 项，选择目标文件夹后粘贴（Ctrl+V）`);
  }
  async function treePaste() {
    if (!T.clip) { toast('剪贴板是空的'); return; }
    const dest = targetDir(), { mode, keys } = T.clip;
    if (mode === 'cut') {
      const n = await moveKeys(keys, dest);
      T.clip = null;
      await refreshDocs(); renderTree(); if (S.doc) renderReader(true);
      toast(n ? `已移动 ${n} 个文档到「${dest || '根目录'}」` : '位置没有变化');
    } else {
      const n = await copyKeys(keys, dest);
      await refreshDocs(); renderTree();
      toast(`已复制 ${n} 个文档到「${dest || '根目录'}」（副本只含正文，节点与解释不随副本复制）`);
    }
  }
  async function treeRename(key) {
    if (key.startsWith('d:')) {
      const d = docOf(key.slice(2));
      const name = await promptModal('重命名文档', '名称', baseName(d.title), 120);
      if (!name || name === baseName(d.title)) return;
      if (name.includes('/')) { toast('名称里不能包含 /'); return; }
      await retitle(d, uniqueTitle(joinPath(dirOf(d.title), name), new Set(S.docs.filter((x) => x !== d).map((x) => x.title))));
    } else {
      const f = key.slice(2);
      const name = await promptModal('重命名文件夹', '名称', baseName(f), 60);
      if (!name || name === baseName(f)) return;
      if (name.includes('/')) { toast('名称里不能包含 /'); return; }
      const to = joinPath(dirOf(f), name);
      for (const d of S.docs.filter((x) => dirOf(x.title) === f || dirOf(x.title).startsWith(f + '/'))) await retitle(d, to + normPath(d.title).slice(f.length));
      saveLocalFolders(localFolders().map((x) => (x === f || x.startsWith(f + '/') ? to + x.slice(f.length) : x)));
      if (openFolders().delete(f)) openFolders().add(to), saveOpen();
      T.sel = new Set(['f:' + to]);
    }
    await refreshDocs(); renderTree(); if (S.doc) renderReader(true);
  }
  async function treeDelete(keys = [...T.sel]) {
    const docs = docsFor(keys), folders = keys.filter((k) => k.startsWith('f:')).map((k) => k.slice(2));
    if (!docs.length && !folders.length) return;
    const nodes = S.nodes.filter((n) => docs.some((d) => d.id === n.document_id)).length;
    const what = [docs.length ? `${docs.length} 个文档` : '', folders.length ? `${folders.length} 个文件夹` : ''].filter(Boolean).join('、');
    if (!(await confirmModal('删除', `删除 ${what}${nodes ? `，以及其中的 ${nodes} 个节点、解释与连接` : ''}？此操作无法撤销。`, '删除'))) return;
    for (const d of docs) {
      await sync(api.del(`/api/documents/${p(d.id)}`));
      highlights = highlights.filter((h) => h.doc !== d.id);
      if (S.doc?.id === d.id) { S.doc = null; S.text = ''; }
    }
    saveHighlights();
    saveLocalFolders(localFolders().filter((x) => !folders.some((f) => x === f || x.startsWith(f + '/'))));
    T.sel.clear();
    await afterDocChange(S.doc?.id || null);
    toast(`已删除 ${what}`);
  }
  async function newFolder() {
    const name = await promptModal('新建文件夹', `位置：${targetDir() || '根目录'}`, '新建文件夹', 60);
    if (!name) return;
    if (name.includes('/')) { toast('名称里不能包含 /'); return; }
    const path = joinPath(targetDir(), name);
    saveLocalFolders([...localFolders(), path]);
    openFolders().add(path); saveOpen();
    T.sel = new Set(['f:' + path]);
    renderTree();
    toast('文件夹已创建；放入文档后会保存到服务器');
  }
  function pick(key, e) {
    const rows = treeRows().map((r) => r.key);
    if (e.shiftKey && T.anchor) {
      const a = rows.indexOf(T.anchor), b = rows.indexOf(key);
      if (a >= 0 && b >= 0) { T.sel = new Set(rows.slice(Math.min(a, b), Math.max(a, b) + 1)); renderTree(); return; }
    }
    if (e.ctrlKey || e.metaKey) { T.sel.has(key) ? T.sel.delete(key) : T.sel.add(key); }
    else T.sel = new Set([key]);
    T.anchor = key; T.focusKey = key;
    renderTree();
    focusRow(key);
  }
  const focusRow = (key) => treeEl.querySelector(`.t-item[data-key="${CSS.escape(key)}"] .t-row`)?.focus();

  /* Context menu on the tree (light, like the reader's). */
  let treeMenu = null;
  const hideTreeMenu = () => { treeMenu?.remove(); treeMenu = null; };
  function showTreeMenu(x, y) {
    hideTreeMenu();
    const one = T.sel.size === 1 ? [...T.sel][0] : null;
    const items = [
      ['new-file', '新建文档', ''], ['new-folder', '新建文件夹', ''], ['sep'],
      ['cut', '剪切', 'Ctrl X', !T.sel.size], ['copy', '复制', 'Ctrl C', !T.sel.size], ['paste', '粘贴', 'Ctrl V', !T.clip], ['sep'],
      ['rename', '重命名', 'F2', !one], ['delete', '删除', 'Del', !T.sel.size],
    ];
    treeMenu = document.createElement('div');
    treeMenu.className = 'menu ctx-menu'; treeMenu.setAttribute('role', 'menu');
    treeMenu.innerHTML = items.map(([k, l, kb, dis]) => (k === 'sep' ? '<hr>' : `<button class="menu__item" role="menuitem" data-tk="${k}" ${dis ? 'disabled' : ''}><span>${l}</span><span class="mono muted">${kb}</span></button>`)).join('');
    document.body.appendChild(treeMenu);
    treeMenu.style.left = Math.min(x, innerWidth - treeMenu.offsetWidth - 8) + 'px';
    treeMenu.style.top = Math.min(y, innerHeight - treeMenu.offsetHeight - 8) + 'px';
    treeMenu.addEventListener('click', action(async (e) => {
      const k = e.target.closest('[data-tk]')?.dataset.tk;
      if (!k) return;
      hideTreeMenu();
      await treeCommand(k, one);
    }));
    $('.menu__item:not(:disabled)', treeMenu)?.focus();
  }
  async function treeCommand(k, one = T.sel.size === 1 ? [...T.sel][0] : null) {
    if (k === 'new-file') newDocument();
    if (k === 'new-folder') await newFolder();
    if (k === 'cut' || k === 'copy') treeCopy(k);
    if (k === 'paste') await treePaste();
    if (k === 'rename' && one) await treeRename(one);
    if (k === 'delete') await treeDelete();
  }
  treeEl.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const item = e.target.closest('.t-item');
    if (item && !T.sel.has(item.dataset.key)) { T.sel = new Set([item.dataset.key]); T.anchor = item.dataset.key; renderTree(); }
    if (!item) { T.sel.clear(); renderTree(); }
    showTreeMenu(e.clientX, e.clientY);
  });
  document.addEventListener('mousedown', (e) => { if (treeMenu && !treeMenu.contains(e.target)) hideTreeMenu(); });

  treeEl.addEventListener('click', action(async (e) => {
    const del = e.target.closest('[data-doc-delete]');
    if (del) return treeDelete(['d:' + del.dataset.docDelete]);
    const item = e.target.closest('.t-item');
    if (!item) { T.sel.clear(); renderTree(); return; }
    const key = item.dataset.key, multi = e.ctrlKey || e.metaKey || e.shiftKey;
    pick(key, e);
    if (multi) return;
    if (key.startsWith('f:')) { const f = key.slice(2), o = openFolders(); o.has(f) ? o.delete(f) : o.add(f); saveOpen(); renderTree(); focusRow(key); }
    else if (key.slice(2) !== S.doc?.id) await openDocument(key.slice(2));
  }));
  treeEl.addEventListener('keydown', (e) => {
    const rows = $$('.t-row', treeEl), i = rows.indexOf(document.activeElement);
    const key = document.activeElement?.closest?.('.t-item')?.dataset.key;
    const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (i < 0) return;
      e.preventDefault();
      const next = rows[e.key === 'ArrowDown' ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1)];
      next.focus();
      if (e.shiftKey) pick(next.closest('.t-item').dataset.key, { shiftKey: true }), next.closest('.t-item') && focusRow(next.closest('.t-item').dataset.key);
      return;
    }
    if (mod && ['c', 'x', 'v', 'a'].includes(k)) {
      e.preventDefault(); e.stopPropagation();
      if (k === 'a') { T.sel = new Set(treeRows().map((r) => r.key)); renderTree(); return; }
      if (k === 'v') { treePaste().catch(fail); return; }
      if (key && !T.sel.has(key)) T.sel = new Set([key]);
      treeCopy(k === 'x' ? 'cut' : 'copy');
      return;
    }
    if (e.key === 'F2' && key) { e.preventDefault(); treeRename(key).catch(fail); }
    if (e.key === 'Delete' && (T.sel.size || key)) { e.preventDefault(); if (!T.sel.size) T.sel.add(key); treeDelete().catch(fail); }
    if (e.key === 'Escape' && T.clip) { T.clip = null; renderTree(); }
  });
  /* Drag rows onto a folder (or the empty area = root) to move them. */
  let treeDrag = null;
  treeEl.addEventListener('dragstart', (e) => {
    const item = e.target.closest('.t-item');
    if (!item) return;
    if (!T.sel.has(item.dataset.key)) {
      // Never re-render here: replacing the dragged element cancels the drag.
      T.sel = new Set([item.dataset.key]);
      $$('.t-item', treeEl).forEach((x) => x.classList.toggle('picked', x === item));
    }
    treeDrag = [...T.sel];
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/x-tutorlm-tree', '1');
  });
  const dropDir = (e) => {
    const item = e.target.closest('.t-item');
    if (!item) return '';
    const key = item.dataset.key;
    return key.startsWith('f:') ? key.slice(2) : dirOf(docOf(key.slice(2))?.title);
  };
  treeEl.addEventListener('dragover', (e) => {
    if (!treeDrag) return;
    e.preventDefault();
    $$('.t-item.drop', treeEl).forEach((x) => x.classList.remove('drop'));
    e.target.closest('.t-item')?.classList.add('drop');
  });
  treeEl.addEventListener('dragleave', (e) => { if (!treeEl.contains(e.relatedTarget)) $$('.t-item.drop', treeEl).forEach((x) => x.classList.remove('drop')); });
  treeEl.addEventListener('drop', (e) => {
    if (!treeDrag) return;
    e.preventDefault(); e.stopPropagation();
    const keys = treeDrag, dest = dropDir(e);
    treeDrag = null;
    moveKeys(keys, dest).then(async (n) => { await refreshDocs(); renderTree(); if (n) toast(`已移动 ${n} 个文档到「${dest || '根目录'}」`); }).catch(fail);
  });
  treeEl.addEventListener('dragend', () => { treeDrag = null; $$('.t-item.drop', treeEl).forEach((x) => x.classList.remove('drop')); });
  async function refreshDocs() { S.docs = (await sync(api.get(`${projectPath()}/documents`))).items || []; }
  /* md/txt: whole text from /content. pdf/tex: extracted text from /text, queuing
     one extraction run when none exists yet. Nothing is substituted when the
     server has no text: the reader shows why. */
  const TEXT_FORMATS = new Set(['md', 'txt']);
  async function documentText(doc) {
    if (TEXT_FORMATS.has(doc.format)) return { text: (await api.get(`/api/documents/${p(doc.id)}/content`)).text || '' };
    const read = async () => { const v = await api.get(`/api/documents/${p(doc.id)}/text`); return { text: v.text || '', version: v.extractor_version || null }; };
    try { return await read(); }
    catch (e) { if (e.code !== 'content_not_available') throw e; }
    const unavailable = () => Object.assign(new Error(doc.format === 'tex'
      ? '这个实例暂不支持 TeX 文本抽取（需要编译沙箱），可以下载原文件查看' : '无法从这个文件抽取文本'), { code: 'content_not_available' });
    // An extraction that already ended unsupported/failed for this revision is final; don't queue another run.
    const past = (await api.get(`/api/documents/${p(doc.id)}/extractions?revision_id=${p(doc.current_revision_id || '')}`)).items || [];
    if (past.some((x) => x.status === 'unsupported' || x.status === 'failed')) throw unavailable();
    const result = await api.follow(await api.post(`/api/documents/${p(doc.id)}/extract`, {}));
    if (result?.extraction?.status && result.extraction.status !== 'ready') throw unavailable();
    return read();
  }
  async function openDocument(id) {
    const current = ++revision;
    const detail = await sync(api.get(`/api/documents/${p(id)}`));
    const doc = detail.document || detail;
    let text, textVersion = null;
    try { ({ text, version: textVersion } = await sync(documentText(doc))); }
    catch (e) {
      if (e.code !== 'content_not_available') throw e;
      text = `> ${e.message}`;
      // A PDF still renders from its original; only the text views lack content.
      if (doc.format !== 'pdf') toast(e.message, { ms: 6000 });
    }
    if (current !== revision) return;
    if (S.doc && S.doc.id !== id) S.views[S.doc.id] = { ...S.view };
    S.doc = doc; S.text = text; S.textVersion = textVersion;
    // View mode is remembered per document; a PDF opens in the real PDF view.
    S.mode = ['md', 'raw', 'pdf'].includes(S.modes[doc.id]) ? S.modes[doc.id] : doc.format === 'pdf' ? 'pdf' : 'md';
    if (S.selected && !inDoc(nodeOf(S.selected))) S.selected = null;
    const saved = S.views[S.doc.id];
    S.view = saved && Number.isFinite(saved.z) ? { ...saved } : { x: 16, y: 8, z: 0.85 };
    closeFind(); renderTree(); renderReader(); relayout();
    if (!saved && docNodes().length) fitView();
    savePrefs();
  }
  async function afterDocChange(docId) {
    await refreshDocs(); await refreshGraph();
    if (docId) await openDocument(docId); else { renderTree(); renderReader(); }
  }
  async function importFiles(files) {
    const max = api.limits?.max_bytes_per_file || 2 * 1024 * 1024;
    const ok = [...files].filter((f) => /\.(md|markdown|txt|pdf|tex|zip)$/i.test(f.name));
    if (!ok.length) { toast('只支持 .md / .txt / .pdf / .tex（或 TeX 工程 .zip）文件'); return; }
    if (ok.length < files.length) toast(`已跳过 ${files.length - ok.length} 个不支持的文件`);
    let last = null;
    for (const f of ok) {
      if (f.size > max) { toast(`${f.name} 超过 ${(max / 1024 / 1024).toFixed(1)} MB，已跳过`); continue; }
      const form = new FormData(); form.append('file', f, f.name); form.append('derive_nodes', 'false');
      try {
        last = (await sync(api.post(`${projectPath()}/documents/upload`, form))).document;
        // Upload names the document after the file; the folder prefix is a title rename.
        const dir = targetDir();
        if (dir) last = (await sync(api.patch(`/api/documents/${p(last.id)}`, { title: joinPath(dir, last.title).slice(0, 200) }))).document || last;
      } catch (e) { fail(e); }
    }
    if (last) { await afterDocChange(last.id); toast('已导入文档，圈选正文后可提取或解释知识节点'); }
  }
  async function createDocument(title, text) {
    const value = await sync(api.post(`${projectPath()}/documents`, { title: title.slice(0, 200), text, derive_nodes: false }));
    await afterDocChange((value.document || value).id);
    toast('已保存为文档「' + clip(title, 20) + '」');
  }
  function newDocument() {
    modal({ title: '新建文档', wide: true,
      body: `<div style="display:grid;gap:12px"><label class="field"><span class="label">标题</span><input class="input" id="nd-title" maxlength="200" placeholder="例如：第三章笔记"></label>
        <label class="field"><span class="label">正文（Markdown / 纯文本）</span><textarea class="input nd-text" id="nd-text" rows="14" placeholder="# 标题&#10;&#10;粘贴或输入正文。保存后标题会自动生成知识节点。"></textarea></label>
        <p class="gate__msg" id="nd-msg" role="alert"></p></div>`,
      actions: [{ label: '取消', run: (c) => c() }, { label: '保存', primary: true, run: async (c, r, b) => {
        const title = $('#nd-title', r).value.trim(), text = $('#nd-text', r).value;
        if (!text.trim()) { $('#nd-msg', r).textContent = '正文不能为空'; return; }
        b.disabled = true;
        try { c(); await createDocument(joinPath(targetDir(), (title || guessTitle(text)).replace(/\//g, '／')), text); } catch (e) { fail(e); }
      } }] });
  }
  async function deleteDocument(id) {
    const d = docOf(id);
    if (!d) return;
    const count = S.nodes.filter((n) => n.document_id === id).length;
    if (!(await confirmModal('删除文档', `删除「${esc(d.title)}」及其 ${count} 个节点、解释与连接？此操作无法撤销。`, '删除'))) return;
    await sync(api.del(`/api/documents/${p(id)}`));
    highlights = highlights.filter((h) => h.doc !== id); saveHighlights();
    if (S.doc?.id === id) { S.doc = null; S.text = ''; }
    await afterDocChange(S.doc?.id || null);
    toast('已删除文档');
  }
  filterEl.addEventListener('input', renderTree);
  filterEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); filterEl.value = ''; renderTree(); filterEl.blur(); }
    if (e.key === 'Enter') { const first = $('[data-doc]', treeEl); if (first) openDocument(first.dataset.doc).catch(fail); }
  });
  $('#file-input').addEventListener('change', (e) => { importFiles(e.target.files).catch(fail).finally(() => { e.target.value = ''; }); });
  let dragDepth = 0;
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  window.addEventListener('dragenter', (e) => { if (!hasFiles(e) || !S.connected) return; dragDepth++; app.classList.remove('no-explorer', 'focus'); $('#dropzone').hidden = false; });
  window.addEventListener('dragleave', (e) => { if (!hasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $('#dropzone').hidden = true; } });
  window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; $('#dropzone').hidden = true;
    if (S.connected) importFiles(e.dataTransfer.files).catch(fail);
  });

  /* ================= Projects ================= */
  async function refreshGraph() {
    const graph = await sync(api.get(`${projectPath()}/graph`));
    S.nodes = graph.nodes || []; S.edges = graph.edges || [];
    if (!nodeOf(S.selected)) S.selected = null;
    relayout(); refreshMarks(); renderTree();
  }
  /* Render once to measure real card heights, then place nodes without a stored position. */
  function relayout() {
    renderCanvas();
    S.layout = { ...S.layout, ...computeLayout(false) };
    renderCanvas();
  }
  async function loadHistory() {
    const items = (await sync(api.get(`${projectPath()}/explanations`))).items || [];
    S.history = {};
    items.forEach((h) => (S.history[h.node_id] ||= []).push(h));
    Object.values(S.history).forEach((l) => l.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()));
  }
  async function openProject(id) {
    ++revision;
    S.project = S.projects.find((x) => x.id === id) || S.projects[0];
    if (api.managed && api.lease?.workspace_id) {
      const { workspace } = await sync(api.get(`/control/workspaces/${p(api.lease.workspace_id)}`, { auth: false }));
      S.project = { ...S.project, title: workspace.title };
    }
    const prefs = readJSON(prefKey(), {});
    highlights = readJSON(hlKey(), []);
    S.doc = null; S.text = ''; S.selected = prefs.node || null; S.history = {};
    S.modes = prefs.modes && typeof prefs.modes === 'object' ? prefs.modes : {};
    S.views = prefs.views && typeof prefs.views === 'object' ? prefs.views : {};
    S.view = { x: 16, y: 8, z: 0.85 };
    $('#ws-name').textContent = S.project.title;
    T.open = null; T.sel.clear(); T.clip = null;
    closeFind(); hideAsk();
    await refreshDocs();
    await loadHistory();
    await refreshGraph();
    const docId = docOf(prefs.doc)?.id || S.docs[0]?.id;
    if (docId) await openDocument(docId); else { renderTree(); renderReader(); }
    $('#usage-docs').textContent = `${S.docs.length}${api.limits?.max_documents_per_session ? ' / ' + api.limits.max_documents_per_session : ''}`;
  }
  async function loadProjects(selectId) {
    // One instance serves exactly one workspace-bound project; it always exists.
    S.projects = (await sync(api.get('/api/projects'))).items || [];
    if (!S.projects.length) throw new Error('实例没有绑定工作区');
    // A redeemed share code left a target project id for this one load.
    const pending = api.takePendingProject?.();
    const last = localStorage.getItem('tlm.last-project');
    const pick = [selectId, pending, last].find((id) => id && S.projects.some((x) => x.id === id)) || S.projects[0].id;
    if (pending && pending === pick) localStorage.setItem('tlm.last-project', pending);
    await openProject(pick);
  }
  async function switchProject(id) {
    if (Object.keys(S.runs).some(running)) { toast('有任务正在生成，请等待完成后再切换'); return; }
    localStorage.setItem('tlm.last-project', id);
    await openProject(id);
    toast('已切换到「' + S.project.title + '」');
  }

  async function openWorkspace(workspaceId) {
    if (Object.keys(S.runs).some(running)) { toast('有任务正在生成，请等待完成后再切换'); return; }
    const policy = await api.accessPolicy();
    const code = await promptModal('打开工作区', policy.invitation_only ? '系统邀请码（必填）' : '系统邀请码（可选）', '', 128, !policy.invitation_only);
    if (code === null) return;
    await api.enter(code, { workspaceId });
    location.reload();
  }

  /* ================= Menus ================= */
  const MENUS = { 'ws-btn': 'ws-menu', 'usage-btn': 'usage-menu', 'avatar-btn': 'avatar-menu', 'outline-btn': 'outline-menu', 'export-caret': 'export-menu' };
  function closeMenus(except) {
    Object.entries(MENUS).forEach(([b, m]) => {
      if (b === except || !$('#' + b)) return;
      $('#' + m).hidden = true; $('#' + b).setAttribute('aria-expanded', 'false');
    });
  }
  function toggleMenu(btnId) {
    const menu = $('#' + MENUS[btnId]);
    const open = menu.hidden;
    closeMenus(btnId);
    if (open) {
      if (btnId === 'ws-btn') paintWsMenu();
      if (btnId === 'usage-btn') paintUsage().catch(fail);
      if (btnId === 'outline-btn') paintOutline();
      if (btnId === 'avatar-btn') $('#avatar-lease').textContent = api.session?.id || (api.runtime?.mode === 'local' ? '本地模式' : '—');
    }
    menu.hidden = !open;
    $('#' + btnId).setAttribute('aria-expanded', String(open));
    if (open) $('.menu__item', menu)?.focus();
  }
  Object.keys(MENUS).forEach((b) => $('#' + b)?.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(b); }));
  document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('.menu-anchor')) closeMenus();
    if (!askEl.hidden && !askEl.contains(e.target)) hideAsk();
    if (selBar && !selBar.contains(e.target)) hideSelBar();
  });
  document.addEventListener('keydown', (e) => {
    const menu = e.target.closest?.('.menu');
    if (!menu || !/^Arrow(Up|Down)$/.test(e.key)) return;
    const items = $$('.menu__item:not(:disabled)', menu), i = items.indexOf(document.activeElement);
    e.preventDefault();
    items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
  });
  async function paintWsMenu() {
    if (api.managed) {
      try {
        const { items } = await api.listWorkspaces();
        $('#ws-menu').innerHTML = items.map((x) => `<button class="menu__item" role="menuitemradio" aria-checked="${x.workspace_id === api.lease?.workspace_id}" data-workspace="${esc(x.workspace_id)}">${esc(x.title)}</button>`).join('') +
          '<hr><button class="menu__item" data-act="ws-new">新建工作区…</button><button class="menu__item" data-act="ws-rename">重命名当前工作区</button><button class="menu__item" data-act="ws-delete">清空当前工作区</button>';
      } catch (error) { toast(describe(error)); }
      return;
    }
    const items = S.projects.map((x) => `<button class="menu__item" role="menuitemradio" aria-checked="${x.id === S.project?.id}" data-project="${esc(x.id)}"><span>${esc(x.title)}</span></button>`).join('');
    $('#ws-menu').innerHTML = `<div class="menu__section" style="padding:8px 12px"><span class="label">工作区</span></div>${items}<hr>
      <button class="menu__item" role="menuitem" data-act="ws-new" title="由控制面管理">新建工作区…</button>
      <button class="menu__item" role="menuitem" data-act="ws-rename">重命名当前工作区</button>
      <button class="menu__item" role="menuitem" data-act="ws-delete">清空当前工作区</button>`;
  }
  $('#ws-menu').addEventListener('click', action(async (e) => {
    const workspace = e.target.closest('[data-workspace]');
    if (workspace) { closeMenus(); if (workspace.dataset.workspace !== api.lease?.workspace_id) await openWorkspace(workspace.dataset.workspace); return; }
    const item = e.target.closest('[data-project]');
    if (item) { closeMenus(); if (item.dataset.project !== S.project.id) await switchProject(item.dataset.project); }
  }));
  async function paintUsage() {
    const runs = (await api.get('/api/runs')).items || [];
    const cap = api.limits?.max_runs_per_session;
    $('#usage-big').textContent = runs.length;
    $('#usage-cap').textContent = cap ? ` / ${cap} 次` : ' 次';
    $('#usage-meter').style.width = cap ? Math.min(100, (runs.length / cap) * 100) + '%' : '0%';
    $('#usage-v').textContent = String(runs.length);
    $('#usage-docs').textContent = `${S.docs.length}${api.limits?.max_documents_per_session ? ' / ' + api.limits.max_documents_per_session : ''}`;
    $('#usage-session').textContent = api.session?.id || (api.runtime?.mode === 'local' ? '本地模式（无会话）' : '—');
    $('#usage-lease').textContent = api.lease?.lease_id || '独立实例';
  }
  function paintOutline() {
    if (isPdfView()) { paintPdfOutline().catch(fail); return; }
    const hs = $$('h1[data-b], h2[data-b], h3[data-b], h4[data-b], h5[data-b], h6[data-b]', sheet);
    $('#outline-menu').innerHTML = hs.length
      ? `<div class="menu__section" style="padding:8px 12px"><span class="label">大纲</span></div>` + hs.map((h) =>
        `<button class="menu__item ${h.tagName === 'H3' ? 'lvl3' : ''}" role="menuitem" data-goto="${h.dataset.b}">${esc(clip(collapse(h.textContent), 40))}</button>`).join('')
      : `<div class="menu__section muted" style="font-size:12px">${S.mode === 'raw' ? 'RAW 视图下不显示大纲' : '此文档没有标题'}</div>`;
  }
  $('#outline-menu').addEventListener('click', (e) => {
    const pg = e.target.closest('[data-page]');
    if (pg) { closeMenus(); pdfv?.view.goTo(Number(pg.dataset.page)); return; }
    const g = e.target.closest('[data-goto]');
    if (!g) return;
    closeMenus();
    const h = sheet.querySelector(`[data-b="${g.dataset.goto}"]`);
    if (h) { h.scrollIntoView({ block: 'start' }); h.classList.remove('flash'); void h.offsetWidth; h.classList.add('flash'); }
  });

  /* ================= Share =================
     Ask the server for a share code bound to the CURRENT project. The receiver
     redeems that code and gets an independent deep copy, so editing one never
     touches the other. When the server has no share endpoint yet we say so
     plainly instead of inventing a fake code. */
  async function openShare() {
    if (!S.project) { toast('请先打开一个工作区'); return; }
    modal({
      title: '分享这份笔记',
      body: `<div id="share-body"><p class="muted small">正在生成分享码…</p></div>`,
      actions: [{ label: '关闭', run: (c) => c() }],
      onOpen: async (root) => {
        const body = $('#share-body', root);
        if (!body) return;
        try {
          const res = await api.createShare();
          const code = res?.code || res?.share_code || '';
          if (!code) throw new Error('服务器未返回分享码');
          const expires = res.invitation?.expires_at ? new Date(res.invitation.expires_at * 1000) : null;
          body.innerHTML = `
            <label class="field"><span class="label">分享码</span>
              <div style="display:flex;gap:8px">
                <input class="input mono" id="share-code" readonly value="${esc(code)}">
                <button class="btn btn--primary" id="share-copy" type="button">${icon('copy')}复制</button>
              </div>
            </label>
            <button class="btn" id="share-revoke" type="button" style="margin-top:16px">撤销分享码</button>
            ${expires && !isNaN(expires) ? `<p class="muted small" style="margin-top:12px">有效期至 ${esc(expires.toLocaleString())}</p>` : ''}`;
          $('#share-code', root).addEventListener('focus', (e) => e.target.select());
          $('#share-copy', root).onclick = action(async () => { await copyText(code); toast('已复制分享码'); });
          $('#share-revoke', root).onclick = action(async () => {
            await api.revokeShare(res.invitation.invitation_id);
            body.innerHTML = '<p>分享码已撤销</p>';
          });
        } catch (e) {
          body.innerHTML = `<p class="error">无法生成分享码：${esc(describe(e))}</p>`;
        }
      },
    });
  }
  $('#share-btn')?.addEventListener('click', action(openShare));

  /* ================= Export ================= */
  function exportMd() {
    const nodes = S.doc ? S.nodes.filter((n) => n.document_id === S.doc.id) : S.nodes;
    const parts = [S.doc ? S.text.trimEnd() : `# ${S.project.title}`, '', '---', '', '## 知识节点笔记', ''];
    treeOrder().filter((n) => nodes.includes(n)).forEach((n) => {
      parts.push(`### ${n.title}（${KIND[n.kind] || n.kind} · ${depthLabel(n)}${meta(n).status === 'mastered' ? ' · 已掌握' : ''}）`);
      if (meta(n).source?.quote) parts.push(`> ${collapse(meta(n).source.quote)}`, '');
      if (n.content) parts.push(n.content.trim(), '');
      (S.history[n.id] || []).forEach((h) => parts.push(`**问** ${h.question}`, '', h.answer.trim(), ''));
    });
    if (!nodes.length) parts.push('（本文档暂无节点）');
    download((S.doc ? baseName(S.doc.title).replace(/\.[^.]+$/, '') : S.project.title) + '.notes.md', parts.join('\n'));
    toast('已导出 Markdown（服务器上的当前数据）');
  }
  function exportJson() {
    download('knowledge_graph.json', JSON.stringify({
      project: { id: S.project.id, title: S.project.title }, exported_at: new Date().toISOString(),
      nodes: S.nodes, edges: S.edges, explanations: Object.values(S.history).flat(),
    }, null, 2), 'application/json');
    toast('已导出知识图 JSON');
  }

  /* ================= View controls ================= */
  function setFocusMode(on) {
    app.classList.toggle('focus', on);
    const b = $('#focus-btn');
    b.setAttribute('aria-pressed', String(on));
    b.title = on ? '退出专注 (Esc)' : '专注阅读';
    b.innerHTML = icon(on ? 'shrink' : 'expand');
    setTimeout(() => { drawConnector(); updateStatus(); }, 0);
  }
  function togglePane(cls, btnAct) {
    setFocusMode(false);
    const on = app.classList.toggle(cls);
    $(`[data-act="${btnAct}"]`).setAttribute('aria-pressed', String(!on));
    setTimeout(() => { drawEdges(); drawConnector(); updateStatus(); }, 0);
  }
  function setTool(t) {
    if (t === 'visual' && (!isPdfView() || !api.runtime?.learning_features?.visual_selection)) { toast('请打开 PDF；旧会话需保存工作后重新进入以启用图像框选。'); return; }
    S.tool = t;
    $$('[data-tool]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tool === t)));
    $('#reader').classList.toggle('tool-highlight', t === 'highlight');
    $('#reader').classList.toggle('tool-draw', t === 'draw');
    $('#reader').classList.toggle('tool-visual', t === 'visual');
    $('#drawbar').hidden = t !== 'draw';
    if (t === 'draw') { hideAsk(); hideSelBar(); getSelection().removeAllRanges(); paintDrawbar(); paintInk(); }
    if (t === 'highlight') toast('高亮模式：选中文字即高亮（仅保存在本浏览器），点击已有高亮可移除');
  }
  function setView(v) { if (!S.doc) return; S.mode = v; S.modes[S.doc.id] = v; savePrefs(); renderReader(); }

  async function endTrial() {
    const local = api.runtime?.mode === 'local' && !api.managed;
    const ok = await confirmModal(local ? '断开本地会话' : '结束试用',
      local ? '数据会保留在本机，返回首页即可。' : api.lease?.workspace_id ? '结束后释放计算，项目数据保留；项目超过 4 小时不使用后清理。' : '临时工作区数据会被删除，请先导出。',
      local ? '返回首页' : '结束试用');
    if (!ok) return;
    if (!local) await sync(api.release());
    location.href = 'index.html';
  }
  function showShortcuts() {
    const rows = [['命令面板', 'Ctrl + K'], ['筛选文档', 'Ctrl + P'], ['在文档中查找', 'Ctrl + F'], ['对选中内容提问', 'Alt + E'],
      ['删除选中节点', 'Delete'], ['画布缩放', 'Ctrl + 滚轮'], ['画布平移', '拖动空白处 / 滚轮'], ['关闭浮层 / 退出专注', 'Esc']];
    modal({ title: '键盘快捷键', body: `<table class="kbd-table">${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join('')}</table>`,
      actions: [{ label: '知道了', primary: true, run: (c) => c() }] });
  }
  async function showIdentity() {
    closeMenus();
    if (!api.managed) { toast('独立实例没有控制面用户身份'); return; }
    const { user } = await api.get('/control/users/me', { auth: false });
    const saved = api.recovery;
    const recovery = saved?.user?.user_id === user.user_id ? saved.recovery_token : '';
    modal({ title: '用户与恢复凭据', body: `<label class="field"><span class="label">用户 UUID</span><input class="input mono" readonly value="${esc(user.user_id)}"></label>
      ${recovery ? `<label class="field"><span class="label">恢复凭据</span><input class="input mono" readonly value="${esc(recovery)}"></label>` : '<p>恢复凭据仅在创建用户时显示，请使用已保存的凭据恢复登录。</p>'}`,
      actions: [{ label: '关闭', run: c => c() }], onOpen: root => $$('input', root).forEach(input => input.addEventListener('focus', () => input.select())) });
  }
  const ACTIONS = {
    'identity': showIdentity,
    'import': () => $('#file-input').click(),
    'new-file': newDocument,
    'new-folder': () => newFolder(),
    'export-md': () => { closeMenus(); exportMd(); },
    'export-json': () => { closeMenus(); exportJson(); },
    'print': () => { closeMenus(); if (S.mode === 'raw') setView('md'); setTimeout(() => window.print(), 50); },
    'ink-undo': () => inkUndoLast(), 'ink-clear': () => inkClear(),
    'find-prev': () => stepFind(-1), 'find-next': () => stepFind(1), 'find-close': closeFind,
    'zoom-in': () => zoomAt(...surfaceCenter(), S.view.z * 1.2),
    'zoom-out': () => zoomAt(...surfaceCenter(), S.view.z / 1.2),
    'zoom-reset': () => zoomAt(...surfaceCenter(), 1),
    'fit': fitView, 'auto-layout': autoLayout, 'ai-relations': organizeRelations,
    'toggle-explorer': () => togglePane('no-explorer', 'toggle-explorer'),
    'toggle-canvas': () => togglePane('no-canvas', 'toggle-canvas'),
    'toggle-follow': () => { setPref('follow', !prefs.follow); toast(prefs.follow ? '知识图将跟随阅读区滚动' : '已关闭跟随滚动'); },
    'end-trial': () => { closeMenus(); return endTrial(); },
    'shortcuts': () => { closeMenus(); showShortcuts(); },
    'ws-new': async () => {
      closeMenus();
      if (!api.managed) { toast('独立实例只服务一个工作区'); return; }
      const title = await promptModal('新建工作区', '名称');
      if (!title) return;
      const { workspace } = await api.createWorkspace(title);
      await openWorkspace(workspace.workspace_id);
    },
    'ws-rename': async () => {
      closeMenus();
      const title = await promptModal('重命名工作区', '名称', S.project.title);
      if (!title || title === S.project.title) return;
      Object.assign(S.project, await sync(api.patch(projectPath(), { title })));
      $('#ws-name').textContent = S.project.title;
    },
    'ws-delete': async () => {
      closeMenus();
      if (Object.keys(S.runs).some(running)) { toast('有任务正在生成，请等待完成后再清空'); return; }
      // DELETE /api/projects/{id} purges the project's content; the project itself stays.
      if (!(await confirmModal('清空工作区', `「${esc(S.project.title)}」中的全部文档、节点、解释和串讲会被删除，无法恢复。`, '清空'))) return;
      await sync(api.del(projectPath()));
      localStorage.removeItem(prefKey()); localStorage.removeItem(hlKey());
      await loadProjects(S.project.id);
      toast('已清空工作区');
    },
  };
  document.addEventListener('click', action(async (e) => {
    const el = e.target.closest('[data-act]');
    if (!el || el.disabled || el.closest('#ask, #inspector, #nodes, #modal-host')) return;
    const fn = ACTIONS[el.dataset.act];
    if (fn) { if (!S.connected && !['end-trial', 'shortcuts'].includes(el.dataset.act)) return; await fn(); }
  }));
  $$('.seg [data-view]').forEach((b) => b.addEventListener('click', () => { if (!b.disabled) setView(b.dataset.view); }));
  $$('[data-tool]').forEach((b) => b.addEventListener('click', () => setTool(b.dataset.tool)));
  let visualDrag = null;
  scroller.addEventListener('pointerdown', (event) => {
    if (S.tool !== 'visual' || !pdfv || event.button !== 0) return;
    const page = event.target.closest('.pdfv-page');
    if (!page) return;
    event.preventDefault(); hideAsk(); getSelection().removeAllRanges();
    const box = document.createElement('div'); box.className = 'visual-selection'; page.appendChild(box);
    visualDrag = { page, box, x: event.clientX, y: event.clientY, documentId: S.doc.id, revisionId: S.doc.current_revision_id };
    scroller.setPointerCapture(event.pointerId);
  });
  scroller.addEventListener('pointermove', (event) => {
    if (!visualDrag) return;
    const { page, box, x, y } = visualDrag, r = page.getBoundingClientRect();
    const x2 = Math.max(r.left, Math.min(r.right, event.clientX)), y2 = Math.max(r.top, Math.min(r.bottom, event.clientY));
    Object.assign(box.style, { left: Math.min(x, x2) - r.left + 'px', top: Math.min(y, y2) - r.top + 'px', width: Math.abs(x2 - x) + 'px', height: Math.abs(y2 - y) + 'px' });
  });
  scroller.addEventListener('pointercancel', () => { visualDrag?.box.remove(); visualDrag = null; });
  scroller.addEventListener('pointerup', action(async () => {
    if (!visualDrag) return;
    const drag = visualDrag; visualDrag = null;
    const rect = drag.box.getBoundingClientRect(); drag.box.remove();
    if (rect.width < 8 || rect.height < 8) return;
    const cropped = await pdfv.view.crop(Number(drag.page.dataset.page), rect);
    setTool('select');
    openAsk({ kind: 'visual', ...cropped, documentId: drag.documentId, revisionId: drag.revisionId,
      quote: `第 ${drag.page.dataset.page} 页图像选区`, label: `第 ${drag.page.dataset.page} 页` }, rect);
  }));
  $('#visual-file-button').addEventListener('click', () => $('#visual-file').click());
  $('#visual-file').addEventListener('change', action(async (event) => {
    const file = event.target.files[0]; event.target.value = '';
    if (!file) return;
    if (!api.runtime?.learning_features?.visual_selection) throw new Error('当前会话不支持图像，请保存后重新进入。');
    if (file.size > 8 * 1024 * 1024) throw new Error('请选择小于 8 MiB 的图片。');
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas'); canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height); bitmap.close();
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob || blob.size > 4 * 1024 * 1024) throw new Error('图像过大，请裁剪后重试。');
    const { document: doc } = await api.post(`${projectPath()}/documents`, { title: file.name, text: '# ' + file.name, derive_nodes: false });
    S.docs.push(doc); await openDocument(doc.id);
    openAsk({ kind: 'visual', blob, documentId: doc.id, revisionId: doc.current_revision_id, quote: '图像：' + file.name, label: file.name }, { left: 24, top: 80, bottom: 100 });
  }));
  $('#focus-btn').addEventListener('click', () => setFocusMode(!app.classList.contains('focus')));
  $('#find-btn').addEventListener('click', () => openPalette());
  scroller.addEventListener('scroll', () => { updateStatus(); drawConnector(); followScroll(); }, { passive: true });
  window.addEventListener('resize', () => {
    placeInspector();
    if (innerWidth <= 700 && nodeOf(S.selected)) ensureVisible(nodeOf(S.selected));
    drawConnector(); updateStatus(); if (!isPdfView()) paintInk();
  });
  $('#prefs-btn')?.addEventListener('click', () => openPrefs());
  // Right-click on a reader selection opens the same ask menu.
  sheet.addEventListener('contextmenu', (e) => {
    if (S.modal) return;
    const s = readerSelection();
    if (!s) return;
    e.preventDefault();
    openAsk(s.origin, { left: e.clientX, right: e.clientX, top: e.clientY - 4, bottom: e.clientY + 4 });
  });

  /* ================= Command palette ================= */
  const palette = $('#palette'), palInput = $('#palette-input'), palList = $('#palette-list');
  let palItems = [], palIdx = 0;
  function paletteSource() {
    const cmds = [
      ['切换到 MD 视图', () => setView('md')], ['切换到 RAW 视图', () => setView('raw')], ['切换到版式视图', () => setView('pdf')],
      ['在文档中查找', () => openFind('')], ['导出文档 + 笔记 (.md)', exportMd], ['导出知识图 (.json)', exportJson],
      ['生成串讲', openNarration], ['适应画布', fitView], ['按层级排列节点', autoLayout],
      ['专注阅读', () => setFocusMode(!app.classList.contains('focus'))], ['显示 / 隐藏资源栏', ACTIONS['toggle-explorer']],
      ['显示 / 隐藏知识图', ACTIONS['toggle-canvas']], ['导入文件', ACTIONS.import], ['新建文档', newDocument],
      ['新建工作区', ACTIONS['ws-new']], ['键盘快捷键', showShortcuts], ['结束试用', endTrial],
    ].map(([label, run]) => ({ g: '命令', label, run }));
    const docs = S.docs.map((d) => ({ g: '文档', label: d.title, run: () => openDocument(d.id) }));
    const nodes = S.nodes.map((n) => ({ g: '节点', label: n.title, hint: `${KIND[n.kind] || n.kind} · ${depthLabel(n)}`, run: () => selectNode(n.id, { center: true }) }));
    const projects = S.projects.filter((x) => x.id !== S.project?.id).map((x) => ({ g: '工作区', label: x.title, run: () => switchProject(x.id) }));
    return [...cmds, ...docs, ...nodes, ...projects];
  }
  function paintPalette() {
    const q = palInput.value.trim().toLowerCase();
    palItems = paletteSource().filter((it) => !q || it.label.toLowerCase().includes(q)).slice(0, 50);
    palIdx = Math.min(palIdx, Math.max(0, palItems.length - 1));
    let g = '';
    palList.innerHTML = palItems.map((it, i) => {
      const head = it.g !== g ? `<li class="grp label" role="presentation">${(g = it.g)}</li>` : '';
      return head + `<li role="option" id="pal-${i}" data-i="${i}" aria-selected="${i === palIdx}"><span>${esc(it.label)}</span>${it.hint ? `<span class="k mono muted">${esc(it.hint)}</span>` : ''}</li>`;
    }).join('') || '<li class="muted" role="presentation">没有匹配项</li>';
    palInput.setAttribute('aria-activedescendant', palItems.length ? 'pal-' + palIdx : '');
    $(`#pal-${palIdx}`)?.scrollIntoView({ block: 'nearest' });
  }
  function openPalette() {
    if (!S.connected) return;
    closeMenus(); hideAsk();
    palette.hidden = false; palette.classList.add('top'); S.modal = true;
    palInput.value = ''; palIdx = 0; paintPalette(); palInput.focus();
  }
  function closePalette() { palette.hidden = true; S.modal = false; }
  function runPalette(i) { const it = palItems[i]; closePalette(); if (it) Promise.resolve(it.run()).catch(fail); }
  palInput.addEventListener('input', () => { palIdx = 0; paintPalette(); });
  palInput.addEventListener('keydown', (e) => {
    const n = Math.max(1, palItems.length);
    if (e.key === 'ArrowDown') { e.preventDefault(); palIdx = (palIdx + 1) % n; paintPalette(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); palIdx = (palIdx - 1 + n) % n; paintPalette(); }
    if (e.key === 'Enter') { e.preventDefault(); runPalette(palIdx); }
    if (e.key === 'Escape') { e.stopPropagation(); closePalette(); }
  });
  palList.addEventListener('click', (e) => { const li = e.target.closest('[data-i]'); if (li) runPalette(+li.dataset.i); });
  palette.addEventListener('mousedown', (e) => { if (e.target === palette) closePalette(); });

  /* ================= Session / TTL ================= */
  let warned = false, ttlTimer = null;
  function tickTtl() {
    const s = api.secondsLeft();
    const ttl = $('#ttl');
    if (s == null) { $('#ttl-v').textContent = '本地'; ttl.title = '本地模式：没有试用时限'; return; }
    $('#ttl-v').textContent = fmtClock(s);
    ttl.classList.toggle('warn', s < 300);
    if (s < 300 && s > 0 && !warned) { warned = true; toast('试用还剩 5 分钟，记得导出需要的内容', { ms: 6000 }); }
    if (s <= 0) {
      clearInterval(ttlTimer); S.connected = false; paintState();
      modal({ title: '试用已结束', locked: true,
        body: `<p style="margin:0">${api.lease?.workspace_id ? '计算租约已到期，项目数据暂时保留；超过 4 小时不使用后清理。' : '试用时间已到，临时工作区会被回收。'}</p>`,
        actions: [{ label: '返回首页', primary: true, run: () => { location.href = 'index.html'; } }] });
    }
  }
  /* Session lost (401/410) or never established: offer invite entry in place. */
  function reconnect(err) {
    if (api.managed) { location.href = 'index.html'; return; }
    const needsInvite = api.runtime?.mode === 'demo' || api.managed || err?.code === 'invite_required';
    const lost = err && [401, 410].includes(err.status) && err.code !== 'invite_required';
    modal({
      title: lost ? '会话已失效' : '连接工作区', locked: true,
      body: `<div style="display:grid;gap:12px"><p style="margin:0">${lost ? '试用会话已过期或被撤销，需要重新连接。原工作区的数据可能已被清理。' : '请输入应用体验码以连接这个工作区。'}</p>
        ${needsInvite ? '<label class="field"><span class="label">应用体验码</span><input class="input" type="password" id="rc-code" autocomplete="off"></label>' : ''}
        <p class="gate__msg" id="rc-msg" role="alert"></p></div>`,
      actions: [{ label: '返回首页', run: () => { location.href = 'index.html'; } }, { label: '连接', primary: true, run: async (c, r, b) => {
        b.disabled = true;
        try { await api.connect($('#rc-code', r)?.value.trim()); c(); await start(); }
        catch (e) { $('#rc-msg', r).textContent = e.status === 401 || e.status === 403 ? '体验码无效或已失效' : describe(e); b.disabled = false; }
      } }],
      onOpen: (r) => $('#rc-code', r)?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('.btn--primary', r).click(); } }),
    });
  }

  /* ================= Keyboard ================= */
  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey, key = e.key.toLowerCase();
    if (mod && key === 'k') { e.preventDefault(); palette.hidden ? openPalette() : closePalette(); return; }
    if (S.modal || !S.connected) return;
    if (mod && key === 'z' && !e.shiftKey && S.tool === 'draw' && !isEditable(document.activeElement)) { e.preventDefault(); inkUndoLast(); return; }
    if (mod && e.key === ',') { e.preventDefault(); openPrefs(); return; }
    if (mod && key === 'p') {
      e.preventDefault();
      if (app.classList.contains('no-explorer')) togglePane('no-explorer', 'toggle-explorer');
      setFocusMode(false);
      filterEl.focus(); filterEl.select();
      return;
    }
    if (mod && key === 'f' && !e.target.closest('.canvas')) {
      e.preventDefault();
      const s = readerSelection();
      openFind(s ? s.origin.quote : null);
      return;
    }
    if (e.altKey && key === 'e') {
      e.preventDefault();
      if (!askEl.hidden) { $('#ask-form').requestSubmit(); return; }
      const s = readerSelection();
      if (s) openAsk(s.origin, s.rect); else toast('先在文档中选中一段文字');
      return;
    }
    if (e.key === 'Escape') {
      if (!askEl.hidden) return hideAsk();
      if (selBar) return hideSelBar();
      if (Object.values(MENUS).some((m) => $('#' + m) && !$('#' + m).hidden)) return closeMenus();
      if (S.find.open) return closeFind();
      if (S.tool === 'draw') return setTool('select');
      if (app.classList.contains('focus')) return setFocusMode(false);
      if (isEditable(document.activeElement)) return document.activeElement.blur();
      if (S.selected) return selectNode(null);
      return;
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && S.selected && !isEditable(document.activeElement) && !e.target.closest('.tree')) {
      e.preventDefault();
      deleteNode(S.selected).catch(fail);
    }
  });

  /* ================= Resizable panes =================
     Two gutters: explorer↔reader sets --w-exp, reader↔canvas sets --w-canvas;
     the reader takes the rest. Arrow keys on a focused gutter, double-click resets. */
  const PANE = { exp: { v: '--w-exp', min: 160, max: 520, def: 240 }, canvas: { v: '--w-canvas', min: 320, max: 4000, def: 0 } };
  const defaultCanvasW = () => Math.round(Math.max(420, app.clientWidth / 3));
  const paneApp = app;
  const paneW = (v) => parseFloat(getComputedStyle(app).getPropertyValue(v)) || 0;
  function readPaneWidths() {
    const saved = readJSON('tlm.panes.v2', null);
    app.style.setProperty('--w-canvas', defaultCanvasW() + 'px');
    if (saved) {
      if (Number.isFinite(saved.exp)) app.style.setProperty('--w-exp', saved.exp + 'px');
      if (Number.isFinite(saved.canvas)) app.style.setProperty('--w-canvas', saved.canvas + 'px');
    }
  }
  function savePaneWidths() { localStorage.setItem('tlm.panes.v2', JSON.stringify({ exp: paneW('--w-exp'), canvas: paneW('--w-canvas') })); }
  function setPane(which, w) {
    const c = PANE[which];
    const other = which === 'exp' ? (app.classList.contains('no-canvas') ? 0 : paneW('--w-canvas')) : (app.classList.contains('no-explorer') ? 0 : paneW('--w-exp'));
    const max = Math.min(c.max, app.clientWidth - other - 320 - 24);   // reader keeps ≥ 320px
    app.style.setProperty(c.v, Math.round(Math.max(c.min, Math.min(max, w))) + 'px');
    drawEdges(); drawConnector(); drawMinimap(); updateStatus();
  }
  $$('.gutter').forEach((g) => {
    const which = g.dataset.g;
    g.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      g.setPointerCapture(e.pointerId);
      g.classList.add('active'); app.classList.add('col-resize');
      const r = app.getBoundingClientRect();
      const move = (ev) => setPane(which, which === 'exp' ? ev.clientX - r.left - 9 : r.right - ev.clientX - 9);
      const up = () => {
        g.classList.remove('active'); app.classList.remove('col-resize');
        g.removeEventListener('pointermove', move); g.removeEventListener('pointerup', up); g.removeEventListener('pointercancel', up);
        savePaneWidths();
      };
      g.addEventListener('pointermove', move); g.addEventListener('pointerup', up); g.addEventListener('pointercancel', up);
    });
    g.addEventListener('keydown', (e) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();
      const dir = (e.key === 'ArrowRight' ? 1 : -1) * (which === 'exp' ? 1 : -1);
      setPane(which, paneW(PANE[which].v) + dir * 24); savePaneWidths();
    });
    g.addEventListener('dblclick', () => { setPane(which, PANE[which].def || defaultCanvasW()); savePaneWidths(); });
  });
  readPaneWidths();

  /* ================= Reader font zoom =================
     [−] [100%] [+] scales the document body via --doc-scale; the middle 100%
     resets to 1. Range 0.7–1.8, persisted in localStorage. */
  const DSCALE_MIN = 0.7, DSCALE_MAX = 1.8, DSCALE_STEP = 0.1;
  let docScale = Number(localStorage.getItem('tlm.docscale')) || 1;
  function applyDocScale() {
    docScale = Math.max(DSCALE_MIN, Math.min(DSCALE_MAX, Math.round(docScale * 100) / 100));
    paneApp.style.setProperty('--doc-scale', docScale);
    const label = $('#doc-zoom-v');
    if (label) label.textContent = Math.round(docScale * 100) + '%';
    localStorage.setItem('tlm.docscale', docScale);
  }
  $('#doc-zoom-in')?.addEventListener('click', () => { docScale += DSCALE_STEP; applyDocScale(); });
  $('#doc-zoom-out')?.addEventListener('click', () => { docScale -= DSCALE_STEP; applyDocScale(); });
  $('#doc-zoom-v')?.addEventListener('click', () => { docScale = 1; applyDocScale(); });
  applyDocScale();

  /* ================= Boot ================= */
  let presence = null, sessionCheck = null, lastTouch = 0;
  document.addEventListener('pointerdown', () => {
    if (!S.connected || !api.lease?.workspace_id || Date.now() - lastTouch < 60000) return;
    lastTouch = Date.now();
    api.post(`/control/workspaces/${p(api.lease.workspace_id)}/touch`, {}, { auth: false }).catch(fail);
  }, { passive: true });
  async function start() {
    applyPrefs();
    const health = await api.get('/health/ready', { auth: false }).catch(() => ({}));
    const provider = api.runtime.provider || health.provider || {};
    const modeChip = $('.topbar > .chip');
    if (modeChip) {
      modeChip.textContent = provider.name === 'fake' ? '演示模型' : 'v2';
      modeChip.title = provider.name === 'fake' ? '服务器使用离线演示 Provider，回答不代表真实模型质量' : '';
    }
    $('#usage-model').textContent = `${provider.name || '—'} / ${provider.model || '—'}`;
    const workflows = (api.runtime.plugins || []).filter((x) => x.enabled !== false).flatMap((x) => x.workflows || []);
    S.kinds = new Set(workflows.map((w) => w.kind));
    S.connected = true; paintState();
    if (api.managed && api.lease) {
      presence?.dispose();
      const { attachLeaseLifecycle } = await import('/static/reader/lease-lifecycle.js');
      presence = attachLeaseLifecycle(api.lease, { onLeaseLost: fail });
      await presence.ready;
    }
    clearInterval(ttlTimer); tickTtl(); ttlTimer = setInterval(tickTtl, 1000);
    // Redeem a share link once: the server copies the shared workspace into a
    // new project owned by this session; loadProjects() then opens it.
    await loadProjects();
    await resumeRuns();
    if (api.managed && api.recovery && !sessionStorage.getItem('tlm.recovery-shown')) {
      sessionStorage.setItem('tlm.recovery-shown', '1');
      toast('已创建持久用户，请在用户菜单中查看并保存恢复凭据', { ms: 8000 });
    }
    clearInterval(sessionCheck);
    if (api.session) sessionCheck = setInterval(() => api.loadSession().catch((e) => { if ([401, 410].includes(e.status)) fail(e); }), 60000);
  }
  async function boot() {
    placeInspector();
    applyPrefs();
    renderReader(); renderCanvas(); paintState();
    // A share link (?share=CODE) is redeemed once, then removed from the URL so
    // a reload never copies the workspace twice.
    pendingShareCode = new URLSearchParams(location.search).get('share');
    if (pendingShareCode) { location.replace('index.html?share=' + encodeURIComponent(pendingShareCode)); return; }
    try {
      await api.connect();
    } catch (e) {
      if (e.status === 503 && api.managed) { location.href = 'busy.html'; return; }
      if ([401, 403, 410].includes(e.status)) { reconnect(e); return; }
      fail(e);
      $('#st-state').textContent = '连接失败';
      return;
    }
    await start();
  }
  window.TLM_WS = { get state() { return S; }, select: (id) => selectNode(id, { center: true }), splitAnswer }; // test/debug hook
  if (document.fonts?.ready) document.fonts.ready.then(() => { drawEdges(); drawConnector(); updateStatus(); });
  boot().catch(fail);
})();
