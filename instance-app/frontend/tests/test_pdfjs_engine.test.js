/**
 * PDF.js engine bridge tests.
 *
 * These are *real* engine tests: they import the locally bundled `pdfjs-dist`
 * (no network, no CDN) and drive the actual `getDocument` / `getPage` /
 * `getTextContent` / `render` / `destroy` code paths through the bridge and the
 * unchanged `pdf-reader.js` adapter. The PDF bytes are built inline, so no binary
 * fixture is committed.
 *
 * Environment note: this repository runs the tests under Node, where the dedicated
 * `pdfjs-dist/legacy/build/pdf.mjs` build and a local `file:`-URL worker are used.
 * The browser build is not exercised headlessly here; the *API contract* the bridge
 * maps (getDocument -> document/pages/renderTask/cancel/destroy) is the same.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

import {
  PDFJS_BUNDLED_FILES,
  assertSelfHostedAssetUrl,
  createPDFJSEngine,
  loadPDFJSEngine,
  pdfjsAssetUrls,
} from '../assets/pdfjs-engine.js';
import { READER_ERROR_CODES, isReaderError } from '../assets/reader-errors.js';
import { createPdfReader } from '../assets/pdf-reader.js';
import { createReaderRegistry } from '../assets/reader-registry.js';

// The bundled engine and its self-hosted worker, both resolved locally.
const pdfjsLib = await import('pdfjs-dist/legacy/build/pdf.mjs');
const WORKER_SRC = import.meta.resolve('pdfjs-dist/legacy/build/pdf.worker.min.mjs');

// Optionally present (pdfjs-dist declares it as an optional dependency); used only
// to exercise the real render path. When absent, render is reported as unverified.
let canvasModule = null;
try {
  canvasModule = await import('@napi-rs/canvas');
} catch {
  canvasModule = null;
}

const REF = { document_id: 'doc-pdf', revision_id: 'rev-pdf' };

/** Build a minimal single-page PDF with a real text layer (Helvetica + text). */
function buildTextPdf(text) {
  const objects = {};
  objects[1] = '<< /Type /Catalog /Pages 2 0 R >>';
  objects[2] = '<< /Type /Pages /Kids [3 0 R] /Count 1 >>';
  objects[3] = '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>';
  objects[4] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>';
  const content = `BT /F1 18 Tf 30 200 Td (${text}) Tj ET`;
  objects[5] = `<< /Length ${Buffer.byteLength(content, 'latin1')} >>\nstream\n${content}\nendstream`;
  let pdf = '%PDF-1.4\n';
  const offsets = {};
  for (let i = 1; i <= 5; i += 1) {
    offsets[i] = Buffer.byteLength(pdf, 'latin1');
    pdf += `${i} 0 obj\n${objects[i]}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf, 'latin1');
  pdf += 'xref\n0 6\n0000000000 65535 f \n';
  for (let i = 1; i <= 5; i += 1) pdf += `${String(offsets[i]).padStart(10, '0')} 00000 n \n`;
  pdf += `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return new Uint8Array(Buffer.from(pdf, 'latin1'));
}

// Standard RC4 (PDF 1.4 /R 2) primitives, enough to hand-build an encrypted PDF
// fixture inline with no dependencies.
const PAD = Buffer.from([
  0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
  0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80, 0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
]);
const md5 = buffer => createHash('md5').update(buffer).digest();
function padPassword(password) {
  const raw = Buffer.from(password, 'latin1').subarray(0, 32);
  const out = Buffer.alloc(32);
  raw.copy(out);
  PAD.copy(out, raw.length);
  return out;
}
function rc4(key, data) {
  const s = [...Array(256).keys()];
  let j = 0;
  for (let i = 0; i < 256; i += 1) { j = (j + s[i] + key[i % key.length]) & 0xff; [s[i], s[j]] = [s[j], s[i]]; }
  const out = Buffer.alloc(data.length);
  let i = 0; j = 0;
  for (let k = 0; k < data.length; k += 1) {
    i = (i + 1) & 0xff; j = (j + s[i]) & 0xff; [s[i], s[j]] = [s[j], s[i]];
    out[k] = data[k] ^ s[(s[i] + s[j]) & 0xff];
  }
  return out;
}
/** Build a real encrypted single-page PDF (RC4 /R2, empty owner password). */
function buildEncryptedPdf(text, password, fileId) {
  const o = rc4(md5(padPassword(password)).subarray(0, 5), padPassword(password));
  const p = Buffer.alloc(4); p.writeInt32LE(-1, 0);
  const key = md5(Buffer.concat([padPassword(password), o, p, fileId])).subarray(0, 5);
  const u = rc4(key, PAD);
  const content = `BT /F1 18 Tf 30 200 Td (${text}) Tj ET`;
  const encContent = rc4(md5(Buffer.concat([key, Buffer.from([5, 0, 0, 0, 0])])).subarray(0, 10), Buffer.from(content, 'latin1'));
  const objects = {
    1: '<< /Type /Catalog /Pages 2 0 R >>',
    2: '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    3: '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    4: '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  };
  let head = '%PDF-1.4\n';
  const offsets = {};
  for (const n of [1, 2, 3, 4]) { offsets[n] = Buffer.byteLength(head, 'latin1'); head += `${n} 0 obj\n${objects[n]}\nendobj\n`; }
  offsets[5] = Buffer.byteLength(head, 'latin1');
  let body = Buffer.concat([
    Buffer.from(head, 'latin1'),
    Buffer.from(`5 0 obj\n<< /Length ${encContent.length} >>\nstream\n`, 'latin1'),
    encContent,
    Buffer.from('\nendstream\nendobj\n', 'latin1'),
  ]);
  offsets[6] = body.length;
  body = Buffer.concat([
    body,
    Buffer.from(`6 0 obj\n<< /Filter /Standard /V 1 /R 2 /O <${o.toString('hex')}> /U <${u.toString('hex')}> /P -1 >>\nendobj\n`, 'latin1'),
  ]);
  const xref = body.length;
  let tail = 'xref\n0 7\n0000000000 65535 f \n';
  for (let i = 1; i <= 6; i += 1) tail += `${String(offsets[i]).padStart(10, '0')} 00000 n \n`;
  tail += `trailer\n<< /Size 7 /Root 1 0 R /Encrypt 6 0 R /ID [<${fileId.toString('hex')}><${fileId.toString('hex')}>] >>\nstartxref\n${xref}\n%%EOF\n`;
  return new Uint8Array(Buffer.concat([body, Buffer.from(tail, 'latin1')]));
}

function engine(options = {}) {
  return createPDFJSEngine({ pdfjsLib, workerSrc: WORKER_SRC, ...options });
}

// ------------------------------------------------------------- asset URLs ----

test('asset URLs are built from one self-hosted local base', () => {
  const urls = pdfjsAssetUrls('/static/pdfjs/');
  assert.equal(urls.library, `/static/pdfjs/${PDFJS_BUNDLED_FILES.moduleMin}`);
  assert.equal(urls.workerSrc, `/static/pdfjs/${PDFJS_BUNDLED_FILES.worker}`);
  assert.ok(urls.cMapUrl.endsWith('/cmaps/'));
  assert.ok(urls.standardFontDataUrl.endsWith('/standard_fonts/'));
  // A base without a trailing slash is normalized, not concatenated naively.
  assert.equal(pdfjsAssetUrls('/static/pdfjs').workerSrc, '/static/pdfjs/pdf.worker.min.mjs');
});

test('remote/CDN and inline asset URLs are rejected', () => {
  assert.throws(() => assertSelfHostedAssetUrl('https://cdn.example/pdf.worker.mjs'), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
  assert.throws(() => assertSelfHostedAssetUrl('//cdn.example/pdf.worker.mjs'), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
  assert.throws(() => assertSelfHostedAssetUrl('data:text/javascript,1'), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
  // A same-origin path is accepted.
  assert.equal(assertSelfHostedAssetUrl('/static/pdfjs/pdf.worker.mjs'), '/static/pdfjs/pdf.worker.mjs');
});

test('a remote worker URL is refused before anything loads', () => {
  assert.throws(() => engine({ workerSrc: 'https://cdn.example/pdf.worker.mjs' }), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
});

test('loadPDFJSEngine refuses a remote library URL without importing it', async () => {
  let imported = false;
  await assert.rejects(
    loadPDFJSEngine({ base: '/static/pdfjs/', libraryUrl: 'https://cdn.example/pdf.mjs', importModule: async () => { imported = true; return pdfjsLib; } }),
    error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE),
  );
  assert.equal(imported, false);
});

// ------------------------------------------------------------ engine shape ----

test('createPDFJSEngine needs a module exposing getDocument()', () => {
  assert.throws(() => createPDFJSEngine({}), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
  assert.throws(() => createPDFJSEngine({ pdfjsLib: { notADocument: true } }), error => isReaderError(error, READER_ERROR_CODES.ENGINE_UNAVAILABLE));
});

test('the engine points PDF.js at the self-hosted worker', () => {
  const built = engine();
  assert.equal(built.workerSrc, WORKER_SRC);
  assert.equal(pdfjsLib.GlobalWorkerOptions.workerSrc, WORKER_SRC);
  assert.equal(built.version, pdfjsLib.version);
});

test('loadPDFJSEngine builds an engine from a local importModule', async () => {
  const built = await loadPDFJSEngine({ base: '/static/pdfjs/', importModule: async () => pdfjsLib });
  assert.equal(built.version, pdfjsLib.version);
  // The injected module never routes through a real dynamic import of a remote path.
  assert.equal(built.workerSrc, '/static/pdfjs/pdf.worker.min.mjs');
});

// ---------------------------------------------------------- real extraction ----

test('a real text PDF opens through the bridge and extracts page text', async () => {
  const reader = createPdfReader({ engine: engine() });
  const meta = await reader.open(REF, { data: buildTextPdf('Bridge extraction works') });
  assert.equal(meta.num_pages, 1);
  assert.equal(meta.coordinate_space, 'normalized-cropbox-unrotated-top-left');
  assert.equal(meta.document_id, 'doc-pdf');

  const text = await reader.getText(1);
  assert.equal(text.page, 1);
  assert.match(text.text, /Bridge extraction works/);

  const hits = await reader.search('extraction');
  assert.equal(hits.length, 1);
  assert.equal(hits[0].page, 1);
  assert.equal(hits[0].quote, 'extraction');
  await reader.close();
});

test('page geometry maps through the bridge (view/rotate/userUnit/getViewport)', async () => {
  const built = engine();
  const task = built.getDocument({ data: buildTextPdf('geometry') });
  const document = await task.promise;
  const page = await document.getPage(1);
  assert.deepEqual(page.view, [0, 0, 300, 300]);
  assert.equal(page.rotate, 0);
  assert.equal(page.userUnit, 1);
  const viewport = page.getViewport({ scale: 2, rotation: 0 });
  assert.equal(viewport.width, 600);
  assert.equal(viewport.height, 600);
  await document.destroy();
});

test('document.destroy() releases resources and is idempotent', async () => {
  const task = engine().getDocument({ data: buildTextPdf('destroy') });
  const document = await task.promise;
  assert.equal(document.destroyed, false);
  await document.destroy();
  assert.equal(document.destroyed, true);
  await document.destroy(); // second call is a no-op, never throws
  assert.equal(document.destroyed, true);
});

// ------------------------------------------------------------- url / token ----

test('a url is fetched with headers and never as a query token', async () => {
  const bytes = buildTextPdf('Token stays out of the URL');
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url, headers: init.headers });
    return { ok: true, status: 200, arrayBuffer: async () => bytes.buffer.slice(0) };
  };
  const built = engine({ headers: { Authorization: 'Bearer secret-token' }, fetchImpl });

  const reader = createPdfReader({ engine: built });
  const meta = await reader.open(REF, { url: '/api/documents/x/source' });
  assert.equal(meta.num_pages, 1);
  const text = await reader.getText(1);
  assert.match(text.text, /Token stays out of the URL/);
  await reader.close();

  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, '/api/documents/x/source');
  assert.equal(seen[0].url.includes('secret-token'), false);
  assert.equal(seen[0].headers.Authorization, 'Bearer secret-token');
});

// --------------------------------------------------------- failure mapping ----

test('malformed bytes map to pdf_malformed through the bridge', async () => {
  const reader = createPdfReader({ engine: engine() });
  await assert.rejects(
    reader.open(REF, { data: new Uint8Array(Buffer.from('not a pdf at all, just junk')) }),
    error => isReaderError(error, READER_ERROR_CODES.MALFORMED),
  );
  await reader.close();
});

test('a real encrypted PDF needs a password and maps to pdf_encrypted', async () => {
  const bytes = buildEncryptedPdf('Encrypted real text', 'secret', Buffer.from('0123456789abcdef'));
  const reader = createPdfReader({ engine: engine() });
  // No password: the real PDF.js PasswordException maps to the reader taxonomy and
  // no password value ever reaches the error.
  await assert.rejects(reader.open(REF, { data: new Uint8Array(bytes) }), error => {
    assert.ok(isReaderError(error, READER_ERROR_CODES.ENCRYPTED));
    assert.equal(String(error.message).includes('secret'), false);
    return true;
  });
  await reader.close();
});

test('a real encrypted PDF opens with the correct password', async () => {
  const bytes = buildEncryptedPdf('Encrypted real text', 'secret', Buffer.from('0123456789abcdef'));
  const reader = createPdfReader({ engine: engine() });
  await reader.open(REF, { data: new Uint8Array(bytes), password: 'secret' });
  const text = await reader.getText(1);
  assert.match(text.text, /Encrypted real text/);
  await reader.close();
});

// ------------------------------------------------------------------- render ----

test('render returns a cancellable render task', { skip: canvasModule ? false : 'no canvas backend in this Node environment (render unverified)' }, async () => {
  const built = engine();
  const task = built.getDocument({ data: buildTextPdf('render me') });
  const document = await task.promise;
  const page = await document.getPage(1);
  const viewport = page.getViewport({ scale: 1 });
  const canvas = canvasModule.createCanvas(300, 300);
  const renderTask = page.render({ canvasContext: canvas.getContext('2d'), viewport, canvas });
  assert.equal(typeof renderTask.promise.then, 'function');
  assert.equal(typeof renderTask.cancel, 'function');
  await renderTask.promise;
  await document.destroy();
});

// ------------------------------------------------------------- capabilities ----

test('the registry reports pdf available once the real engine is injected', () => {
  const capabilities = createReaderRegistry({ engine: engine() }).capabilities();
  assert.equal(capabilities.readers.pdf.available, true);
  assert.equal(capabilities.readers.pdf.reason, undefined);
});
