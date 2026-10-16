/**
 * Markdown reader adapter (browser-side ES module, no dependencies).
 *
 * Implements the ReaderAdapter surface from docs/document-readers.md §1 for
 * `.md`/`.txt` documents. Trust rules follow the same doc:
 *
 * - Raw HTML in the markdown source is escaped, never passed through. CommonMark
 *   allows raw-HTML passthrough, so this is an explicit choice, not a default.
 * - `javascript:`/`data:`/`vbscript:`/`file:`/`blob:` URLs are rejected; external
 *   links are marked `data-external` so wiring must ask the user before navigating.
 * - Images are never auto-loaded: local images become placeholders carrying
 *   `data-src`, external images are blocked outright, `..` traversal is refused.
 * - The canonical text is the document source; every anchor is expressed in
 *   codepoints. DOM offsets (UTF-16) are converted through `reader-offsets.js`,
 *   never mixed into one anchor.
 *
 * The adapter is headless on purpose: it produces sanitized HTML strings, outline
 * data, search hits and reference drafts. Mounting, virtualization and scrolling
 * belong to the future workspace wiring. An injected `renderer` (markdown -> HTML)
 * may replace the built-in renderer; its output is always allowlist-sanitized.
 */

import { createEmitter } from './reader-events.js';
import { READER_ERROR_CODES, readerError, toReaderError } from './reader-errors.js';
import { codepointToUtf16Index, utf16ToCodepointIndex } from './reader-offsets.js';
import {
  createReferenceDraft,
  makeDocumentRevisionRef,
  prefixWindow,
  suffixWindow,
  validateReferenceDraft,
} from './reference-draft.js';

const DEFAULT_MAX_RESULTS = 200;
const EXTRACTOR_VERSION = 'frontend-markdown-1';
const QUOTE_CONTEXT_LENGTH = 32;

const ALLOWED_TAGS = new Set([
  'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
  'blockquote', 'pre', 'code', 'em', 'strong', 'del', 'span', 'a',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
]);
const ALLOWED_ATTRIBUTES = new Map([
  ['a', new Set(['href', 'title', 'rel', 'target', 'data-external'])],
  ['span', new Set(['class', 'data-src', 'role'])],
  ['code', new Set(['class'])],
  ['th', new Set(['align'])],
  ['td', new Set(['align'])],
]);
const VOID_TAGS = new Set(['br', 'hr']);
const DANGEROUS_SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;
const BLOCKED_SCHEMES = new Set(['javascript', 'data', 'vbscript', 'file', 'blob']);

export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function urlScheme(url) {
  const match = DANGEROUS_SCHEME.exec(String(url).trim());
  return match ? match[0].slice(0, -1).toLowerCase() : null;
}

function isBlockedScheme(url) {
  const scheme = urlScheme(url);
  return scheme !== null && BLOCKED_SCHEMES.has(scheme);
}

/** A same-origin, project-relative resource path; `..` traversal is refused. */
function isLocalResource(url) {
  const trimmed = String(url).trim();
  if (urlScheme(trimmed) !== null) return false;
  if (!trimmed.startsWith('/') && !trimmed.startsWith('./')) return false;
  return !trimmed.split('/').some(part => part === '..');
}

function renderLink(innerHtml, url, label) {
  if (!url || isBlockedScheme(url)) {
    // The dangerous target is dropped entirely: no navigation, and the scheme
    // string is not echoed back into the DOM either.
    return `<span class="tutorlm-md-blocked-link">${innerHtml}</span>`;
  }
  const href = escapeHtml(url.trim());
  return `<a href="${href}" data-external="true" rel="noopener noreferrer nofollow" target="_blank" title="${escapeHtml(label)}">${innerHtml}</a>`;
}

function renderImagePlaceholder(alt, url) {
  if (isLocalResource(url)) {
    // Authorized same-origin resource: still not loaded until the user asks.
    return `<span class="tutorlm-md-image" role="img" data-src="${escapeHtml(url.trim())}">${escapeHtml(alt)}</span>`;
  }
  // External image: blocked outright, and the remote URL is not echoed into the DOM.
  return `<span class="tutorlm-md-blocked-image" role="img">${escapeHtml(alt)}</span>`;
}

