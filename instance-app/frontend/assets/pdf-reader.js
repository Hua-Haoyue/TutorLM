/**
 * PDF reader adapter (browser-side ES module, no dependencies).
 *
 * The adapter never ships or fetches a PDF engine. A PDF engine must be injected
 * (`createPdfReader({ engine })`), because docs/document-readers.md §4.6 requires a
 * pinned, self-hosted `pdfjs-dist` build and this repository installs no packages.
 * With no engine the adapter fails fast with `pdf_engine_unavailable` instead of
 * pretending to render, silently falling back, or loading remote script.
 *
 * Engine surface expected by this adapter (documented, not verified here):
 *
 *   engine.openDocument({ data | url, options }) -> document
 *   document.numPages: number
 *   document.getPage(pageNumber)   // 1-based, per PDF.js
 *   document.getOutline()          // optional
 *   document.destroy()
 *   page.rotate, page.userUnit, page.view, page.getViewport({ scale, rotation })
 *   page.getTextContent()          // optional; absent text layer is explicit
 *   page.render(...) / renderTask.cancel()   // optional; owned by the mounting UI
 *
 * The canonical PDF selector is exactly one coordinate space
 * (`normalized-cropbox-unrotated-top-left`): normalized, unrotated CropBox,
 * origin top-left, x right, y down, values 0..1. `rotation` and
 * `cropbox_transform` are carried for display mapping only and never rewrite
 * stored rects. A page without a text layer reports `no_text_layer`; no offset is
 * fabricated and a chosen excerpt becomes an explicit whole-document draft.
 */

import { createEmitter } from './reader-events.js';
import { READER_ERROR_CODES, readerError, toReaderError } from './reader-errors.js';
import {
  PDF_COORDINATE_SPACE,
  createReferenceDraft,
  makeDocumentRevisionRef,
  normalizeRect,
  validateReferenceDraft,
} from './reference-draft.js';

const EXTRACTOR_VERSION = 'frontend-pdf-1';
const QUOTE_CONTEXT_LENGTH = 32;
const DEFAULT_MAX_PAGES = 50;
const DEFAULT_MAX_RESULTS = 200;

/** Accept the engine in any of the shapes a host bundle tends to expose. */
export function resolvePdfEngine(options = {}) {
  const candidate = options.engine ?? options.pdfjsLib ?? options.pdfjs;
  if (!candidate) {
    throw readerError(
      READER_ERROR_CODES.ENGINE_UNAVAILABLE,
      'A PDF engine must be injected: this repository installs no pdfjs-dist build and never loads remote script',
    );
  }
  const loader = typeof candidate.getDocument === 'function'
    ? candidate
    : candidate.default && typeof candidate.default.getDocument === 'function'
      ? candidate.default
      : null;
  if (!loader) {
    throw readerError(READER_ERROR_CODES.ENGINE_UNAVAILABLE, 'The injected PDF engine exposes no getDocument()', { engine: Boolean(candidate) });
  }
  return loader;
}

/** Map engine-specific failures onto the reader error taxonomy. */
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

/**
 * Whole quarter turns of clockwise page rotation, normalized to 0..3.
 * `rotate` is the official PDF.js `PDFPageProxy.rotate` value.
 */
function quarterTurns(rotate) {
  return ((Math.round((Number(rotate) || 0) / 90) % 4) + 4) % 4;
}

/**
 * Canonical fraction -> rotated (screen) fraction for `quarters` clockwise turns.
 * Canonical space is the unrotated CropBox; rotation lives only in this mapping.
 */
export function rotateCanonicalToScreen(point, quarters) {
  const { x, y } = point;
  switch (quarters) {
    case 1: return { x: 1 - y, y: x };
    case 2: return { x: 1 - x, y: 1 - y };
    case 3: return { x: y, y: 1 - x };
    default: return { x, y };
  }
}

