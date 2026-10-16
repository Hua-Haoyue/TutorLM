import test from 'node:test';
import assert from 'node:assert/strict';

import {
  READER_ERROR_CODES,
  isReaderError,
  readerError,
} from '../assets/reader-errors.js';
import {
  codepointLength,
  utf16Length,
  utf16ToCodepointIndex,
  codepointToUtf16Index,
  utf16ToCodepointRange,
  convertRange,
  sliceCodepoints,
} from '../assets/reader-offsets.js';
import {
  PDF_COORDINATE_SPACE,
  createReferenceDraft,
  validateReferenceDraft,
  normalizeSelector,
  normalizeResourcePath,
  makeDocumentRevisionRef,
} from '../assets/reference-draft.js';
import { createMarkdownReader, buildMarkdownModel, sanitizeHtml } from '../assets/markdown-reader.js';
import {
  createPdfReader,
  resolvePdfEngine,
  screenPointToCanonical,
  canonicalRectToScreen,
  rotateCanonicalToScreen,
  rotateScreenToCanonical,
} from '../assets/pdf-reader.js';
import {
  compileProject,
  createTexSourceReader,
  createTexCompiledReader,
  normalizeTexProject,
} from '../assets/tex-reader.js';
import { createReaderRegistry, detectFormat, SUPPORTED_FORMATS } from '../assets/reader-registry.js';

const REF = { document_id: 'doc-1', revision_id: 'rev-1' };

// ---------------------------------------------------------------- offsets ----

test('codepoint and UTF-16 lengths differ only for astral characters', () => {
  assert.equal(utf16Length('ab'), 2);
  assert.equal(codepointLength('ab'), 2);
  assert.equal(utf16Length('🎉'), 2);
  assert.equal(codepointLength('🎉'), 1);
  assert.equal(codepointLength('a🎉b'), 3);
  assert.equal(utf16Length('a🎉b'), 4);
});

test('offset conversion is exact across astral characters', () => {
  const text = 'a🎉b';
  // utf16: a=0..1, emoji=1..3, b=3..4 ; codepoint: a=0, emoji=1, b=2
  assert.equal(utf16ToCodepointIndex(text, 0), 0);
  assert.equal(utf16ToCodepointIndex(text, 1), 1);
  assert.equal(utf16ToCodepointIndex(text, 3), 2);
  assert.equal(utf16ToCodepointIndex(text, 4), 3);
  assert.equal(codepointToUtf16Index(text, 2), 3);
  assert.equal(codepointToUtf16Index(text, 3), 4);
});

test('a UTF-16 index inside a surrogate pair is rejected, never split', () => {
  assert.throws(() => utf16ToCodepointIndex('a🎉b', 2), error => isReaderError(error, READER_ERROR_CODES.INVALID_OFFSET));
});

test('goTo-style round trip over an emoji stays stable', () => {
  const text = 'x🎉y';
  for (const index of [0, 1, 2, 3]) {
    const utf16 = codepointToUtf16Index(text, index);
    assert.equal(utf16ToCodepointIndex(text, utf16), index);
  }
});

test('codepoint range conversion and slicing never cut a surrogate pair', () => {
  const text = 'x🎉y';
  assert.deepEqual(utf16ToCodepointRange(text, 1, 3), { start: 1, end: 2 });
  assert.equal(sliceCodepoints(text, 1, 2), '🎉');
  const converted = convertRange(text, { start: 1, end: 2 }, 'codepoint');
  assert.deepEqual(converted.utf16, { start: 1, end: 3 });
  assert.deepEqual(converted.codepoint, { start: 1, end: 2 });
});

test('out-of-range offsets are rejected with invalid_offset', () => {
  assert.throws(() => utf16ToCodepointIndex('ab', 5), error => isReaderError(error, READER_ERROR_CODES.INVALID_OFFSET));
  assert.throws(() => codepointToUtf16Index('ab', -1), error => isReaderError(error, READER_ERROR_CODES.INVALID_OFFSET));
});

// --------------------------------------------------------- reference draft ----