/**
 * Escape a text run while leaving already-formed character entities intact.
 * The built-in renderer escapes raw HTML itself, so re-escaping here would show the
 * user a double-escaped `&amp;lt;`. Values that were never escaped are escaped once.
 */
function escapeTextPreservingEntities(text) {
  return String(text)
    .replace(/&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});)/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Strict allowlist sanitizer. Applied to the built-in renderer output *and* to any
 * injected renderer, so a third-party markdown library can never inject script,
 * event handlers, iframes, `<img>` auto-loading or `javascript:` targets.
 */
export function sanitizeHtml(html) {
  if (typeof html !== 'string') {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'sanitizeHtml expects a string');
  }
  const stripped = html
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<!\s*(?:doctype|\[CDATA)[\s\S]*?>/gi, '')
    .replace(/<(script|style|iframe|object|embed|link|meta|base|form)\b[\s\S]*?<\/\1\s*>/gi, '')
    .replace(/<(script|style|iframe|object|embed|link|meta|base|form|img|input|svg|math|video|audio|source)\b[^>]*?>/gi, '');

  let output = '';
  let cursor = 0;
  const tagPattern = /<\/?([a-zA-Z][a-zA-Z0-9-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)\/?>/g;
  let match;
  while ((match = tagPattern.exec(stripped)) !== null) {
    output += escapeTextPreservingEntities(stripped.slice(cursor, match.index));
    cursor = match.index + match[0].length;
    const name = match[1].toLowerCase();
    const isClosing = match[0][1] === '/';
    if (!ALLOWED_TAGS.has(name)) continue;
    if (isClosing) {
      if (!VOID_TAGS.has(name)) output += `</${name}>`;
      continue;
    }
    const attributes = sanitizeAttributes(name, match[2] || '');
    output += VOID_TAGS.has(name) ? `<${name}${attributes} />` : `<${name}${attributes}>`;
  }
  return output + escapeTextPreservingEntities(stripped.slice(cursor));
}

function sanitizeAttributes(tag, raw) {
  const allowed = ALLOWED_ATTRIBUTES.get(tag);
  if (!allowed) return '';
  const attributePattern = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))/g;
  let output = '';
  let match;
  while ((match = attributePattern.exec(raw)) !== null) {
    const name = match[1].toLowerCase();
    if (!allowed.has(name) || name.startsWith('on')) continue;
    const value = match[2] ?? match[3] ?? match[4] ?? '';
    if ((name === 'href' || name === 'data-src') && isBlockedScheme(value)) continue;
    if (name === 'data-src' && !isLocalResource(value)) continue;
    output += ` ${name}="${escapeHtml(value)}"`;
  }
  return output;
}

function parseLinkLike(source, start, end) {
  const close = source.indexOf(']', start + 1);
  if (close === -1 || close >= end || source[close + 1] !== '(') return null;
  const paren = source.indexOf(')', close + 2);
  if (paren === -1 || paren >= end) return null;
  return { labelStart: start + 1, labelEnd: close, url: source.slice(close + 2, paren).trim(), next: paren + 1 };
}

const EMPHASIS_MARKERS = [['**', 'strong'], ['__', 'strong'], ['~~', 'del'], ['*', 'em'], ['_', 'em']];

function matchEmphasis(source, index, end) {
  for (const [marker, tag] of EMPHASIS_MARKERS) {
    if (!source.startsWith(marker, index)) continue;
    const close = source.indexOf(marker, index + marker.length);
    if (close === -1 || close + marker.length > end) continue;
    const contentStart = index + marker.length;
    if (close <= contentStart) continue;
    if (source[contentStart] === ' ' || source[close - 1] === ' ') continue;
    return { tag, contentStart, contentEnd: close, next: close + marker.length };
  }
  return null;
}

const INLINE_SPECIALS = new Set(['\\', '`', '!', '[', '*', '_', '~']);