/** Rotated (screen) fraction -> canonical fraction; exact inverse of the above. */
export function rotateScreenToCanonical(point, quarters) {
  const { x, y } = point;
  switch (quarters) {
    case 1: return { x: y, y: 1 - x };
    case 2: return { x: 1 - x, y: 1 - y };
    case 3: return { x: 1 - y, y: x };
    default: return { x, y };
  }
}

/**
 * Screen pixel point -> canonical normalized CropBox coordinates.
 * Inputs are the official PDF.js quantities (`view`, `rotate`, `userUnit`); this is
 * used only to build a rect from a user gesture. It never rewrites stored rects.
 */
export function screenPointToCanonical(point, { view, rotate = 0, userUnit = 1 } = {}) {
  if (!point || typeof point.x !== 'number' || typeof point.y !== 'number') {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'A point needs numeric x/y');
  }
  if (!Array.isArray(view) || view.length !== 4) {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'view must be [x1,y1,x2,y2]');
  }
  const [x1, y1, x2, y2] = view;
  const width = Math.abs(x2 - x1) || 1;
  const height = Math.abs(y2 - y1) || 1;
  const raw = {
    x: (point.x / userUnit - Math.min(x1, x2)) / width,
    y: (point.y / userUnit - Math.min(y1, y2)) / height,
  };
  const canonical = rotateScreenToCanonical(raw, quarterTurns(rotate));
  return { x: clamp01(canonical.x), y: clamp01(canonical.y) };
}

/**
 * Canonical rect -> screen pixel bounding box for the current rotation.
 * Display mapping only: the stored canonical rect is returned untouched elsewhere.
 */
