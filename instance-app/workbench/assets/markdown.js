(function (root) {
  'use strict';
  const MarkdownIt = typeof module === 'object' && module.exports
    ? require('./markdown-it/markdown-it.min.js') : root.markdownit;

  function createRenderer({ math } = {}) {
    const md = new MarkdownIt({ html: false, breaks: false, linkify: false, typographer: false });
    const escape = md.utils.escapeHtml;
    math ||= escape;
    const mathSource = token => token.meta?.sourceContent ?? token.content;
    function findLiteral(source, literal, from = 0) {
      const at = source.indexOf(literal, from);
      if (at >= 0) return { at, text: literal };
      const pattern = literal.split('\n').map(line => line.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\r?\\n');
      const match = new RegExp(pattern).exec(source.slice(from));
      return match ? { at: from + match.index, text: match[0] } : null;
    }
    function rawOffset(source, offset) {
      let raw = 0, normalized = 0;
      while (raw < source.length && normalized < offset) {
        if (source[raw] === '\r' && source[raw + 1] === '\n') raw++;
        raw++; normalized++;
      }
      return raw;
    }

    function mathAttrs(token, env) {
      if (!root.TutorMathSelection) return '';
      if (Number.isInteger(token.meta?.sourceStart)) return root.TutorMathSelection.attributes(mathSource(token), token.meta.sourceStart);
      const literal = token.meta?.literal || token.content;
      const at = env.source?.indexOf(literal, env.mathCursor || 0) ?? -1;
      let start = -1;
      if (at >= 0) {
        const inner = literal.indexOf(token.content, token.meta?.openLength || 0);
        if (inner >= 0) start = at + inner;
        env.mathCursor = at + literal.length;
      }
      return root.TutorMathSelection.attributes(token.content, start);
    }

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
        token.meta = { literal: state.src.slice(start, end + 1), openLength: 1, inlineStart: start };
      }
      state.pos = end + 1;
      return true;
    });
    md.renderer.rules.math_inline = (tokens, index, options, env) => `<span class="math"${mathAttrs(tokens[index], env)}>${math(mathSource(tokens[index]))}</span>`;

    const PAIRS = [['\\(', '\\)', false], ['\\[', '\\]', true], ['$$', '$$', true]];
    md.inline.ruler.before('escape', 'math_environment', (state, silent) => {
      const start = state.pos;
      const open = state.src.slice(start).match(/^\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}/);
      if (!open) return false;
      const close = `\\end{${open[1]}}`, end = state.src.indexOf(close, start + open[0].length);
      if (end < 0) return false;
      if (!silent) {
        const token = state.push('math_display', 'span', 0);
        token.content = state.src.slice(start, end + close.length);
        token.meta = { literal: token.content, openLength: 0, inlineStart: start };
      }
      state.pos = end + close.length;
      return true;
    });
    md.inline.ruler.before('escape', 'math_delimited', (state, silent) => {
      const start = state.pos;
      const pair = PAIRS.find(([open]) => state.src.startsWith(open, start));
      if (!pair) return false;
      const [open, close, display] = pair;
      const end = state.src.indexOf(close, start + open.length);
      if (end < 0 || end === start + open.length) return false;
      const content = state.src.slice(start + open.length, end);
      if (!display && /[\n\r]/.test(content)) return false;
      if (!silent) {
        const token = state.push(display ? 'math_display' : 'math_inline', 'span', 0);
        token.content = content.trim();
        token.meta = { literal: state.src.slice(start, end + close.length), openLength: open.length, inlineStart: start };
      }
      state.pos = end + close.length;
      return true;
    });
    md.renderer.rules.math_display = (tokens, index, options, env) => `<span class="math math--display"${mathAttrs(tokens[index], env)}>${math(mathSource(tokens[index]), true)}</span>`;

    md.block.ruler.before('fence', 'math_block', (state, start, end, silent) => {
      const first = state.src.slice(state.bMarks[start] + state.tShift[start], state.eMarks[start]);
      const fence = first.startsWith('$$') ? ['$$', '$$'] : first.startsWith('\\[') ? ['\\[', '\\]'] : null;
      if (!fence) return false;
      if (silent) return true;
      const content = [], rest = first.slice(2);
      let next = start + 1;
      const close = rest.indexOf(fence[1]);
      if (close >= 0) content.push(rest.slice(0, close));
      else {
        content.push(rest);
        for (; next < end; next++) {
          const line = state.src.slice(state.bMarks[next] + state.tShift[next], state.eMarks[next]);
          const at = line.indexOf(fence[1]);
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
      `<div${self.renderAttrs(tokens[index])} class="math-block"${mathAttrs(tokens[index], env)}>${math(mathSource(tokens[index]), true)}</div>\n`;

    md.core.ruler.push('tasks_and_source_ranges', state => {
      let block = 0, row = null, cells = [], cellIndex = 0;
      for (let i = 0; i < state.tokens.length; i++) {
        const token = state.tokens[i];
        if (token.map && (token.type === 'inline' || token.type === 'math_block')) {
          let cursor = state.env.starts[token.map[0]] || 0;
          const limit = state.env.starts[token.map[1]] ?? state.env.length;
          const inlineMatch = token.type === 'inline' ? findLiteral(state.env.source, token.content, cursor) : null;
          for (const child of token.type === 'math_block' ? [token] : token.children || []) {
            if (!['math_inline', 'math_display', 'math_block'].includes(child.type)) continue;
            const literal = child.meta?.literal || child.content;
            const from = inlineMatch && Number.isInteger(child.meta?.inlineStart)
              ? inlineMatch.at + rawOffset(inlineMatch.text, child.meta.inlineStart) : cursor;
            const found = findLiteral(state.env.source, literal, from);
            if (!found || found.at + found.text.length > limit) continue;
            const content = findLiteral(found.text, child.content, child.meta?.openLength || 0);
            if (!content) continue;
            child.meta = { ...child.meta, sourceStart: found.at + content.at, sourceContent: content.text };
            cursor = found.at + found.text.length;
          }
        }
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
    md.renderer.rules.table_open = (tokens, index, options, env) => '<div class="md-table" role="region" aria-label="表格" tabindex="0">' +
      (env.blocks === false ? '' : '<button type="button" class="icon-btn" data-ir-edit-table title="编辑表格" aria-label="编辑表格"><svg class="ic" aria-hidden="true"><use href="#i-code"/></svg></button>') + '<table>\n';
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
    // Project visible Markdown text back to UTF-16 spans in its raw source. A
    // selection may cross formatting delimiters, so matching its rendered text
    // directly against the raw Markdown cannot produce a reliable anchor.
    function project(raw) {
      const source = String(raw || ''), starts = [0];
      for (const match of source.matchAll(/\r\n?|\n/g)) starts.push(match.index + match[0].length);
      const tokens = md.parse(source, { blocks: false, starts, source, length: source.length });
      const result = { text: '', starts: [], ends: [] };
      let cursor = 0;
      function unit(at, code) {
        if (source[at] === '\r' && source[at + 1] === '\n') return { value: '\n', length: 2 };
        if (code && source[at] === '\\' && /[!"#$%&'()*+,\-./:;<=>?@[\]\\^_`{|}~]/.test(source[at + 1] || '')) {
          return { value: source[at + 1], length: 2 };
        }
        if (code && source[at] === '&') {
          const entity = source.slice(at).match(/^&(?:#[xX][\da-fA-F]+|#\d+|[a-zA-Z][a-zA-Z\d]{1,31});/);
          if (entity) {
            const decoded = md.utils.unescapeAll(entity[0]);
            if (decoded !== entity[0]) return { value: decoded, length: entity[0].length };
          }
        }
        return { value: source[at], length: 1 };
      }
      function append(value, code = true) {
        if (!value) return true;
        for (let at = cursor; at < source.length; at++) {
          let pos = at, index = 0;
          const spans = [];
          while (index < value.length && pos < source.length) {
            const part = unit(pos, code);
            if (!value.startsWith(part.value, index)) break;
            for (let i = 0; i < part.value.length; i++) spans.push([pos, pos + part.length]);
            index += part.value.length;
            pos += part.length;
          }
          if (index !== value.length) continue;
          result.text += value;
          for (const [start, end] of spans) { result.starts.push(start); result.ends.push(end); }
          cursor = pos;
          return true;
        }
        return false;
      }
      function skipLinkTail() {
        // The link destination and title are source-only text. Skip them so a
        // later visible word cannot accidentally map to the URL or title.
        if (source[cursor] === '>') { cursor++; return true; }
        let close = cursor;
        while (close < source.length && source[close] !== ']') {
          if (source[close] === '\\') close++;
          close++;
        }
        if (source[close] !== ']') return false;
        cursor = close + 1;
        if (source[cursor] === '[') {
          close = source.indexOf(']', cursor + 1);
          if (close < 0) return false;
          cursor = close + 1;
        } else if (source[cursor] === '(') {
          let depth = 1, quoted = '';
          cursor++;
          while (cursor < source.length && depth) {
            if (source[cursor] === '\\') { cursor += 2; continue; }
            if (quoted) {
              if (source[cursor] === quoted) quoted = '';
            } else if (depth === 1 && /["']/.test(source[cursor])) quoted = source[cursor];
            else if (source[cursor] === '(') depth++;
            else if (source[cursor] === ')') depth--;
            cursor++;
          }
          if (depth) return false;
        }
        return true;
      }
      for (const token of tokens) {
        if (token.type === 'inline') {
          for (const child of token.children || []) {
            if (child.type === 'text' || child.type === 'code_inline') {
              if (!append(child.content, child.type === 'text')) return null;
            } else if (child.type === 'softbreak') {
              if (!append('\n')) return null;
            } else if (child.type === 'link_close') {
              if (!skipLinkTail()) return null;
            } else if (child.type === 'html_inline' && child.content.startsWith('<input type="checkbox" disabled')) {
              // The task checkbox is generated by this renderer, not the source.
            } else if (['image', 'math_inline', 'math_display', 'html_inline'].includes(child.type)) {
              return null;
            }
          }
        } else if (token.type === 'fence' || token.type === 'code_block') {
          if (!append(token.content, false)) return null;
        } else if (token.type === 'math_block') return null;
      }
      return result;
    }
    return {
      render(raw, { blocks = true } = {}) {
        const source = String(raw || ''), starts = [0];
        for (const match of source.matchAll(/\r\n?|\n/g)) starts.push(match.index + match[0].length);
        return md.render(source, { blocks, starts, source, length: source.length });
      },
      inline: value => { const source = String(value || ''); return md.renderInline(source, { source, starts: [0], length: source.length, blocks: false }); },
      project,
      text(value, { offset = 0 } = {}) {
        const source = String(value || ''), env = { source, starts: [0], length: source.length, blocks: false };
        const tokens = md.parseInline(source, env)[0]?.children || [];
        let cursor = 0, html = '';
        for (const token of tokens) {
          if (!['math_inline', 'math_display'].includes(token.type)) continue;
          const literal = token.meta.literal;
          const from = Number.isInteger(token.meta.inlineStart) ? rawOffset(source, token.meta.inlineStart) : cursor;
          const found = findLiteral(source, literal, from);
          if (!found) continue;
          html += escape(source.slice(cursor, found.at)).replace(/\r?\n/g, '<br>');
          const content = findLiteral(found.text, token.content, token.meta.openLength);
          if (!content) continue;
          token.meta.sourceStart = offset + found.at + content.at; token.meta.sourceContent = content.text;
          html += md.renderer.rules[token.type]([token], 0, md.options, env);
          cursor = found.at + found.text.length;
        }
        return html + escape(source.slice(cursor)).replace(/\r?\n/g, '<br>');
      },
    };
  }
  if (typeof module === 'object' && module.exports) module.exports = { createRenderer };
  else root.TutorMarkdown = { createRenderer };
})(typeof window === 'object' ? window : globalThis);