test('a text selector persists codepoint units and keeps the bound revision', () => {
  const draft = createReferenceDraft(REF, {
    selector: { type: 'text', start: 0, end: 3, unit: 'codepoint' },
    quote: 'abc',
    prefix: 'x',
    suffix: 'y',
  });
  assert.equal(draft.document_id, 'doc-1');
  assert.equal(draft.revision_id, 'rev-1');
  assert.deepEqual(draft.selector, { type: 'text', start: 0, end: 3, unit: 'codepoint' });
  assert.equal(draft.quote, 'abc');
  assert.ok(draft.created_at);
});

test('a draft cannot be re-bound to another document by its selector', () => {
  const draft = createReferenceDraft(REF, {
    selector: { type: 'text', start: 0, end: 1, unit: 'codepoint' },
    quote: 'a',
  });
  assert.equal(draft.document_id, 'doc-1');
  assert.throws(
    () => createReferenceDraft({ ...REF, document_id: '' }, { selector: { type: 'whole-document' } }),
    error => isReaderError(error, READER_ERROR_CODES.INVALID_REFERENCE),
  );
});

test('multi-page pdf selections use pages[] and a single canonical space', () => {
  const draft = createReferenceDraft(
    { ...REF, artifact_id: 'art-1', build_id: 'build-1' },
    {
      selector: {
        type: 'pdf',
        pages: [
          { page: 1, rects: [{ x: 0.1, y: 0.2, w: 0.3, h: 0.05 }] },
          { page: 2, rects: [{ x: 0.1, y: 0.3, w: 0.4, h: 0.05 }], quads: [{ x: 0.1, y: 0.3, w: 0.4, h: 0.05 }] },
        ],
      },
      quote: 'selected text',
    },
  );
  assert.equal(draft.selector.coordinate_space, PDF_COORDINATE_SPACE);
  assert.equal(draft.selector.coordinate_space, 'normalized-cropbox-unrotated-top-left');
  assert.equal(draft.selector.pages.length, 2);
  assert.equal(draft.selector.pages[1].page, 2);
  assert.equal(draft.artifact_id, 'art-1');
  assert.equal(draft.build_id, 'build-1');
  assert.equal(draft.selector.page, undefined);
});

test('a single-page pdf selection is still a pages[] array of length one', () => {
  const draft = createReferenceDraft(REF, {
    selector: { type: 'pdf', pages: [{ page: 1, rects: [{ x: 0, y: 0, w: 1, h: 1 }] }] },
    quote: 'q',
  });
  assert.equal(draft.selector.pages.length, 1);
});

test('pdf rects outside the unit square are rejected instead of clamped', () => {
  assert.throws(
    () => normalizeSelector({ type: 'pdf', pages: [{ page: 1, rects: [{ x: 1.2, y: 0, w: 0.1, h: 0.1 }] }] }),
    error => isReaderError(error, READER_ERROR_CODES.INVALID_SELECTOR),
  );
});

test('rotation is display-only metadata and does not rewrite rects', () => {
  const input = { x: 0.1, y: 0.1, w: 0.2, h: 0.2 };
  const selector = normalizeSelector({
    type: 'pdf',
    pages: [{ page: 1, rects: [input] }],
    rotation: 90,
    cropbox_transform: [1, 0, 0, 1, 0, 0],
  });
  assert.deepEqual(selector.pages[0].rects[0], input);
  assert.equal(selector.rotation, 90);
  assert.deepEqual(selector.cropbox_transform, [1, 0, 0, 1, 0, 0]);
});

test('a tex-source selector requires a canonical relative file', () => {
  assert.throws(
    () => normalizeSelector({ type: 'tex-source', start: 0, end: 1, unit: 'codepoint' }),
    error => isReaderError(error, READER_ERROR_CODES.TEX_SOURCE_FILE_REQUIRED),
  );
  assert.equal(
    normalizeResourcePath('main.tex'),
    'main.tex',
  );
  assert.throws(() => normalizeResourcePath('/etc/passwd'), error => isReaderError(error, READER_ERROR_CODES.TEX_PATH_INVALID));
  assert.throws(() => normalizeResourcePath('../../secret.tex'), error => isReaderError(error, READER_ERROR_CODES.TEX_PATH_INVALID));
});

