/* TutorLM workbench · code view, laid out like an editor: the whole reading page is the
   file, with a breadcrumb path, a line-number gutter and a current-line highlight.
   The <pre> keeps plain text joined by newlines (plus data-b / data-start / data-end)
   so selections and node anchors still map onto the source exactly; line numbers live
   in a separate, non-selectable gutter. */
(function () {
  'use strict';
  const escape = text => String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const extension = file => String(file || '').split('.').pop().toLowerCase();
  const languages = { py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
    c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', java: 'java', rs: 'rust', go: 'go', sh: 'bash', json: 'json', yml: 'yaml', yaml: 'yaml',
    sql: 'sql', html: 'xml', css: 'css', toml: 'ini', rb: 'ruby', php: 'php', swift: 'swift', kt: 'kotlin', vue: 'xml' };
  const LABEL = { python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript', c: 'C', cpp: 'C++', java: 'Java', rust: 'Rust',
    go: 'Go', bash: 'Shell', json: 'JSON', yaml: 'YAML', sql: 'SQL', xml: 'HTML', css: 'CSS', ini: 'TOML', ruby: 'Ruby', php: 'PHP',
    swift: 'Swift', kotlin: 'Kotlin', plaintext: 'Plain Text' };
  let dirty = false;

  const lineCount = text => (String(text).match(/\n/g) || []).length + 1;
  function gutterHtml(count) {
    let out = '';
    for (let i = 1; i <= count; i++) out += `<span>${i}</span>`;
    return out;
  }
  function crumbs(path) {
    const parts = String(path).split('/').filter(Boolean);
    return parts.map((p, i) => `<span class="code-crumb${i === parts.length - 1 ? ' code-crumb--file' : ''}">${escape(p)}</span>`)
      .join('<span class="code-crumb-sep" aria-hidden="true">›</span>');
  }

  function mount(host, { doc, text, save, error }) {
    dirty = false;
    const path = doc.code_path || doc.source_filename || doc.title;
    const language = languages[extension(path)] || 'plaintext';
    const html = window.hljs?.getLanguage(language) ? hljs.highlight(text, { language }).value : escape(text);
    const lines = lineCount(text);
    host.innerHTML = `<div class="code-bar">
        <nav class="code-crumbs" aria-label="文件路径">${crumbs(path)}</nav>
        <span class="code-meta mono">${lines} 行 · ${LABEL[language] || language}</span>
        <button class="icon-btn" data-code-edit title="编辑代码" aria-label="编辑代码" ${doc.code_path ? '' : 'disabled'}><svg class="ic"><use href="#i-pen"/></svg></button></div>
      <div class="code-reading" style="--code-digits:${String(lines).length}">
        <div class="code-gutter" aria-hidden="true">${gutterHtml(lines)}</div>
        <div class="code-body"><div class="code-current" aria-hidden="true" hidden></div>
          <pre class="code-source" data-b="0" data-start="0" data-end="${text.length}"><code>${html}</code></pre></div></div>
      <form class="code-editor" hidden>
        <div class="code-bar"><span class="code-meta mono">编辑 · ${escape(path)}</span><output aria-live="polite"></output>
          <button type="button" class="icon-btn" data-code-cancel title="取消编辑" aria-label="取消编辑"><svg class="ic"><use href="#i-close"/></svg></button>
          <button class="icon-btn" type="submit" title="保存代码 (Ctrl+S)" aria-label="保存代码"><svg class="ic"><use href="#i-check"/></svg></button></div>
        <div class="code-reading code-reading--edit" style="--code-digits:${String(lines).length}">
          <div class="code-gutter" aria-hidden="true">${gutterHtml(lines)}</div>
          <textarea spellcheck="false" wrap="off" aria-label="代码编辑器"></textarea></div></form>`;
    const bar = host.querySelector('.code-bar'), reading = host.querySelector('.code-reading');
    const form = host.querySelector('form'), editor = form.querySelector('textarea');
    const pre = reading.querySelector('pre'), current = reading.querySelector('.code-current');
    const editGutter = form.querySelector('.code-gutter');
    editor.value = text;
    const output = form.querySelector('output');

    /* Current line: the line holding the caret / click, as in an editor. */
    function lineAt(node, offset) {
      const r = document.createRange();
      r.selectNodeContents(pre); r.setEnd(node, offset);
      return (r.toString().match(/\n/g) || []).length;
    }
    pre.addEventListener('mouseup', () => {
      const sel = getSelection();
      if (!sel.rangeCount || !pre.contains(sel.anchorNode)) return;
      const line = lineAt(sel.anchorNode, sel.anchorOffset);
      const style = getComputedStyle(pre), height = parseFloat(style.lineHeight) || 22;
      current.hidden = false;
      current.style.top = `${parseFloat(style.paddingTop) + line * height}px`;
      current.style.height = `${height}px`;
      reading.querySelectorAll('.code-gutter span.on').forEach(s => s.classList.remove('on'));
      reading.querySelector(`.code-gutter span:nth-child(${line + 1})`)?.classList.add('on');
    });

    function syncEditGutter() {
      const count = lineCount(editor.value);
      if (editGutter.childElementCount !== count) {
        editGutter.innerHTML = gutterHtml(count);
        form.querySelector('.code-reading').style.setProperty('--code-digits', String(count).length);
      }
      editor.style.height = 'auto';
      editor.style.height = Math.max(400, editor.scrollHeight) + 'px';
    }

    host.querySelector('[data-code-edit]').onclick = () => { form.hidden = false; reading.hidden = true; bar.hidden = true; syncEditGutter(); editor.focus(); };
    host.querySelector('[data-code-cancel]').onclick = () => {
      dirty = false; editor.value = text; form.hidden = true; reading.hidden = false; bar.hidden = false;
    };
    editor.oninput = () => { dirty = editor.value !== text; syncEditGutter(); };
    editor.addEventListener('keydown', event => {
      const start = editor.selectionStart, end = editor.selectionEnd;
      if (event.key === 'Tab') {
        event.preventDefault();
        const lineStart = editor.value.lastIndexOf('\n', start - 1) + 1;
        if (start !== end || event.shiftKey) {
          const prefix = editor.value.slice(0, lineStart), block = editor.value.slice(lineStart, end);
          const updated = block.split('\n').map(line => event.shiftKey ? line.replace(/^( {1,4}|\t)/, '') : '    ' + line).join('\n');
          editor.setRangeText(updated, lineStart, end, 'select');
          if (!prefix && !updated) editor.selectionStart = 0;
        } else {
          const word = editor.value.slice(lineStart, start).match(/[A-Za-z_$][\w$]*$/)?.[0];
          const words = [...new Set(text.match(/[A-Za-z_$][\w$]{2,}/g) || [])];
          const matches = word ? words.filter(value => value.startsWith(word) && value !== word).sort() : [];
          editor.setRangeText(matches.length === 1 ? matches[0].slice(word.length) : '    ', start, end, 'end');
        }
        dirty = true; syncEditGutter();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        const line = editor.value.slice(editor.value.lastIndexOf('\n', start - 1) + 1, start);
        const indent = line.match(/^\s*/)[0] + (/[{(:]\s*$/.test(line) ? '    ' : '');
        editor.setRangeText('\n' + indent, start, end, 'end'); dirty = true; syncEditGutter();
      } else if ((event.ctrlKey || event.metaKey) && event.key === 's') {
        event.preventDefault(); form.requestSubmit();
      }
    });
    form.onsubmit = async event => {
      event.preventDefault(); output.textContent = '保存中…';
      const button = form.querySelector('[type=submit]'); button.disabled = true;
      try { dirty = false; await save(editor.value); }
      catch (err) { dirty = true; output.textContent = err.message; error(err); }
      finally { button.disabled = false; }
    };
  }
  const supported = file => Object.hasOwn(languages, extension(file));
  /** A code document: imported from a code project, or a source file by extension. */
  const isCode = doc => !!doc && (!!doc.code_path || supported(doc.source_filename || doc.title));
  window.TutorCode = { mount, dirty: () => dirty, supported, isCode };
})();
