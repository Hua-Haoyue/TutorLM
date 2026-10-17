(function (root) {
  'use strict';
  const escape = value => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const families = { bin: 'mathbin', rel: 'mathrel', open: 'mathopen', close: 'mathclose', punct: 'mathpunct', inner: 'mathinner' };

  function render(katex, source, display = false) {
    const options = { displayMode: !!display, throwOnError: false, trust: false, strict: 'ignore', output: 'html' };
    if (source.includes('\\htmlData')) return katex.renderToString(source, options);
    // KaTeX's parser supplies source locations; never infer TeX offsets from glyphs.
    try {
      const locations = new Map(), structures = [], radicals = [];
      function visit(node) {
        if (!node || typeof node !== 'object') return null;
        if (Array.isArray(node)) {
          const ranges = node.map(visit).filter(Boolean);
          return ranges.length ? [Math.min(...ranges.map(r => r[0])), Math.max(...ranges.map(r => r[1]))] : null;
        }
        if (['mathord', 'textord', 'atom'].includes(node.type) && node.loc?.lexer?.input === source) {
          const { start, end } = node.loc;
          if (start >= 0 && end > start && end <= source.length) locations.set(start, { start, end, family: families[node.family] });
        }
        const children = Object.entries(node).filter(([key]) => key !== 'loc').map(([, value]) => visit(value)).filter(Boolean);
        let bounds = node.loc?.lexer?.input === source ? [node.loc.start, node.loc.end] :
          children.length ? [Math.min(...children.map(r => r[0])), Math.max(...children.map(r => r[1]))] : null;
        if (bounds && node.type === 'genfrac' && node.numer?.loc && node.denom?.loc) {
          const command = source.slice(0, node.numer.loc.start).match(/\\(?:frac|dfrac|tfrac|cfrac|binom|dbinom|tbinom)\s*$/);
          if (command) { bounds = [command.index, node.denom.loc.end]; structures.push(bounds); }
        }
        if (bounds && node.type === 'supsub' && node.base?.loc?.lexer?.input === source) structures.push(bounds);
        if (node.type === 'sqrt' && node.body?.loc?.lexer?.input === source) {
          const argument = node.index?.loc?.start ?? node.body.loc.start;
          const command = source.slice(0, argument).match(/\\sqrt\s*$/);
          if (command) {
            radicals.push({ start: command.index, end: command.index + 5 });
            bounds = [command.index, node.body.loc.end]; structures.push(bounds);
          }
        }
        return bounds;
      }
      visit(katex.__parse(source, options));
      let marked = source;
      const spans = [...locations.values()].sort((a, b) => b.start - a.start);
      let boundary = source.length;
      for (const span of spans) {
        if (span.end > boundary) continue;
        const original = source.slice(span.start, span.end);
        let token = `\\htmlData{tlm-start=${span.start},tlm-end=${span.end}}{${original}}`;
        token = span.family ? `\\${span.family}{${token}}` : `{${token}}`;
        marked = marked.slice(0, span.start) + token + marked.slice(span.end);
        boundary = span.start;
      }
      let html = katex.renderToString(marked, { ...options, trust: context =>
        context.command === '\\htmlData' && Object.keys(context.attributes || {}).length === 2 &&
        Object.entries(context.attributes).every(([key, value]) => ['data-tlm-start', 'data-tlm-end'].includes(key) && /^\d+$/.test(value)) });
      if (html.includes('katex-error')) return katex.renderToString(source, options);
      // Named operators lack parser locations. Match their rendered operator nodes
      // to source commands without changing limit placement or operator spacing.
      if (root.document) {
        const template = root.document.createElement('template');
        template.innerHTML = html;
        const roots = [...template.content.querySelectorAll('.sqrt')];
        radicals.sort((a, b) => a.start - b.start).forEach((radical, index) => {
          const root = roots[index];
          const tail = root && [...root.querySelectorAll('.hide-tail')].find(el => el.closest('.sqrt') === root);
          if (!tail) return;
          const marker = root.ownerDocument.createElement('span');
          marker.dataset.tlmStart = String(radical.start); marker.dataset.tlmEnd = String(radical.end);
          marker.style.cssText = 'position:absolute;left:0;top:0;width:.833em;height:100%;color:transparent;user-select:text;cursor:text;line-height:1;';
          marker.textContent = '\u221a'; tail.appendChild(marker);
        });
        const operators = [...template.content.querySelectorAll('.mop')].filter(el => !el.querySelector('.mop'));
        let cursor = 0;
        for (const match of source.matchAll(/\\[a-zA-Z]+/g)) {
          if (locations.has(match.index)) continue;
          const command = match[0];
          const probe = root.document.createElement('template');
          probe.innerHTML = katex.renderToString(command, options);
          const operator = probe.content.querySelector('.mop');
          if (!operator || probe.content.querySelector('.katex-error')) continue;
          const at = operators.findIndex((el, index) => index >= cursor && el.textContent === operator.textContent);
          if (at < 0) continue;
          operators[at].dataset.tlmStart = String(match.index);
          operators[at].dataset.tlmEnd = String(match.index + command.length);
          cursor = at + 1;
        }
        html = template.innerHTML;
      }
      return `<span data-math-structures="${escape(JSON.stringify(structures))}">${html}</span>`;
    } catch {
      return katex.renderToString(source, options);
    }
  }

  function attributes(source, start = -1) {
    return ` data-math-source="${escape(source).replace(/\r/g, '&#13;').replace(/\n/g, '&#10;')}" data-math-start="${Number.isInteger(start) ? start : -1}"`;
  }

  function selectedText(range, element) {
    if (!range.intersectsNode(element)) return '';
    const part = element.ownerDocument.createRange();
    part.selectNodeContents(element);
    if (range.compareBoundaryPoints(Range.START_TO_START, part) > 0) part.setStart(range.startContainer, range.startOffset);
    if (range.compareBoundaryPoints(Range.END_TO_END, part) < 0) part.setEnd(range.endContainer, range.endOffset);
    return part.toString().replace(/[\u200b\u2060]/g, '').trim();
  }

  function formulaSelection(range, math) {
    const source = math.dataset.mathSource;
    const mapped = [...math.querySelectorAll('[data-tlm-start][data-tlm-end]')];
    const chosen = mapped.filter(span => selectedText(range, span));
    let start = 0, end = source.length;
    const full = math.ownerDocument.createRange(); full.selectNodeContents(math);
    const coversFormula = range.compareBoundaryPoints(Range.START_TO_START, full) <= 0 && range.compareBoundaryPoints(Range.END_TO_END, full) >= 0;
    if (chosen.length && !coversFormula) {
      start = Math.min(...chosen.map(span => Number(span.dataset.tlmStart)));
      end = Math.max(...chosen.map(span => Number(span.dataset.tlmEnd)));
      const structures = JSON.parse(math.querySelector('[data-math-structures]')?.dataset.mathStructures || '[]');
      for (const [left, right] of structures.sort((a, b) => (a[1] - a[0]) - (b[1] - b[0]))) {
        const members = mapped.filter(span => Number(span.dataset.tlmStart) >= left && Number(span.dataset.tlmEnd) <= right);
        if (members.length > 1 && members.every(span => chosen.includes(span)) && start <= Number(members[0].dataset.tlmStart) && end >= Math.max(...members.map(span => Number(span.dataset.tlmEnd)))) {
          start = Math.min(start, left); end = Math.max(end, right);
        }
      }
    }
    if (start < 0 || end > source.length || start >= end) return null;
    const offset = Number(math.dataset.mathStart);
    return { quote: source.slice(start, end), start: offset >= 0 ? offset + start : null,
      end: offset >= 0 ? offset + end : null, formula: source, element: math };
  }

  function selection(range, scope, source) {
    const formulas = [...(scope.matches('[data-math-source]') ? [scope] : []), ...scope.querySelectorAll('[data-math-source]')].filter(math => selectedText(range, math));
    const fragment = range.cloneContents();
    fragment.querySelectorAll?.('[data-math-source]').forEach(el => el.remove());
    const startEl = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
    const endEl = range.endContainer.nodeType === 1 ? range.endContainer : range.endContainer.parentElement;
    if (formulas.length === 1 && ((formulas[0].contains(startEl) && formulas[0].contains(endEl)) || !fragment.textContent.trim())) {
      return formulaSelection(range, formulas[0]);
    }
    if (!formulas.length || typeof source !== 'string') return null;
    // Walk prose and formulas in document order. Formula source positions anchor
    // each prose interval, so identical glyphs in different formulas stay distinct.
    const nodes = [], walker = scope.ownerDocument.createTreeWalker(scope, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(node) {
        if (node.nodeType === 1) return node.matches('[data-math-source]') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        return node.parentElement.closest('[data-math-source]') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
    });
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === 1 && node.parentElement.closest('[data-math-source]')) continue;
      nodes.push(node);
    }
    let cursor = Number(scope.dataset.start || 0), start = null, end = null;
    const limit = Number(scope.dataset.end || source.length);
    for (let index = 0; index < nodes.length; index++) {
      const node = nodes[index];
      if (node.nodeType === 1) {
        const offset = Number(node.dataset.mathStart);
        if (offset < cursor || offset < 0 || source.slice(offset, offset + node.dataset.mathSource.length) !== node.dataset.mathSource) return null;
        if (selectedText(range, node)) {
          const value = formulaSelection(range, node);
          if (!value || value.start == null) return null;
          start ??= value.start; end = value.end;
        }
        cursor = offset + node.dataset.mathSource.length;
        continue;
      }
      const text = node.textContent;
      if (!text) continue;
      const next = nodes.slice(index + 1).find(item => item.nodeType === 1);
      const right = next ? Number(next.dataset.mathStart) : limit;
      const raw = source.slice(cursor, right), positions = [], chars = [];
      // Decode Markdown escapes/entities while retaining raw UTF-16 boundaries.
      for (let at = 0; at < raw.length;) {
        const entity = raw.slice(at).match(/^&(?:#\d+|#x[\da-f]+|[a-z][\da-z]+);/i);
        let value = raw[at], width = 1;
        if (entity) {
          const decoder = scope.ownerDocument.createElement('textarea'); decoder.innerHTML = entity[0];
          value = decoder.value; width = entity[0].length;
        } else if (raw[at] === '\\' && /[!"#$%&'()*+,\-./:;<=>?@[\]\\^_`{|}~]/.test(raw[at + 1] || '')) {
          value = raw[at + 1]; width = 2;
        } else if (raw[at] === '\r' && raw[at + 1] === '\n') { value = '\n'; width = 2; }
        for (let i = 0; i < value.length; i++) { chars.push(value[i]); positions.push([cursor + at, cursor + at + width]); }
        at += width;
      }
      const at = chars.join('').indexOf(text);
      if (at < 0) return null;
      if (range.intersectsNode(node)) {
        const left = range.startContainer === node ? range.startOffset : 0;
        const right = range.endContainer === node ? range.endOffset : text.length;
        const part = node.ownerDocument.createRange(); part.setStart(node, left); part.setEnd(node, right);
        if (range.compareBoundaryPoints(Range.START_TO_START, part) <= 0 && range.compareBoundaryPoints(Range.END_TO_END, part) >= 0 && left < right) {
          start ??= positions[at + left][0]; end = positions[at + right - 1][1];
        }
      }
      cursor = positions[at + text.length - 1][1];
    }
    if (start == null || end == null || start >= end) return null;
    while (start < end && /\s/.test(source[start])) start++;
    while (end > start && /\s/.test(source[end - 1])) end--;
    return start < end ? { quote: source.slice(start, end), start, end } : null;
  }

  function targets(scope, start, end) {
    const result = [];
    for (const math of scope.querySelectorAll('[data-math-source]')) {
      const offset = Number(math.dataset.mathStart), limit = offset + math.dataset.mathSource.length;
      if (offset < 0 || start >= limit || end <= offset) continue;
      if (start <= offset && end >= limit) { result.push(math); continue; }
      result.push(...[...math.querySelectorAll('[data-tlm-start][data-tlm-end]')].filter(el =>
        offset + Number(el.dataset.tlmStart) < end && offset + Number(el.dataset.tlmEnd) > start));
    }
    return result;
  }

  const api = { render, attributes, selection, targets };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TutorMathSelection = api;
})(typeof window === 'object' ? window : globalThis);