test('a compiled pdf reference must carry artifact_id and build_id', () => {
  assert.throws(
    () => validateReferenceDraft({
      ...REF,
      selector: { type: 'pdf', pages: [{ page: 1, rects: [{ x: 0, y: 0, w: 0.1, h: 0.1 }] }] },
      quote: 'q',
    }),
    error => isReaderError(error, READER_ERROR_CODES.INVALID_REFERENCE),
  );
  assert.ok(validateReferenceDraft({
    ...REF,
    artifact_id: 'art-1',
    build_id: 'build-1',
    selector: { type: 'pdf', pages: [{ page: 1, rects: [{ x: 0, y: 0, w: 0.1, h: 0.1 }] }] },
    quote: 'q',
  }));
});

test('whole-document is an explicit placeholder with no fabricated offsets', () => {
  assert.deepEqual(normalizeSelector({ type: 'whole-document' }), { type: 'whole-document' });
  const draft = createReferenceDraft(REF, { selector: { type: 'whole-document' } });
  assert.equal(draft.quote, '');
  assert.equal(draft.selector.start, undefined);
});

test('makeDocumentRevisionRef refuses identity swaps between revision and artifact', () => {
  assert.throws(() => makeDocumentRevisionRef({ document_id: 'd', revision_id: 5 }), error => isReaderError(error, READER_ERROR_CODES.INVALID_REFERENCE));
  const ref = makeDocumentRevisionRef({ document_id: 'd', revision_id: 'r', artifact_id: 'a', build_id: 'b' });
  assert.deepEqual(ref, { document_id: 'd', revision_id: 'r', artifact_id: 'a', build_id: 'b' });
});

// ------------------------------------------------------------- sanitizer ----

test('a script block never survives sanitizing, and safe markup is kept', () => {
  const html = sanitizeHtml('<p>ok</p><script>alert(1)</script>');
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('alert(1)'), 'the script body is dropped, not merely hidden');
  assert.ok(html.includes('<p>ok</p>'));
});

test('event handlers and javascript: targets never survive sanitizing', () => {
  const html = sanitizeHtml('<a href="javascript:alert(1)" onclick="alert(2)">x</a>');
  assert.ok(!html.includes('javascript:'));
  assert.ok(!html.includes('onclick'));
});

test('images are dropped so a document cannot auto-load remote media', () => {
  const html = sanitizeHtml('<img src="http://evil.test/x.png" onerror="alert(1)">');
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('evil.test'));
});

// -------------------------------------------------------- markdown reader ----

test('markdown render escapes raw html as text and drops external images', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'Hi <img src=x onerror=alert(1)> ![alt](http://x/y.png) <b>bold</b>\n' });
  const html = reader.getRender();
  assert.ok(!html.includes('<img'), 'no image element is emitted');
  assert.ok(html.includes('&lt;img'), 'the raw html survives only as escaped text');
  assert.ok(html.includes('&lt;b&gt;bold&lt;/b&gt;'), 'raw html tags are escaped, not interpreted');
  assert.ok(!html.includes('http://x/y.png'), 'the external image is not emitted as a loadable source');
  assert.ok(html.includes('tutorlm-md-blocked-image'), 'the blocked image is explicitly marked');
});

test('markdown render blocks javascript: links and flags external ones', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: '[danger](javascript:alert(1)) and [safe](https://example.test/page)\n' });
  const html = reader.getRender();
  assert.ok(!html.includes('javascript:'));
  assert.ok(html.includes('data-external="true"'));
});

test('search returns codepoint ranges that map back to the source', async () => {
  const reader = createMarkdownReader();
  const text = '# Title\n\nHello 🎉 world\n';
  await reader.open(REF, { text });
  const hits = await reader.search('world');
  assert.equal(hits.length, 1);
  const hit = hits[0];
  assert.equal(hit.source_range.unit, 'codepoint');
  const source = reader.getText();
  const slice = sliceCodepoints(source, hit.source_range.start, hit.source_range.end);
  assert.equal(slice, 'world');
});