/** Parse inline markdown, tracking the source range of every literal run. */
function parseInline(source, start, end) {
  const parts = [];
  const html = [];
  const pushText = (text, from, to) => {
    if (!text.length) return;
    parts.push({ text, sourceStart: from, sourceEnd: to });
    html.push(escapeHtml(text));
  };
  let index = start;
  while (index < end) {
    const char = source[index];
    if (char === '\\' && index + 1 < end) {
      pushText(source[index + 1], index + 1, index + 2);
      index += 2;
      continue;
    }
    if (char === '`') {
      const close = source.indexOf('`', index + 1);
      if (close !== -1 && close < end) {
        const content = source.slice(index + 1, close);
        parts.push({ text: content, sourceStart: index + 1, sourceEnd: close });
        html.push(`<code>${escapeHtml(content)}</code>`);
        index = close + 1;
        continue;
      }
    }
    if (char === '!' && source[index + 1] === '[') {
      const parsed = parseLinkLike(source, index + 1, end);
      if (parsed) {
        const alt = source.slice(parsed.labelStart, parsed.labelEnd);
        parts.push({ text: alt, sourceStart: parsed.labelStart, sourceEnd: parsed.labelEnd });
        html.push(renderImagePlaceholder(alt, parsed.url));
        index = parsed.next;
        continue;
      }
    }
    if (char === '[') {
      const parsed = parseLinkLike(source, index, end);
      if (parsed) {
        const inner = parseInline(source, parsed.labelStart, parsed.labelEnd);
        parts.push(...inner.parts);
        html.push(renderLink(inner.html, parsed.url, source.slice(parsed.labelStart, parsed.labelEnd)));
        index = parsed.next;
        continue;
      }
    }
    const emphasis = matchEmphasis(source, index, end);
    if (emphasis) {
      const inner = parseInline(source, emphasis.contentStart, emphasis.contentEnd);
      parts.push(...inner.parts);
      html.push(`<${emphasis.tag}>${inner.html}</${emphasis.tag}>`);
      index = emphasis.next;
      continue;
    }
    if (INLINE_SPECIALS.has(char)) {
      pushText(char, index, index + 1);
      index += 1;
      continue;
    }
    let stop = index;
    while (stop < end && !INLINE_SPECIALS.has(source[stop])) stop += 1;
    pushText(source.slice(index, stop), index, stop);
    index = stop;
  }
  return { html: html.join(''), parts };
}

function indentOf(line) {
  const match = /^( {0,3})/.exec(line);
  return match[1].length;
}

