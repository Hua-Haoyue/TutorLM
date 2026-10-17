(function (root) {
  'use strict';
  const MarkdownIt = typeof module === 'object' && module.exports
    ? require('./markdown-it/markdown-it.min.js') : root.markdownit;

  function createRenderer({ math } = {}) {
    const md = new MarkdownIt({ html: false, breaks: false, linkify: false, typographer: false });
    const escape = md.utils.escapeHtml;
    math ||= escape;

    md.inline.ruler.before('escape', 'math_inline', (state, silent) => {
      const start = state.pos;
      if (state.src[start] !== '$' || state.src[start + 1] === '$' || /\s/.test(state.src[start + 1] || ' ')) return false;
      let end = start + 1;
      while ((end = state.src.indexOf('$', end)) >= 0) {
        if (state.src[end - 1] !== '\\') break;
        end++;
      }
      if (end < 0 || /[\n\r]/.test(state.src.slice(start, end)) || /\s/.test(state.src[end - 1])) return false;
      if (!silent) {
        const token = state.push('math_inline', 'span', 0);
        token.content = state.src.slice(start + 1, end);
      }
      state.pos = end + 1;
      return true;
    });
    md.renderer.rules.math_inline = (tokens, index) => `<span class="math">${math(tokens[index].content)}</span>`;

    md.block.ruler.before('fence', 'math_block', (state, start, end, silent) => {
      const first = state.src.slice(state.bMarks[start] + state.tShift[start], state.eMarks[start]);
      if (!first.startsWith('$$')) return false;
      if (silent) return true;
      const content = [], rest = first.slice(2);
      let next = start + 1;
      const close = rest.indexOf('$$');
      if (close >= 0) content.push(rest.slice(0, close));
      else {
        content.push(rest);
        for (; next < end; next++) {
          const line = state.src.slice(state.bMarks[next] + state.tShift[next], state.eMarks[next]);
          const at = line.indexOf('$$');
          if (at >= 0) { content.push(line.slice(0, at)); next++; break; }
          content.push(line);
        }
      }
      const token = state.push('math_block', 'div', 0);
      token.block = true; token.map = [start, next]; token.content = content.join('\n').trim();
      state.line = next;
      return true;
    }, { alt: ['paragraph', 'reference', 'blockquote', 'list'] });
    md.renderer.rules.math_block = (tokens, index, options, env, self) =>
      `<div${self.renderAttrs(tokens[index])} class="math-block">${math(tokens[index].content)}</div>\n`;

    md.core.ruler.push('tasks_and_source_ranges', state => {
      let block = 0, row = null, cells = [], cellIndex = 0;
      for (let i = 0; i < state.tokens.length; i++) {
        const token = state.tokens[i];
        if (token.type === 'tr_open') {
          row = token.map;
          cells = []; cellIndex = 0;
          const start = state.env.starts[row[0]], end = state.env.starts[row[0] + 1] ?? state.env.length;
          const line = state.env.source.slice(start, end).replace(/[\r\n]+$/, '');
          let left = 0;
          for (let at = 0; at < line.length; at++) {
            if (line[at] === '\\') { at++; continue; }
            if (line[at] === '|') { cells.push([start + left, start + at]); left = at + 1; }
          }
          cells.push([start + left, start + line.length]);
          if (!state.env.source.slice(...cells[0]).trim()) cells.shift();
          if (cells.length && !state.env.source.slice(...cells[cells.length - 1]).trim()) cells.pop();
        }
        const range = token.map || (['th_open', 'td_open'].includes(token.type) ? row : null);
        const selectable = ['paragraph_open', 'heading_open', 'fence', 'code_block', 'math_block', 'th_open', 'td_open'].includes(token.type)
          || (token.type === 'list_item_open' && state.tokens[i + 1]?.hidden);
        if (state.env.blocks !== false && range && selectable && !token.hidden) {
          token.attrSet('data-b', String(block++));
          token.attrSet('data-start', String(state.env.starts[range[0]] ?? state.env.length));
          token.attrSet('data-end', String(state.env.starts[range[1]] ?? state.env.length));
          if (['th_open', 'td_open'].includes(token.type)) {
            const cell = cells[cellIndex++];
            if (cell) { token.attrSet('data-start', String(cell[0])); token.attrSet('data-end', String(cell[1])); }
          }
          if (token.type === 'heading_open') token.attrSet('id', `h-${block - 1}`);
        }
        if (token.type !== 'inline' || state.tokens[i - 2]?.type !== 'list_item_open') continue;
        const first = token.children?.[0];
        const task = first?.type === 'text' && first.content.match(/^\[([ xX])\]\s+/);
        if (!task) continue;
        first.content = first.content.slice(task[0].length);
        const checkbox = new state.Token('html_inline', '', 0);
        checkbox.content = `<input type="checkbox" disabled${task[1].toLowerCase() === 'x' ? ' checked' : ''} aria-label="${task[1].toLowerCase() === 'x' ? '已完成' : '未完成'}"> `;
        token.children.unshift(checkbox);
        state.tokens[i - 2].attrJoin('class', 'task-list-item');
      }
    });

    for (const type of ['fence', 'code_block']) {
      const render = md.renderer.rules[type];
      md.renderer.rules[type] = (tokens, index, ...args) => {
        const attrs = md.renderer.renderAttrs(tokens[index]);
        return render(tokens, index, ...args).replace('<pre>', `<pre${attrs}>`);
      };
    }
    md.renderer.rules.table_open = () => '<div class="md-table" role="region" aria-label="表格" tabindex="0"><table>\n';
    md.renderer.rules.table_close = () => '</table></div>\n';
    // Documents never load remote images or execute supplied HTML.
    md.renderer.rules.image = (tokens, index) => `<span class="md-image-alt">[图片：${escape(tokens[index].content)}]</span>`;
    const link = md.renderer.rules.link_open || ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));
    md.renderer.rules.link_open = (tokens, index, ...args) => {
      if (/^(https?:)?\/\//i.test(tokens[index].attrGet('href') || '')) {
        tokens[index].attrSet('target', '_blank');
        tokens[index].attrSet('rel', 'noopener noreferrer');
      }
      return link(tokens, index, ...args);
    };
    return {
      render(raw, { blocks = true } = {}) {
        const source = String(raw || ''), starts = [0];
        for (const match of source.matchAll(/\r\n?|\n/g)) starts.push(match.index + match[0].length);
        return md.render(source, { blocks, starts, source, length: source.length });
      },
      inline: value => md.renderInline(String(value || '')),
    };
  }
  if (typeof module === 'object' && module.exports) module.exports = { createRenderer };
  else root.TutorMarkdown = { createRenderer };
})(typeof window === 'object' ? window : globalThis);