test('search handles astral characters without half-surrogate ranges', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'prefix 🎉 suffix\n' });
  const hits = await reader.search('🎉');
  assert.equal(hits.length, 1);
  assert.equal(sliceCodepoints(reader.getText(), hits[0].source_range.start, hits[0].source_range.end), '🎉');
  assert.equal(hits[0].source_range.start, 7);
  assert.equal(hits[0].source_range.end, 8);
});

test('search reports truncation when the result cap is reached', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'aaaa aaaa aaaa aaaa\n' });
  const hits = await reader.search('aaaa', { maxResults: 2 });
  assert.equal(hits.length, 2);
  assert.equal(hits.truncated, true);
});

test('search rejects an empty query', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'abc' });
  await assert.rejects(reader.search(''), error => isReaderError(error, READER_ERROR_CODES.INVALID_ARGUMENT));
});

test('selection from DOM (utf16) offsets yields a codepoint anchor and quote', async () => {
  const reader = createMarkdownReader();
  const text = 'x🎉y';
  await reader.open(REF, { text });
  const events = [];
  reader.on('selection', event => events.push(event));
  const draft = reader.selectRange(1, 3, 'utf16');
  assert.deepEqual(draft.selector, { type: 'text', start: 1, end: 2, unit: 'codepoint' });
  assert.equal(draft.quote, '🎉');
  assert.equal(events.length, 1);
  assert.equal(events[0].document_id, 'doc-1');
  assert.equal(events[0].revision_id, 'rev-1');
});

test('selection from a split surrogate is rejected, not rounded', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'x🎉y' });
  assert.throws(() => reader.selectRange(2, 3, 'utf16'), error => isReaderError(error, READER_ERROR_CODES.INVALID_OFFSET));
});

test('the outline nests headings by level', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: '# A\n\n## A.1\n\n# B\n' });
  const outline = await reader.getOutline();
  assert.deepEqual(outline.map(node => node.title), ['A', 'B']);
  assert.equal(outline[0].children.length, 1);
  assert.equal(outline[0].children[0].title, 'A.1');
});

test('an injected renderer output is still sanitized', async () => {
  const reader = createMarkdownReader({ renderer: () => '<h1>Hi</h1><script>bad()</script>' });
  await reader.open(REF, { text: 'ignored' });
  const html = reader.getRender();
  assert.ok(html.includes('<h1>Hi</h1>'));
  assert.ok(!html.includes('<script>'));
});

test('abort during load rejects the pending open and does not bind a document', async () => {
  let release;
  const reader = createMarkdownReader({ loadText: () => new Promise(resolve => { release = resolve; }) });
  const pending = reader.open(REF);
  pending.catch(() => {});
  reader.abort();
  release('late text');
  await assert.rejects(pending, error => isReaderError(error, READER_ERROR_CODES.ABORTED));
  assert.equal(reader.viewState().ready, false);
});

test('clear drops state so an old selection cannot leak into the next document', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'first' });
  reader.clear();
  assert.throws(() => reader.getText(), error => isReaderError(error, READER_ERROR_CODES.NOT_OPEN));
  await reader.open({ document_id: 'doc-2', revision_id: 'rev-2' }, { text: 'second' });
  assert.equal(reader.selectRange(0, 1, 'codepoint').document_id, 'doc-2');
});

test('the reader refuses use after close', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'abc' });
  reader.close();
  assert.throws(() => reader.getText(), error => isReaderError(error, READER_ERROR_CODES.CLOSED));
});

test('the default content loader uses the content endpoint with same-origin credentials', async () => {
  const calls = [];
  const reader = createMarkdownReader({
    environment: { async fetch(url, options) { calls.push({ url, options }); return { ok: true, async json() { return { text: 'hello' }; } }; } },
  });
  await reader.open(REF);
  assert.equal(calls[0].url, '/api/documents/doc-1/content');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(reader.getText(), 'hello');
});