const LIST_MARKER = /^( {0,3})([-*+]|\d{1,9}[.)])(\s+)/;
const HEADING = /^( {0,3})(#{1,6})(\s+)(.*)$/;
const HORIZONTAL_RULE = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;
const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;

function block(entries, kind, html, parts, extra) {
  return {
    kind,
    html,
    parts,
    sourceStart: entries[0].start,
    sourceEnd: entries[entries.length - 1].end,
    ...extra,
  };
}

function parseParagraph(source, entries) {
  const parts = [];
  const html = [];
  entries.forEach((entry, position) => {
    const inline = parseInline(source, entry.start, entry.end);
    if (position > 0) parts.push({ text: ' ', sourceStart: entry.start, sourceEnd: entry.start });
    parts.push(...inline.parts);
    html.push(inline.html);
  });
  return block(entries, 'paragraph', `<p>${html.join(' ')}</p>`, parts);
}

/**
 * Built-in block parser: headings, paragraphs, fenced code, blockquotes, list
 * items and thematic breaks, plus inline emphasis/code/links/images. Tables and
 * reference links are intentionally unsupported and stay literal text.
 */
function parseBlocks(source) {
  const lines = [];
  let lineStart = 0;
  for (let index = 0; index <= source.length; index += 1) {
    if (index === source.length || source[index] === '\n') {
      lines.push({ text: source.slice(lineStart, index), start: lineStart, end: index });
      lineStart = index + 1;
    }
  }

  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.text.trim()) {
      index += 1;
      continue;
    }
    const fence = FENCE.exec(line.text);
    if (fence) {
      const marker = fence[1][0] === '`' ? '`' : '~';
      const closePattern = new RegExp(`^ {0,3}${marker === '`' ? '`' : '~'}{3,}\\s*$`);
      const content = [];
      let scan = index + 1;
      while (scan < lines.length && !closePattern.test(lines[scan].text)) {
        content.push(lines[scan]);
        scan += 1;
      }
      if (scan >= lines.length) {
        // Unterminated fence stays ordinary text instead of guessing an end.
        blocks.push(parseParagraph(source, [line]));
        index += 1;
        continue;
      }
      blocks.push(block(
        [line, ...content, lines[scan]],
        'code',
        `<pre><code>${content.map(entry => escapeHtml(entry.text)).join('\n')}</code></pre>`,
        content.map(entry => ({ text: entry.text, sourceStart: entry.start, sourceEnd: entry.end })),
      ));
      index = scan + 1;
      continue;
    }
    const heading = HEADING.exec(line.text);
    if (heading) {
      const level = heading[2].length;
      const contentStart = line.start + heading[1].length + heading[2].length + heading[3].length;
      const inline = parseInline(source, contentStart, line.end);
      blocks.push({
        kind: 'heading',
        level,
        html: `<h${level}>${inline.html}</h${level}>`,
        parts: inline.parts,
        sourceStart: line.start,
        sourceEnd: line.end,
      });
      index += 1;
      continue;
    }
    if (HORIZONTAL_RULE.test(line.text)) {
      blocks.push({ kind: 'thematic-break', html: '<hr />', parts: [], sourceStart: line.start, sourceEnd: line.end });
      index += 1;
      continue;
    }
    const list = LIST_MARKER.exec(line.text);
    if (list) {
      const contentStart = line.start + list[1].length + list[2].length + list[3].length;
      const inline = parseInline(source, contentStart, line.end);
      blocks.push(block([line], 'list-item', `<ul><li>${inline.html}</li></ul>`, inline.parts));
      index += 1;
      continue;
    }
    if (indentOf(line.text) < 4 && line.text.startsWith('>')) {
      const collected = [];
      let scan = index;
      while (scan < lines.length && lines[scan].text.startsWith('>')) {
        const entry = lines[scan];
        const afterMarker = entry.text.slice(1);
        const offset = 1 + (afterMarker.startsWith(' ') ? 1 : 0);
        collected.push({ inline: parseInline(source, entry.start + offset, entry.end), entry });
        scan += 1;
      }
      const parts = [];
      const inner = [];
      collected.forEach((item, position) => {
        if (position > 0) parts.push({ text: ' ', sourceStart: item.entry.start, sourceEnd: item.entry.start });
        parts.push(...item.inline.parts);
        inner.push(item.inline.html);
      });
      blocks.push(block(collected.map(item => item.entry), 'blockquote', `<blockquote><p>${inner.join(' ')}</p></blockquote>`, parts));
      index = scan;
      continue;
    }
    const paragraph = [];
    let scan = index;
    while (scan < lines.length) {
      const entry = lines[scan];
      if (!entry.text.trim()) break;
      if (HEADING.test(entry.text) || HORIZONTAL_RULE.test(entry.text) || FENCE.test(entry.text)) break;
      if (LIST_MARKER.test(entry.text) || entry.text.startsWith('>')) break;
      paragraph.push(entry);
      scan += 1;
    }
    blocks.push(parseParagraph(source, paragraph));
    index = scan;
  }
  return blocks;
}

/**
 * Build the content model: sanitized HTML plus a plain-text projection whose every
 * codepoint maps back to a source codepoint. The map is what lets search hits and
 * DOM selections produce honest codepoint anchors across the render/plain gap.
 */