export function canonicalRectToScreen(rect, { view, rotate = 0, userUnit = 1 } = {}) {
  const normalized = normalizeRect(rect);
  if (!Array.isArray(view) || view.length !== 4) {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'view must be [x1,y1,x2,y2]');
  }
  const [x1, y1, x2, y2] = view;
  const width = Math.abs(x2 - x1) || 1;
  const height = Math.abs(y2 - y1) || 1;
  const quarters = quarterTurns(rotate);
  const corners = [
    { x: normalized.x, y: normalized.y },
    { x: normalized.x + normalized.w, y: normalized.y },
    { x: normalized.x + normalized.w, y: normalized.y + normalized.h },
    { x: normalized.x, y: normalized.y + normalized.h },
  ];
  const screen = corners.map(corner => {
    const raw = rotateCanonicalToScreen(corner, quarters);
    return { x: (raw.x * width + Math.min(x1, x2)) * userUnit, y: (raw.y * height + Math.min(y1, y2)) * userUnit };
  });
  const xs = screen.map(entry => entry.x);
  const ys = screen.map(entry => entry.y);
  return {
    x: Math.min(...xs),
    y: Math.min(...ys),
    w: Math.abs(Math.max(...xs) - Math.min(...xs)),
    h: Math.abs(Math.max(...ys) - Math.min(...ys)),
  };
}
function clamp01(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

function normalizePageAnchor(pages, page, rects, quads) {
  pages.push({ page, rects, ...(quads === undefined ? {} : { quads }) });
}

/**
 * Create the PDF reader adapter. `engine` is required unless an engine is already
 * registered through `options.pdfjsLib`/`options.pdfjs`; otherwise construction
 * throws `pdf_engine_unavailable`.
 */
export function createPdfReader(options = {}) {
  const loader = resolvePdfEngine(options);
  const environment = options.environment ?? globalThis;
  const emitter = createEmitter(['selection', 'reference', 'viewchange', 'error']);
  let ref = null;
  let document = null;
  let pagesCache = new Map();
  let textLayers = new Map();
  let closed = false;
  let aborted = false;
  let generation = 0;
  let zoom = { kind: 'scale', value: 1 };
  let position = { page: 1 };
  let highlights = [];

  function requireOpen() {
    if (closed) throw readerError(READER_ERROR_CODES.CLOSED, 'The reader is closed');
    if (aborted) throw readerError(READER_ERROR_CODES.ABORTED, 'The reader was aborted');
    if (!document || !ref) throw readerError(READER_ERROR_CODES.NOT_OPEN, 'No document is open');
    return document;
  }

  function emitViewChange(reason) {
    emitter.emit('viewchange', {
      reason,
      document_id: ref ? ref.document_id : null,
      revision_id: ref ? ref.revision_id : null,
      artifact_id: ref ? ref.artifact_id : undefined,
      build_id: ref ? ref.build_id : undefined,
      page: position ? position.page : null,
      zoom: { ...zoom },
      coordinate_space: PDF_COORDINATE_SPACE,
      highlight_count: highlights.length,
      ready: document !== null,
    });
  }

  async function getPage(pageNumber) {
    const current = requireOpen();
    if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > current.numPages) {
      throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, `page must be an integer in 1..${current.numPages}`, { page: pageNumber });
    }
    if (!pagesCache.has(pageNumber)) {
      const page = await current.getPage(pageNumber);
      pagesCache.set(pageNumber, page);
    }
    return pagesCache.get(pageNumber);
  }

  /**
   * Text layer for one page. Returns `null` for a page without extractable text so
   * the caller must surface `no_text_layer` instead of a fake empty string.
   */
  async function getPageText(pageNumber) {
    if (textLayers.has(pageNumber)) return textLayers.get(pageNumber);
    const page = await getPage(pageNumber);
    if (typeof page.getTextContent !== 'function') {
      textLayers.set(pageNumber, null);
      return null;
    }
    let content;
    try {
      content = await page.getTextContent();
    } catch (error) {
      textLayers.set(pageNumber, null);
      return null;
    }
    const items = Array.isArray(content && content.items) ? content.items : [];
    const text = items.map(item => (item && typeof item.str === 'string' ? item.str : '')).join(' ').trim();
    const entry = text.length ? { page: pageNumber, text, item_count: items.length } : null;
    textLayers.set(pageNumber, entry);
    return entry;
  }

  function pageGeometry(page) {
    return {
      view: page && page.view,
      rotate: (page && page.rotate) || 0,
      userUnit: (page && page.userUnit) || 1,
    };
  }

  return {
    coordinateSpace: PDF_COORDINATE_SPACE,
    engineAvailable: true,

    async open(nextRef, { data, url, password, engineOptions } = {}) {
      if (closed) throw readerError(READER_ERROR_CODES.CLOSED, 'The reader is closed');
      const bound = makeDocumentRevisionRef(nextRef || {});
      const token = ++generation;
      aborted = false;
      let loaded;
      try {
        const source = data ?? url;
        if (source === undefined) {
          throw readerError(READER_ERROR_CODES.LOAD_FAILED, 'open() needs PDF data or a url');
        }
        loaded = await loader.getDocument({
          ...(data !== undefined ? { data } : { url }),
          ...(password !== undefined ? { password } : {}),
          ...(engineOptions || {}),
        });
        // PDF.js exposes `promise`; other engines may resolve directly.
        if (loaded && typeof loaded.then === 'function') loaded = await loaded;
        if (loaded && loaded.promise && typeof loaded.promise.then === 'function') loaded = await loaded.promise;
      } catch (error) {
        if (token !== generation) throw readerError(READER_ERROR_CODES.ABORTED, 'The open call was aborted');
        throw mapEngineError(error);
      }
      if (token !== generation) {
        if (loaded && typeof loaded.destroy === 'function') {
          try {
            await loaded.destroy();
          } catch {
            // Destroying a superseded document is best effort.
          }
        }
        throw readerError(READER_ERROR_CODES.ABORTED, 'The open call was aborted');
      }
      document = loaded;
      ref = bound;
      pagesCache = new Map();
      textLayers = new Map();
      highlights = [];
      position = { page: 1 };
      emitViewChange('open');
      return {
        document_id: bound.document_id,
        revision_id: bound.revision_id,
        artifact_id: bound.artifact_id,
        build_id: bound.build_id,
        num_pages: document.numPages,
        coordinate_space: PDF_COORDINATE_SPACE,
      };
    },

    async close() {
      closed = true;
      aborted = false;
      generation += 1;
      const closing = document;
      document = null;
      ref = null;
      pagesCache = new Map();
      textLayers = new Map();
      highlights = [];
      emitter.clearListeners();
      if (closing && typeof closing.destroy === 'function') await closing.destroy();
    },

    /** Abort in-flight loading and drop the pending document. */
    abort() {
      aborted = true;
      generation += 1;
      if (document && typeof document.destroy === 'function') {
        try {
          document.destroy();
        } catch {
          // Aborting is best effort; state is already invalidated.
        }
      }
      document = null;
      ref = null;
      pagesCache = new Map();
      textLayers = new Map();
      emitViewChange('abort');
    },

    /** Clear caches and selection state when switching documents. */
    clear() {
      document = null;
      ref = null;
      pagesCache = new Map();
      textLayers = new Map();
      highlights = [];
      position = { page: 1 };
      aborted = false;
      generation += 1;
      emitViewChange('clear');
    },

    async numPages() {
      return requireOpen().numPages;
    },

    async getOutline() {
      const current = requireOpen();
      if (typeof current.getOutline !== 'function') return [];
      const outline = await current.getOutline();
      if (!Array.isArray(outline)) return [];
      const convert = items => (items || []).map(item => ({
        id: item.id ?? null,
        title: item.title ?? '',
        page: destructurePage(item.dest),
        children: convert(item.items),
      }));
      return convert(outline);
    },

    /** Page text, or an explicit `no_text_layer` failure. Never invents offsets. */
    async getText(pageNumber = position.page) {
      requireOpen();
      const entry = await getPageText(pageNumber);
      if (!entry) {
        throw readerError(READER_ERROR_CODES.NO_TEXT_LAYER, `Page ${pageNumber} has no extractable text layer`, { page: pageNumber });
      }
      return entry;
    },

    async search(query, options = {}) {
      const current = requireOpen();
      const text = typeof query === 'string' ? query : query && query.text;
      if (typeof text !== 'string' || text.length === 0) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'search needs a non-empty query text');
      }
      const matchCase = Boolean(query && query.matchCase);
      const first = Number.isInteger(options.fromPage) ? options.fromPage : 1;
      const maxPages = Number.isInteger(options.maxPages) && options.maxPages > 0 ? options.maxPages : DEFAULT_MAX_PAGES;
      const maxResults = Number.isInteger(options.maxResults) && options.maxResults > 0 ? options.maxResults : DEFAULT_MAX_RESULTS;
      const last = Math.min(current.numPages, first + maxPages - 1);
      const needle = matchCase ? text : text.toLowerCase();
      const token = generation;
      const results = [];
      let scanned = 0;
      for (let page = first; page <= last; page += 1) {
        if (token !== generation || aborted) {
          results.cancelled = true;
          break;
        }
        const entry = await getPageText(page);
        scanned += 1;
        if (!entry) continue;
        const haystack = matchCase ? entry.text : entry.text.toLowerCase();
        let index = 0;
        while (index + needle.length <= haystack.length) {
          const found = haystack.indexOf(needle, index);
          if (found === -1) break;
          results.push({
            index: results.length,
            page,
            text_range: { start: found, end: found + needle.length, unit: 'codepoint', page_relative: true },
            quote: entry.text.slice(found, found + needle.length),
            prefix: entry.text.slice(Math.max(0, found - QUOTE_CONTEXT_LENGTH), found),
            suffix: entry.text.slice(found + needle.length, found + needle.length + QUOTE_CONTEXT_LENGTH),
          });
          if (results.length >= maxResults) {
            results.truncated = true;
            break;
          }
          index = found + Math.max(1, needle.length);
        }
        if (results.truncated) break;
        await Promise.resolve();
      }
      // A hard page limit that stopped the scan is reported, never silent.
      results.truncated = Boolean(results.truncated) || scanned < current.numPages;
      results.scanned_pages = scanned;
      results.coordinate_space = PDF_COORDINATE_SPACE;
      return results;
    },

    /**
     * Build a profile-correct PDF reference draft from canonical rects.
     * `pages` must be an array: one multi-page selection, never a single `page`.
     */
    selectPages(selection = {}) {
      requireOpen();
      if (!Array.isArray(selection.pages) || selection.pages.length === 0) {
        throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, 'selectPages needs a non-empty pages[] array');
      }
      const pages = [];
      for (const entry of selection.pages) {
        const rects = (entry.rects ?? []).map(rect => normalizeRect(rect));
        const quads = entry.quads === undefined ? undefined : entry.quads.map(rect => normalizeRect(rect));
        if (rects.length === 0 && quads === undefined) {
          throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, `Selection page ${entry.page} needs rects[] or quads[]`, { page: entry.page });
        }
        normalizePageAnchor(pages, entry.page, rects, quads);
      }
      const draft = createReferenceDraft(ref, {
        selector: {
          type: 'pdf',
          pages,
          coordinate_space: PDF_COORDINATE_SPACE,
          ...(selection.rotation === undefined ? {} : { rotation: selection.rotation }),
          ...(selection.cropbox_transform === undefined ? {} : { cropbox_transform: selection.cropbox_transform }),
        },
        quote: selection.quote ?? '',
        prefix: selection.prefix ?? '',
        suffix: selection.suffix ?? '',
        extractor_version: EXTRACTOR_VERSION,
      });
      emitter.emit('selection', {
        document_id: draft.document_id,
        revision_id: draft.revision_id,
        artifact_id: draft.artifact_id,
        build_id: draft.build_id,
        selector: draft.selector,
        quote: draft.quote,
        prefix: draft.prefix,
        suffix: draft.suffix,
      });
      emitter.emit('reference', draft);
      return draft;
    },

    /** Convert a screen click into a canonical rect so a selection can be formed. */
    pointToCanonicalRect(pageNumber, point, size = { w: 0.002, h: 0.002 }) {
      const page = pagesCache.get(pageNumber);
      const geometry = page ? pageGeometry(page) : { view: undefined };
      const canonical = screenPointToCanonical(point, geometry);
      return normalizeRect({ x: canonical.x, y: canonical.y, w: size.w, h: size.h });
    },

    async goTo(target = {}) {
      const current = requireOpen();
      if (target.pages && Array.isArray(target.pages.pages) && target.pages.pages.length) {
        // A stored pdf selector: jump to its first page (display mapping only).
        target = { page: target.pages.pages[0].page };
      }
      if (Number.isInteger(target.page)) {
        if (target.page < 1 || target.page > current.numPages) {
          throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, `page must be in 1..${current.numPages}`, { target });
        }
        position = { page: target.page };
      } else {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'goTo needs a page', { target });
      }
      emitViewChange('navigate');
      return { ...position };
    },

    setZoom(level) {
      if (!level || (level.kind !== 'scale' && level.kind !== 'fit-width' && level.kind !== 'fit-page')) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'zoom must be {kind:"scale"|"fit-width"|"fit-page"}', { level });
      }
      if (level.kind === 'scale' && !(typeof level.value === 'number' && level.value > 0)) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'scale zoom needs a positive value', { level });
      }
      zoom = { ...level };
      emitViewChange('zoom');
    },

    /** Validate and remember anchors; stored canonical rects are never rewritten. */
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
        ready: document !== null,
        page: position ? position.page : null,
        zoom: { ...zoom },
        coordinate_space: PDF_COORDINATE_SPACE,
        highlight_count: highlights.length,
      };
    },

    on(event, listener) {
      return emitter.on(event, listener);
    },
  };
}

/** Extract a 1-based page number from an outline destination, when present. */
function destructurePage(dest) {
  if (!dest) return null;
  const pair = Array.isArray(dest) ? dest : Array.isArray(dest.dest) ? dest.dest : null;
  if (!pair || pair.length === 0) return null;
  const first = pair[0];
  // Either an explicit page index (0-based) or a named destination reference.
  if (Number.isInteger(first)) return first + 1;
  return null;
}