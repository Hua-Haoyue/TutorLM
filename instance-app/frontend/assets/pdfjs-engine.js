/**
 * PDF.js engine bridge (browser-side ES module).
 *
 * The reader adapter (`pdf-reader.js`) never ships or fetches an engine: the host
 * must inject one via `createPdfReader({ engine })`, otherwise the adapter fails
 * fast with `pdf_engine_unavailable`. This module is the *real* engine that can be
 * injected — it wraps a locally bundled `pdfjs-dist` build and maps the official
 * PDF.js objects onto exactly the surface `pdf-reader.js` documents:
 *
 *   engine.getDocument({ data | url, password, ...options }) -> loading task
 *   task.promise                  -> document (readable, see below)
 *   task.destroy()                -> abort loading *and* release resources
 *   document.numPages, document.getPage(n), document.getOutline(), document.destroy()
 *   page.rotate, page.userUnit, page.view, page.getViewport({ scale, rotation })
 *   page.getTextContent(), page.render(...) -> renderTask.promise / renderTask.cancel()
 *
 * Two PDF.js facts this bridge normalizes (verified against pdfjs-dist 6.4.299):
 *
 *   1. `PDFDocumentProxy` no longer exposes `destroy()`; destruction lives on the
 *      *loading task*. The reader calls `document.destroy()`, so the wrapper adds a
 *      `destroy()` that delegates to `task.destroy()`.
 *   2. The reader re-awaits `loaded.promise`, so `promise` must resolve to the
 *      wrapper (not the raw proxy) for `destroy()` to exist on the open document.
 *
 * Loading is always byte-first: when a `url` is passed, the bridge fetches the
 * bytes with the configured `headers` (e.g. `Authorization`) and hands PDF.js
 * `data`, so an access token is *never* placed in a URL. No remote script/CDN is
 * ever loaded: the worker and the font/CMap/wasm assets must be self-hosted local
 * paths, and remote absolute URLs are rejected.
 *
 * This module contains **no** PDF.js source of its own and reads no globals at
 * import time; it stays inert until `createPDFJSEngine` is called. It deliberately
 * does not claim to be a security sandbox: it disables XFA and never wires the
 * annotation scripting layer, and that is all.
 */

import { READER_ERROR_CODES, readerError, toReaderError } from './reader-errors.js';

/** File names of the locally bundled `pdfjs-dist` build (self-hosted under base). */
export const PDFJS_BUNDLED_FILES = Object.freeze({
  module: 'pdf.mjs',
  moduleMin: 'pdf.min.mjs',
  worker: 'pdf.worker.min.mjs',
  workerDebug: 'pdf.worker.mjs',
  cmaps: 'cmaps/',
  standardFonts: 'standard_fonts/',
  wasm: 'wasm/',
  iccs: 'iccs/',
});

/**
 * Build the self-hosted asset URLs PDF.js needs from one local base path.
 * `base` is a same-origin directory, e.g. `/static/pdfjs/` (built from the
 * bundled `node_modules/pdfjs-dist` tree). Trailing slash is preserved.
 */
export function pdfjsAssetUrls(base = '/static/pdfjs/') {
  const root = base.endsWith('/') ? base : `${base}/`;
  return {
    library: `${root}${PDFJS_BUNDLED_FILES.moduleMin}`,
    workerSrc: `${root}${PDFJS_BUNDLED_FILES.worker}`,
    cMapUrl: `${root}${PDFJS_BUNDLED_FILES.cmaps}`,
    standardFontDataUrl: `${root}${PDFJS_BUNDLED_FILES.standardFonts}`,
    wasmUrl: `${root}${PDFJS_BUNDLED_FILES.wasm}`,
    iccUrl: `${root}${PDFJS_BUNDLED_FILES.iccs}`,
  };
}

const REMOTE_URL = /^(?:https?:)?\/\//i;
const FORBIDDEN_SCHEME = /^(?:data|javascript|vbscript):/i;

/**
 * Assert an asset URL is a self-hosted local path, never a remote CDN or an
 * inline scheme. Returns the URL unchanged, or throws a reader error.
 */
export function assertSelfHostedAssetUrl(value, { role = 'asset' } = {}) {
  if (typeof value !== 'string' || value.length === 0) {
    throw readerError(READER_ERROR_CODES.ENGINE_UNAVAILABLE, `A local ${role} URL is required`);
  }
  if (REMOTE_URL.test(value)) {
    throw readerError(
      READER_ERROR_CODES.ENGINE_UNAVAILABLE,
      `The ${role} must be self-hosted: remote/CDN URLs are not allowed (${value})`,
      { role, url: value },
    );
  }
  if (FORBIDDEN_SCHEME.test(value)) {
    throw readerError(READER_ERROR_CODES.ENGINE_UNAVAILABLE, `The ${role} must be a bundled file, not a ${value.split(':', 1)[0]}: URL`, { role });
  }
  return value;
}