export function buildMarkdownModel(source, { renderer } = {}) {
  if (typeof source !== 'string') {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'Markdown source must be a string');
  }
  const blocks = parseBlocks(source);
  const records = [];
  let projection = '';
  blocks.forEach((entry, position) => {
    const plain = entry.parts.map(part => part.text).join('');
    records.push({ ...entry, plain, projectionStart: projection.length, projectionEnd: projection.length + plain.length });
    projection += plain;
    if (position < blocks.length - 1) projection += '\n';
  });

  // projection UTF-16 index -> source codepoint index.
  // Keyed by UTF-16 offset because DOM selections and String#indexOf both work in
  // UTF-16 units; the values are codepoints, which is what anchors persist.
  const map = new Array(projection.length).fill(0);
  let lastSource = 0;
  for (const record of records) {
    let cursor = record.projectionStart;
    for (const part of record.parts) {
      const sourceBase = utf16ToCodepointIndex(source, part.sourceStart);
      let offset = 0;
      for (const char of part.text) {
        const width = char.length;
        for (let unit = 0; unit < width; unit += 1) map[cursor + unit] = sourceBase + offset;
        cursor += width;
        offset += 1;
      }
      lastSource = utf16ToCodepointIndex(source, part.sourceEnd);
    }
    // Synthetic space where joined source lines collapse into projection.
    for (let filler = cursor; filler < record.projectionEnd; filler += 1) map[filler] = lastSource;
  }
  // Synthetic '\n' between blocks anchors on the following block's source start.
  for (let position = 0; position < records.length - 1; position += 1) {
    map[records[position].projectionEnd] = utf16ToCodepointIndex(source, records[position + 1].sourceStart);
  }

  const html = renderer
    ? sanitizeHtml(String(renderer(source)))
    : sanitizeHtml(records.map(record => record.html).join('\n'));

  return { blocks: records, projection, map, html, source };
}

function outlineFrom(blocks) {
  const items = [];
  const stack = [];
  blocks.forEach((entry, index) => {
    if (entry.kind !== 'heading') return;
    const node = { id: `heading-${index}`, title: entry.plain, level: entry.level, blockIndex: index, children: [] };
    while (stack.length && stack[stack.length - 1].level >= node.level) stack.pop();
    if (stack.length) stack[stack.length - 1].children.push(node);
    else items.push(node);
    stack.push(node);
  });
  return items;
}

function normalizeQuery(query) {
  const text = typeof query === 'string' ? query : query && query.text;
  if (typeof text !== 'string' || text.length === 0) {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'search needs a non-empty query text');
  }
  return { text, matchCase: Boolean(query && query.matchCase), wholeWord: Boolean(query && query.wholeWord) };
}

const WORD_CHARACTER = /[\p{L}\p{N}_]/u;

/**
 * Create the markdown reader adapter.
 *
 * options.loadText: async (ref) => string — injected content loader
 *   (default: `GET /api/documents/{id}/content` through `environment.fetch`).
 * options.renderer: markdown -> HTML; its output is always sanitized.
 */
