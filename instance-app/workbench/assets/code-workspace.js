(function () {
  'use strict';
  const escape = text => String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const extension = file => file.split('.').pop().toLowerCase();
  const languages = { py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
    c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', java: 'java', rs: 'rust', go: 'go', sh: 'bash', json: 'json', yml: 'yaml', yaml: 'yaml',
    sql: 'sql', html: 'xml', css: 'css', toml: 'ini', rb: 'ruby', php: 'php', swift: 'swift', kt: 'kotlin', vue: 'xml' };
  let dirty = false;
  function mount(host, { doc, text, save, error }) {
    dirty = false;
    const language = languages[extension(doc.source_filename || doc.title)] || 'plaintext';
    const html = window.hljs?.getLanguage(language) ? hljs.highlight(text, { language }).value : escape(text);
    host.innerHTML = `<div class="code-toolbar"><span class="mono">${escape(doc.code_path || doc.title)} · ${language}</span>
      <button class="icon-btn" data-code-edit title="编辑代码" aria-label="编辑代码" ${doc.code_path ? '' : 'disabled'}><svg class="ic"><use href="#i-file-code"/></svg></button></div>
      <div class="code-reading"><pre class="code-source" data-b="0" data-start="0" data-end="${text.length}"><code>${html}</code></pre></div>
      <form class="code-editor" hidden><textarea spellcheck="false" aria-label="代码编辑器"></textarea>
      <div class="code-toolbar"><output aria-live="polite"></output><button type="button" class="icon-btn" data-code-cancel title="取消编辑" aria-label="取消编辑"><svg class="ic"><use href="#i-close"/></svg></button>
      <button class="icon-btn" type="submit" title="保存代码" aria-label="保存代码"><svg class="ic"><use href="#i-check"/></svg></button></div></form>`;
    const reading = host.querySelector('.code-reading'), form = host.querySelector('form'), editor = form.querySelector('textarea');
    editor.value = text;
    const output = form.querySelector('output');
    host.querySelector('[data-code-edit]').onclick = () => { form.hidden = false; reading.hidden = true; editor.focus(); };
    host.querySelector('[data-code-cancel]').onclick = () => { dirty = false; editor.value = text; form.hidden = true; reading.hidden = false; };
    editor.oninput = () => { dirty = editor.value !== text; };
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
        dirty = true;
      } else if (event.key === 'Enter') {
        event.preventDefault();
        const line = editor.value.slice(editor.value.lastIndexOf('\n', start - 1) + 1, start);
        const indent = line.match(/^\s*/)[0] + (/[{(:]\s*$/.test(line) ? '    ' : '');
        editor.setRangeText('\n' + indent, start, end, 'end'); dirty = true;
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
  window.TutorCode = { mount, dirty: () => dirty, supported: file => Object.hasOwn(languages, extension(file)) };
})();