/** Map a PDF.js failure onto the reader error taxonomy (same codes as the adapter). */
function mapEngineError(error) {
  const name = error && error.name;
  const message = (error && error.message) || '';
  if (name === 'PasswordException' || /password/i.test(message)) {
    return toReaderError(error, READER_ERROR_CODES.ENCRYPTED, 'The PDF is encrypted; no password is stored or logged');
  }
  if (name === 'InvalidPDFException' || /invalid pdf|malformed/i.test(message)) {
    return toReaderError(error, READER_ERROR_CODES.MALFORMED, 'The PDF is malformed');
  }
  return toReaderError(error, READER_ERROR_CODES.LOAD_FAILED, 'The PDF could not be loaded');
}

/** Wrap a PDF.js `RenderTask` so `promise`/`cancel`/`onContinue` are all present. */
function wrapRenderTask(renderTask) {
  return {
    promise: renderTask.promise,
    cancel() {
      return renderTask.cancel();
    },
    get onContinue() {
      return renderTask.onContinue;
    },
    set onContinue(handler) {
      renderTask.onContinue = handler;
    },
  };
}

/** Wrap a `PDFPageProxy`: keep the official fields, add nothing the reader lacks. */
function wrapPage(page) {
  return {
    get pageNumber() { return page.pageNumber; },
    get rotate() { return page.rotate; },
    get userUnit() { return page.userUnit; },
    get view() { return page.view; },
    get ref() { return page.ref; },
    getViewport(params) { return page.getViewport(params); },
    getTextContent(params) { return page.getTextContent(params); },
    getAnnotations(params) { return page.getAnnotations(params); },
    render(params) { return wrapRenderTask(page.render(params)); },
    cleanup(resetStats) { return page.cleanup(resetStats); },
    get isPureXfa() { return page.isPureXfa; },
  };
}

/**
 * Wrap a `PDFDocumentProxy`. `destroy` delegates to the loading task because the
 * proxy in PDF.js 6.x no longer owns destruction; every other method is delegated
 * to the real document so behavior stays identical to upstream PDF.js.
 */
function wrapDocument(document, { destroy }) {
  let destroyed = false;
  return {
    get numPages() { return document.numPages; },
    get fingerprints() { return document.fingerprints; },
    get isPureXfa() { return document.isPureXfa; },
    /** True once the loading task was destroyed (mirrors the wrapper flag). */
    get destroyed() { return destroyed || document.destroyed === true; },
    getPage(pageNumber) { return document.getPage(pageNumber).then(wrapPage); },
    getOutline() { return document.getOutline(); },
    getPageLabels() { return document.getPageLabels(); },
    getMetadata() { return document.getMetadata(); },
    getAnnotations() { return document.getAnnotations(); },
    getData() { return document.getData(); },
    async destroy() {
      if (destroyed) return;
      destroyed = true;
      await destroy();
    },
  };
}

/**
 * Create a PDF.js engine usable as `createPdfReader({ engine })`.
 *
 * options.pdfjsLib         — an already-imported local `pdfjs-dist` module (required).
 * options.base             — local asset base; defaults to `/static/pdfjs/`.
 * options.workerSrc        — self-hosted worker URL; defaults to `pdfjsAssetUrls(base).workerSrc`.
 * options.assetUrls        — override `cMapUrl`/`standardFontDataUrl`/`wasmUrl`/`iccUrl`.
 * options.fetchImpl        — fetch used to load `url` bytes; defaults to the injected
 *                            environment's fetch, then `globalThis.fetch`.
 * options.environment      — injected global (defaults to `globalThis`).
 * options.headers          — headers (e.g. `Authorization`) sent when fetching bytes.
 * options.documentOptions  — extra `getDocument` parameters merged last.
 *
 * Security posture: `enableXfa: false` is forced, byte loading keeps tokens out of
 * URLs, and no scripting/annotation-JS layer is wired. This is not a sandbox.
 */