export function createMarkdownReader({ environment = globalThis, loadText, renderer } = {}) {
  const emitter = createEmitter(['selection', 'reference', 'viewchange', 'error']);
  let ref = null;
  let model = null;
  let closed = false;
  let aborted = false;
  let generation = 0;
  let zoom = { kind: 'scale', value: 1 };
  let position = null;
  let highlights = [];

  function requireOpen() {
    if (closed) throw readerError(READER_ERROR_CODES.CLOSED, 'The reader is closed');
    if (aborted) throw readerError(READER_ERROR_CODES.ABORTED, 'The reader was aborted');
    if (!model || !ref) throw readerError(READER_ERROR_CODES.NOT_OPEN, 'No document is open');
    return model;
  }

  async function defaultLoadText(currentRef) {
    if (!environment || typeof environment.fetch !== 'function') {
      throw readerError(READER_ERROR_CODES.LOAD_FAILED, 'No content loader and no fetch available');
    }
    const url = `/api/documents/${encodeURIComponent(currentRef.document_id)}/content`;
    const response = await environment.fetch(url, { credentials: 'same-origin' });
    if (response.status === 410) throw readerError(READER_ERROR_CODES.LOAD_FAILED, 'The document revision is gone');
    if (!response.ok) throw readerError(READER_ERROR_CODES.LOAD_FAILED, `Content request failed (${response.status})`);
    const payload = await response.json();
    if (!payload || typeof payload.text !== 'string') {
      throw readerError(READER_ERROR_CODES.LOAD_FAILED, 'The content response has no text');
    }
    return payload.text;
  }

  function emitViewChange(reason) {
    emitter.emit('viewchange', {
      reason,
      document_id: ref ? ref.document_id : null,
      revision_id: ref ? ref.revision_id : null,
      zoom: { ...zoom },
      position: position ? { ...position } : null,
      outline: model ? outlineFrom(model.blocks) : [],
      highlight_count: highlights.length,
      ready: model !== null,
    });
  }

  function selectionFromSourceRange(range, unit = 'codepoint') {
    const current = requireOpen();
    if (!range || !Number.isInteger(range.start) || !Number.isInteger(range.end) || range.end < range.start) {
      throw readerError(READER_ERROR_CODES.INVALID_OFFSET, 'A selection requires integer start <= end', { range });
    }
    let codepointRange = range;
    if (unit === 'utf16') {
      codepointRange = {
        start: utf16ToCodepointIndex(current.source, range.start),
        end: utf16ToCodepointIndex(current.source, range.end),
      };
    } else if (unit !== 'codepoint') {
      throw readerError(READER_ERROR_CODES.INVALID_OFFSET, `Unknown offset unit ${String(unit)}`, { unit });
    }
    const utf16 = {
      start: codepointToUtf16Index(current.source, codepointRange.start),
      end: codepointToUtf16Index(current.source, codepointRange.end),
    };
    return createReferenceDraft(ref, {
      selector: { type: 'text', start: codepointRange.start, end: codepointRange.end, unit: 'codepoint' },
      quote: current.source.slice(utf16.start, utf16.end),
      prefix: prefixWindow(current.source, utf16.start, QUOTE_CONTEXT_LENGTH),
      suffix: suffixWindow(current.source, utf16.end, QUOTE_CONTEXT_LENGTH),
      extractor_version: EXTRACTOR_VERSION,
    });
  }

  function sourceEndOfProjectionIndex(current, projectionIndex) {
    const entry = current.blocks.find(item => item.projectionStart <= projectionIndex && projectionIndex <= item.projectionEnd);
    return entry ? utf16ToCodepointIndex(current.source, entry.sourceEnd) : current.map[projectionIndex] ?? 0;
  }

  return {
    async open(nextRef, { text } = {}) {
      if (closed) throw readerError(READER_ERROR_CODES.CLOSED, 'The reader is closed');
      const bound = makeDocumentRevisionRef(nextRef || {});
      const token = ++generation;
      aborted = false;
      const loader = loadText || defaultLoadText;
      let source;
      try {
        source = typeof text === 'string' ? text : await loader(bound);
      } catch (error) {
        if (token !== generation) throw readerError(READER_ERROR_CODES.ABORTED, 'The open call was aborted');
        throw toReaderError(error, READER_ERROR_CODES.LOAD_FAILED, 'Markdown content could not be loaded');
      }
      if (token !== generation) throw readerError(READER_ERROR_CODES.ABORTED, 'The open call was aborted');
      ref = bound;
      model = buildMarkdownModel(source, { renderer });
      position = model.blocks.length ? { blockIndex: 0 } : null;
      highlights = [];
      emitViewChange('open');
      return { document_id: bound.document_id, revision_id: bound.revision_id, block_count: model.blocks.length };
    },
    close() {
      closed = true;
      aborted = false;
      generation += 1;
      ref = null;
      model = null;
      highlights = [];
      position = null;
      emitter.clearListeners();
    },
    /** Abort in-flight loading; a pending open rejects with reader_aborted. */
    abort() {
      aborted = true;
      generation += 1;
      emitViewChange('abort');
    },
    /** Drop content state on document switch so old anchors cannot leak across. */
    clear() {
      model = null;
      ref = null;
      highlights = [];
      position = null;
      aborted = false;
      generation += 1;
      emitViewChange('clear');
    },
    getText() {
      return requireOpen().source;
    },
    getProjection() {
      return requireOpen().projection;
    },
    getRender() {
      return requireOpen().html;
    },
    getOutline() {
      return Promise.resolve(outlineFrom(requireOpen().blocks));
    },
    async search(query, options = {}) {
      const current = requireOpen();
      const normalized = normalizeQuery(query);
      const maxResults = Number.isInteger(options.maxResults) && options.maxResults > 0 ? options.maxResults : DEFAULT_MAX_RESULTS;
      const haystack = normalized.matchCase ? current.projection : current.projection.toLowerCase();
      const needle = normalized.matchCase ? normalized.text : normalized.text.toLowerCase();
      const token = generation;
      const results = [];
      let index = 0;
      while (index + needle.length <= haystack.length) {
        if (token !== generation || aborted) break;
        const found = haystack.indexOf(needle, index);
        if (found === -1) break;
        const end = found + needle.length;
        const before = found > 0 ? current.projection[found - 1] : ' ';
        const after = end < current.projection.length ? current.projection[end] : ' ';
        if (normalized.wholeWord && (WORD_CHARACTER.test(before) || WORD_CHARACTER.test(after))) {
          index = found + 1;
          continue;
        }
        results.push({
          index: results.length,
          blockIndex: current.blocks.findIndex(item => item.projectionStart <= found && found <= item.projectionEnd),
          projection_range: { start: found, end, unit: 'codepoint' },
          source_range: { start: current.map[found], end: end < current.map.length ? current.map[end] : sourceEndOfProjectionIndex(current, found), unit: 'codepoint' },
          quote: current.projection.slice(found, end),
          prefix: current.projection.slice(Math.max(0, found - QUOTE_CONTEXT_LENGTH), found),
          suffix: current.projection.slice(end, end + QUOTE_CONTEXT_LENGTH),
        });
        if (results.length >= maxResults) {
          results.truncated = true;
          break;
        }
        index = found + Math.max(1, needle.length);
        await Promise.resolve();
      }
      results.truncated = Boolean(results.truncated);
      return results;
    },
    /** Build a draft from DOM (UTF-16 by default) offsets and emit selection/reference. */
    selectRange(start, end, unit = 'utf16') {
      const draft = selectionFromSourceRange({ start, end }, unit);
      emitter.emit('selection', {
        document_id: draft.document_id,
        revision_id: draft.revision_id,
        selector: draft.selector,
        quote: draft.quote,
        prefix: draft.prefix,
        suffix: draft.suffix,
      });
      emitter.emit('reference', draft);
      return draft;
    },
    selectionFromSourceRange,
    async goTo(target = {}) {
      const current = requireOpen();
      if (Number.isInteger(target.blockIndex)) {
        if (target.blockIndex < 0 || target.blockIndex >= current.blocks.length) {
          throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'blockIndex is outside the document', { target });
        }
        position = { blockIndex: target.blockIndex };
      } else if (target.sourceRange && Number.isInteger(target.sourceRange.start)) {
        const found = current.blocks.findIndex(item => item.sourceStart <= target.sourceRange.start && target.sourceRange.start <= item.sourceEnd);
        if (found === -1) throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'sourceRange is outside the document', { target });
        position = { blockIndex: found };
      } else {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'goTo needs blockIndex or sourceRange', { target });
      }
      emitViewChange('navigate');
      return { ...position };
    },
    setZoom(level) {
      if (!level || (level.kind !== 'scale' && level.kind !== 'fit-width')) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'zoom must be {kind:"scale"|"fit-width"}', { level });
      }
      if (level.kind === 'scale' && !(typeof level.value === 'number' && level.value > 0)) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'scale zoom needs a positive value', { level });
      }
      zoom = { ...level };
      emitViewChange('zoom');
    },
    /** Validate and remember highlight anchors; stored rects are never rewritten. */
    setHighlight(anchors = []) {
      requireOpen();
      highlights = anchors.map(anchor => validateReferenceDraft(anchor));
      emitViewChange('highlight');
      return highlights.length;
    },
    getHighlights() {
      return highlights.map(anchor => ({ ...anchor }));
    },
    viewState() {
      return {
        document_id: ref ? ref.document_id : null,
        revision_id: ref ? ref.revision_id : null,
        ready: model !== null,
        zoom: { ...zoom },
        position: position ? { ...position } : null,
        outline: model ? outlineFrom(model.blocks) : [],
        highlight_count: highlights.length,
      };
    },
    on(event, listener) {
      return emitter.on(event, listener);
    },
  };
}