test('setHighlight validates anchors and never rewrites stored rects', async () => {
  const reader = createMarkdownReader();
  await reader.open(REF, { text: 'abc' });
  const anchor = createReferenceDraft(REF, { selector: { type: 'text', start: 0, end: 1, unit: 'codepoint' }, quote: 'a' });
  assert.equal(reader.setHighlight([anchor]), 1);
  assert.deepEqual(reader.getHighlights()[0].selector, anchor.selector);
});

// -------------------------------------------------------------- pdf reader ----

test('a pdf reader cannot be created without an injected engine', () => {
  assert.throws(() => createPdfReader({}), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
  assert.throws(() => resolvePdfEngine({}), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
});

test('an unusable injected engine is rejected with a clear reason', () => {
  assert.throws(() => resolvePdfEngine({ engine: { notADocument: true } }), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
});

test('screen to canonical mapping honours the top-left origin', () => {
  const view = [0, 0, 100, 200];
  assert.deepEqual(screenPointToCanonical({ x: 0, y: 0 }, { view }), { x: 0, y: 0 });
  assert.deepEqual(screenPointToCanonical({ x: 100, y: 200 }, { view }), { x: 1, y: 1 });
});

test('a 90 degree page rotation is undone into the unrotated canonical frame', () => {
  // Rotating the sheet clockwise sends the unrotated top-left corner to the
  // rendered top-right; canonical storage always stays unrotated.
  assert.deepEqual(rotateCanonicalToScreen({ x: 0, y: 0 }, 1), { x: 1, y: 0 });
  const view = [0, 0, 100, 200];
  const canonical = screenPointToCanonical({ x: 0, y: 0 }, { view, rotate: 90 });
  assert.ok(Math.abs(canonical.x - 0) < 1e-9 && Math.abs(canonical.y - 1) < 1e-9);
  // Round trip through the display mapping is exact.
  const back = rotateScreenToCanonical(rotateCanonicalToScreen({ x: 0.3, y: 0.7 }, 1), 1);
  assert.ok(Math.abs(back.x - 0.3) < 1e-9 && Math.abs(back.y - 0.7) < 1e-9);
});

test('canonical to screen mapping round trips a rect', () => {
  const geometry = { view: [0, 0, 100, 200] };
  const rect = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
  const screen = canonicalRectToScreen(rect, geometry);
  const back = screenPointToCanonical({ x: screen.x, y: screen.y }, geometry);
  assert.ok(Math.abs(back.x - rect.x) < 1e-9);
  assert.ok(Math.abs(back.y - rect.y) < 1e-9);
});

function fakeEngine({ pages = 3, text = true, fail = null } = {}) {
  const destroyed = [];
  const document = {
    numPages: pages,
    async getPage(pageNumber) {
      return {
        rotate: 0,
        userUnit: 1,
        view: [0, 0, 100, 200],
        async getTextContent() {
          if (!text) return { items: [] };
          return { items: [{ str: `page ${pageNumber} alpha beta gamma` }] };
        },
      };
    },
    async getOutline() {
      return [{ id: 'c1', title: 'Chapter One', dest: [1, 0, 0], items: [] }];
    },
    async destroy() { destroyed.push(pageNumber0()); },
  };
  function pageNumber0() { return 0; }
  const engine = {
    getDocument(params) {
      if (fail) return Promise.reject(fail);
      return { promise: Promise.resolve(document), params, destroy: document.destroy };
    },
  };
  return { engine, document, destroyed };
}

test('a pdf reader opens through an injected engine and reports page count', async () => {
  const { engine } = fakeEngine();
  const reader = createPdfReader({ engine });
  const meta = await reader.open({ ...REF, artifact_id: 'a', build_id: 'b' }, { data: new Uint8Array([1]) });
  assert.equal(meta.num_pages, 3);
  assert.equal(meta.coordinate_space, PDF_COORDINATE_SPACE);
  assert.equal(meta.artifact_id, 'a');
  await reader.close();
});

test('pdf search is bounded by a page limit and reports truncation', async () => {
  const { engine } = fakeEngine({ pages: 10 });
  const reader = createPdfReader({ engine });
  await reader.open(REF, { data: new Uint8Array([1]) });
  const hits = await reader.search('beta', { maxPages: 3 });
  assert.equal(hits.truncated, true);
  assert.equal(hits.scanned_pages, 3);
  assert.ok(hits.every(hit => hit.page <= 3));
  await reader.close();
});

test('a page without a text layer reports no_text_layer and never fabricates offsets', async () => {
  const { engine } = fakeEngine({ text: false });
  const reader = createPdfReader({ engine });
  await reader.open(REF, { data: new Uint8Array([1]) });
  await assert.rejects(reader.getText(1), error => isReaderError(error, READER_ERROR_CODES.NO_TEXT_LAYER));
  await reader.close();
});

test('an encrypted pdf maps to pdf_encrypted without leaking a password', async () => {
  const engine = { getDocument: () => Promise.reject(Object.assign(new Error('Password required'), { name: 'PasswordException' })) };
  const reader = createPdfReader({ engine });
  await assert.rejects(reader.open(REF, { data: new Uint8Array([1]), password: 'secret' }), error => {
    assert.ok(isReaderError(error, READER_ERROR_CODES.ENCRYPTED));
    assert.ok(!String(error.message).includes('secret'));
    return true;
  });
});

test('a malformed pdf maps to pdf_malformed', async () => {
  const engine = { getDocument: () => Promise.reject(Object.assign(new Error('Invalid PDF structure'), { name: 'InvalidPDFException' })) };
  const reader = createPdfReader({ engine });
  await assert.rejects(reader.open(REF, { data: new Uint8Array([1]) }), error => isReaderError(error, READER_ERROR_CODES.MALFORMED));
});

test('selectPages produces a pdf draft with canonical rects and pages[]', async () => {
  const { engine } = fakeEngine();
  const reader = createPdfReader({ engine });
  await reader.open(REF, { data: new Uint8Array([1]) });
  const draft = reader.selectPages({
    pages: [{ page: 1, rects: [{ x: 0.1, y: 0.1, w: 0.2, h: 0.05 }] }],
    quote: 'alpha beta',
  });
  assert.equal(draft.selector.type, 'pdf');
  assert.equal(draft.selector.coordinate_space, PDF_COORDINATE_SPACE);
  assert.equal(draft.selector.pages.length, 1);
  await reader.close();
});

test('selectPages requires a pages[] array rather than a single page', async () => {
  const { engine } = fakeEngine();
  const reader = createPdfReader({ engine });
  await reader.open(REF, { data: new Uint8Array([1]) });
  assert.throws(() => reader.selectPages({ page: 1 }), error => isReaderError(error, READER_ERROR_CODES.INVALID_SELECTOR));
  await reader.close();
});

test('pdf navigation is clamped to the page range', async () => {
  const { engine } = fakeEngine({ pages: 2 });
  const reader = createPdfReader({ engine });
  await reader.open(REF, { data: new Uint8Array([1]) });
  await assert.rejects(reader.goTo({ page: 5 }), error => isReaderError(error, READER_ERROR_CODES.INVALID_ARGUMENT));
  assert.deepEqual(await reader.goTo({ page: 2 }), { page: 2 });
  await reader.close();
});

test('pdf outline destinations map to 1-based pages', async () => {
  const { engine } = fakeEngine();
  const reader = createPdfReader({ engine });
  await reader.open(REF, { data: new Uint8Array([1]) });
  const outline = await reader.getOutline();
  assert.equal(outline[0].title, 'Chapter One');
  assert.equal(outline[0].page, 2);
  await reader.close();
});

// -------------------------------------------------------------- tex reader ----

test('TeX compilation is unavailable and never falls back', async () => {
  await assert.rejects(compileProject(), error => isReaderError(error, READER_ERROR_CODES.TEX_COMPILE_UNAVAILABLE));
  const reader = createTexSourceReader();
  await reader.open(REF, { project: { main: 'main.tex', resources: { 'main.tex': '\\documentclass{article}' } } });
  await assert.rejects(reader.compile(), error => isReaderError(error, READER_ERROR_CODES.TEX_COMPILE_UNAVAILABLE));
  reader.close();
});

test('a TeX project without its main file is rejected', () => {
  assert.throws(
    () => normalizeTexProject({ main: 'main.tex', resources: { 'other.tex': 'x' } }),
    error => isReaderError(error, READER_ERROR_CODES.TEX_MAIN_INVALID),
  );
});

test('a TeX project resource with an absolute path is rejected', () => {
  assert.throws(
    () => normalizeTexProject({ main: 'main.tex', resources: { '/etc/passwd': 'x', 'main.tex': 'y' } }),
    error => isReaderError(error, READER_ERROR_CODES.TEX_PATH_INVALID),
  );
});

test('the TeX source reader exposes file metadata and both unit lengths', async () => {
  const reader = createTexSourceReader();
  await reader.open(REF, {
    project: { main: 'main.tex', resources: { 'main.tex': '\\input{sec}\n🎉', 'sec.tex': 'section' } },
  });
  const files = reader.getFiles();
  const main = files.find(file => file.path === 'main.tex');
  assert.equal(main.role, 'main');
  assert.equal(main.unit, 'codepoint');
  assert.equal(main.codepoint_length, [...'\\input{sec}\n🎉'].length);
  assert.equal(main.utf16_length, '\\input{sec}\n🎉'.length);
  assert.ok(main.utf16_length > main.codepoint_length, 'the emoji is two UTF-16 units but one codepoint');
  reader.close();
});

test('a tex-source selection always carries the mandatory file and codepoints', async () => {
  const reader = createTexSourceReader();
  await reader.open(REF, { project: { main: 'main.tex', resources: { 'main.tex': 'hello', 'a.tex': 'x🎉y' } } });
  const draft = reader.selectRange('a.tex', { start: 1, end: 3 }, 'utf16');
  assert.equal(draft.selector.type, 'tex-source');
  assert.equal(draft.selector.file, 'a.tex');
  assert.equal(draft.selector.unit, 'codepoint');
  assert.deepEqual({ start: draft.selector.start, end: draft.selector.end }, { start: 1, end: 2 });
  assert.equal(draft.quote, '🎉');
  reader.close();
});

test('selecting an unknown TeX file is rejected with tex_path_invalid', async () => {
  const reader = createTexSourceReader();
  await reader.open(REF, { project: { main: 'main.tex', resources: { 'main.tex': 'x' } } });
  assert.throws(() => reader.selectRange('nope.tex', { start: 0, end: 1 }, 'codepoint'), error => isReaderError(error, READER_ERROR_CODES.TEX_PATH_INVALID));
  reader.close();
});

test('the TeX reader refuses a tex-source anchor without a file', async () => {
  const reader = createTexSourceReader();
  await reader.open(REF, { project: { main: 'main.tex', resources: { 'main.tex': 'x' } } });
  const anchor = { ...REF, selector: { type: 'tex-source', start: 0, end: 1, unit: 'codepoint' }, quote: 'x' };
  assert.throws(() => reader.setHighlight([anchor]), error => isReaderError(error, READER_ERROR_CODES.TEX_SOURCE_FILE_REQUIRED));
  reader.close();
});

test('TeX source search covers the whole project unless a file is given', async () => {
  const reader = createTexSourceReader();
  await reader.open(REF, { project: { main: 'main.tex', resources: { 'main.tex': 'alpha', 'b.tex': 'alpha alpha' } } });
  const all = await reader.search('alpha');
  assert.equal(all.length, 3);
  const scoped = await reader.search('alpha', { file: 'b.tex' });
  assert.equal(scoped.length, 2);
  assert.ok(scoped.every(hit => hit.file === 'b.tex'));
  reader.close();
});

test('the compiled artifact reader reuses the PDF adapter and needs an engine', () => {
  assert.throws(() => createTexCompiledReader({}), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
});

test('a compiled artifact reference must carry artifact_id and build_id', async () => {
  const { engine } = fakeEngine();
  const reader = createTexCompiledReader({ engine });
  await assert.rejects(reader.openArtifact(REF, new Uint8Array([1])), error => isReaderError(error, READER_ERROR_CODES.INVALID_REFERENCE));
  const meta = await reader.openArtifact({ ...REF, artifact_id: 'a', build_id: 'b' }, new Uint8Array([1]));
  assert.equal(meta.revision_id, 'rev-1');
  assert.equal(meta.artifact_id, 'a');
  assert.equal(meta.nativeReader, undefined);
  await reader.close();
});

// ---------------------------------------------------------------- registry ----

test('format detection understands extensions, aliases and media types', () => {
  assert.equal(detectFormat('notes.md'), 'md');
  assert.equal(detectFormat('doc.PDF'), 'pdf');
  assert.equal(detectFormat('markdown'), 'md');
  assert.equal(detectFormat('application/pdf'), 'pdf');
  assert.equal(detectFormat({ filename: 'paper.txt' }), 'txt');
  assert.equal(detectFormat('archive.zip'), null);
  assert.equal(detectFormat(''), null);
});

test('capabilities report pdf as unavailable when no engine was injected', () => {
  const registry = createReaderRegistry({});
  const capabilities = registry.capabilities();
  assert.deepEqual(capabilities.formats, [...SUPPORTED_FORMATS]);
  assert.equal(capabilities.readers.pdf.available, false);
  assert.equal(capabilities.readers.pdf.reason, READER_ERROR_CODES.ENGINE_UNAVAILABLE);
  assert.equal(capabilities.tex_compile.available, false);
  assert.equal(capabilities.tex_compile.reason, READER_ERROR_CODES.TEX_COMPILE_UNAVAILABLE);
});

test('capabilities report pdf as available once an engine is injected', () => {
  const { engine } = fakeEngine();
  assert.equal(createReaderRegistry({ engine }).capabilities().readers.pdf.available, true);
});

test('the registry refuses formats with no adapter', () => {
  const registry = createReaderRegistry({});
  assert.throws(() => registry.create('docx'), error => isReaderError(error, READER_ERROR_CODES.UNSUPPORTED_FORMAT));
});

test('the registry opens markdown and tex through their adapters', async () => {
  const registry = createReaderRegistry({});
  const markdown = await registry.open('notes.md', REF, { text: '# Hi\n' });
  assert.equal(markdown.viewState().ready, true);
  markdown.close();

  const tex = await registry.open('paper.tex', REF, { project: { main: 'main.tex', resources: { 'main.tex': 'x' } } });
  assert.equal(tex.viewState().main, 'main.tex');
  tex.close();
});

test('opening a pdf through the registry fails clearly without an engine', async () => {
  const registry = createReaderRegistry({});
  await assert.rejects(registry.open('paper.pdf', REF, { data: new Uint8Array([1]) }), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
});

test('the registry compiles tex through the unavailable contract', async () => {
  const registry = createReaderRegistry({});
  await assert.rejects(registry.compileTex(), error => isReaderError(error, READER_ERROR_CODES.TEX_COMPILE_UNAVAILABLE));
});

test('the registry opens a compiled artifact with the injected engine', async () => {
  const { engine } = fakeEngine();
  const registry = createReaderRegistry({ engine });
  const reader = await registry.openCompiled({ ...REF, artifact_id: 'a', build_id: 'b' }, new Uint8Array([1]));
  assert.equal(reader.viewState().ready, true);
  await reader.close();
});

test('the markdown adapter produces a plain-text projection with a source map', () => {
  const model = buildMarkdownModel('## Head\n\nBody text\n');
  assert.ok(model.projection.includes('Head'));
  assert.ok(model.projection.includes('Body text'));
  assert.equal(model.map.length, [...model.projection].length);
  assert.ok(model.html.includes('<h2>'));
});