export function createPDFJSEngine(options = {}) {
  const pdfjsLib = options.pdfjsLib;
  if (!pdfjsLib || typeof pdfjsLib.getDocument !== 'function') {
    throw readerError(
      READER_ERROR_CODES.ENGINE_UNAVAILABLE,
      'createPDFJSEngine needs a local pdfjs-dist module exposing getDocument()',
    );
  }

  const environment = options.environment ?? globalThis;
  const base = options.base ?? '/static/pdfjs/';
  const defaults = pdfjsAssetUrls(base);
  const workerSrc = assertSelfHostedAssetUrl(options.workerSrc ?? defaults.workerSrc, { role: 'worker' });
  const fetchImpl = options.fetchImpl ?? environment.fetch ?? (typeof fetch === 'function' ? fetch : null);

  // Point the bundled worker at the self-hosted file (no CDN, no blob fallback).
  if (pdfjsLib.GlobalWorkerOptions) {
    pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;
  }

  const assetOptions = {
    cMapUrl: assertSelfHostedAssetUrl(options.assetUrls?.cMapUrl ?? defaults.cMapUrl, { role: 'cMapUrl' }),
    cMapPacked: true,
    standardFontDataUrl: assertSelfHostedAssetUrl(options.assetUrls?.standardFontDataUrl ?? defaults.standardFontDataUrl, { role: 'standardFontDataUrl' }),
    wasmUrl: assertSelfHostedAssetUrl(options.assetUrls?.wasmUrl ?? defaults.wasmUrl, { role: 'wasmUrl' }),
    iccUrl: assertSelfHostedAssetUrl(options.assetUrls?.iccUrl ?? defaults.iccUrl, { role: 'iccUrl' }),
  };

  /**
   * Resolve `{ data } | { url } | { range }` into PDF.js parameters.
   * A `url` is always turned into bytes via `fetchImpl` so the access token travels
   * in a header, never in the query string or path.
   */
  async function resolveSource(params = {}) {
    if (params.data !== undefined && params.data !== null) return { data: params.data };
    if (params.url === undefined || params.url === null) {
      throw readerError(READER_ERROR_CODES.LOAD_FAILED, 'getDocument needs PDF data or a url');
    }
    if (typeof fetchImpl !== 'function') {
      throw readerError(
        READER_ERROR_CODES.ENGINE_UNAVAILABLE,
        'Loading by url needs a fetch implementation so the token stays out of the URL; pass data instead',
      );
    }
    const response = await fetchImpl(params.url, {
      headers: { ...(options.headers || {}), ...(params.headers || {}) },
      credentials: params.credentials ?? 'same-origin',
      signal: params.signal,
    });
    if (response && response.ok === false) {
      throw readerError(READER_ERROR_CODES.LOAD_FAILED, `Fetching the PDF failed with status ${response.status}`, { status: response.status });
    }
    const buffer = await response.arrayBuffer();
    return { data: new Uint8Array(buffer) };
  }

  return {
    /** Official PDF.js version of the injected build, for diagnostics. */
    get version() {
      return pdfjsLib.version ?? null;
    },
    /** The self-hosted worker URL actually used. */
    workerSrc,
    assetUrls: Object.freeze({ ...assetOptions }),

    /**
     * Mirror of `pdfjsLib.getDocument`, but byte-first and with a document that
     * exposes `destroy()`. Returns a loading-task-shaped object; `promise` resolves
     * to the wrapped document the reader expects.
     */
    getDocument(params = {}) {
      const { headers, credentials, signal, range, ...passthrough } = params;
      const load = (async () => {
        const source = range !== undefined ? { range } : await resolveSource({ data: passthrough.data, url: passthrough.url, headers, credentials, signal });
        delete passthrough.data;
        delete passthrough.url;
        const task = pdfjsLib.getDocument({
          ...assetOptions,
          ...passthrough,
          ...(options.documentOptions || {}),
          // Security posture is enforced last: XFA rendering stays off and no
          // annotation-JS/scripting layer is ever requested, regardless of caller
          // options. This is not a sandbox, only a hard default.
          enableXfa: false,
          enableScripting: false,
          disableAutoFetch: true,
          ...source,
        });
        return task;
      })();

      const taskPromise = load.catch(error => { throw mapEngineError(error); });
      return {
        promise: taskPromise.then(task => task.promise).then(
          document => wrapDocument(document, { destroy: () => taskPromise.then(task => task.destroy()) }),
          error => { throw mapEngineError(error); },
        ),
        async destroy() {
          try {
            const task = await taskPromise;
            await task.destroy();
          } catch {
            // Destroying a failed/superseded load is best effort.
          }
        },
      };
    },
  };
}

/**
 * Dynamically import a self-hosted `pdfjs-dist` bundle, then build the engine.
 * Use this when the host has not already imported the local module. `options.importModule`
 * may be injected (tests) to avoid a real dynamic import.
 */
export async function loadPDFJSEngine(options = {}) {
  const base = options.base ?? '/static/pdfjs/';
  const libraryUrl = assertSelfHostedAssetUrl(options.libraryUrl ?? pdfjsAssetUrls(base).library, { role: 'library' });
  const importModule = options.importModule ?? (specifier => import(/* @vite-ignore */ specifier));
  const pdfjsLib = await importModule(libraryUrl);
  return createPDFJSEngine({ ...options, base, pdfjsLib });
}

export { READER_ERROR_CODES };
