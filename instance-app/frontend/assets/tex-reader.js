/**
 * TeX reader adapters (browser-side ES module, no dependencies).
 *
 * docs/document-readers.md §5 splits TeX into two shapes:
 *
 * 1. `createTexSourceReader` — a source view/editor over the project's canonical
 *    relative files. Anchors always carry `file` (mandatory, even for `main.tex`)
 *    and always persist `unit: 'codepoint'`.
 * 2. `createTexCompiledReader` — the *same* PDF reader from `pdf-reader.js`, with a
 *    binding that fixes `revision_id` (source) together with `artifact_id`/`build_id`
 *    (product). A compiled artifact is a distinct identity from its source revision;
 *    they are never interchanged.
 *
 * There is no TeX compiler in this repository and no safe sandbox for one
 * (docs/tex-compilation-security.md §1, §6). `compileProject()` therefore always
 * rejects with `503 tex_compile_unavailable`: it never queues, never runs a
 * subprocess, and never falls back to an unsafe path.
 */

import { createEmitter } from './reader-events.js';
import { READER_ERROR_CODES, readerError, toReaderError } from './reader-errors.js';
import { codepointToUtf16Index, utf16ToCodepointIndex } from './reader-offsets.js';
import {
  createReferenceDraft,
  makeDocumentRevisionRef,
  normalizeResourcePath,
  prefixWindow,
  suffixWindow,
  validateReferenceDraft,
} from './reference-draft.js';
import { createPdfReader, resolvePdfEngine } from './pdf-reader.js';

const EXTRACTOR_VERSION = 'frontend-tex-1';
const QUOTE_CONTEXT_LENGTH = 32;

/** Compilation is unavailable by contract, never by silent failure. */
export function compileProject() {
  return Promise.reject(readerError(
    READER_ERROR_CODES.TEX_COMPILE_UNAVAILABLE,
    'TeX compilation needs a trusted, isolated executor; this repository has none, so compilation is unavailable and there is no fallback',
  ));
}

/**
 * Validate a project source map: `{ main, resources: { path: text } }`.
 * `main` must be present among the resources (docs/instance-app-api.md §4e).
 */
export function normalizeTexProject(project = {}) {
  if (!project || typeof project !== 'object') {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'A TeX project needs { main, resources }');
  }
  const resources = {};
  const input = project.resources || {};
  for (const [rawPath, text] of Object.entries(input)) {
    const path = normalizeResourcePath(rawPath);
    if (typeof text !== 'string') {
      throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, `Resource ${path} must be text`, { path });
    }
    resources[path] = text;
  }
  const main = normalizeResourcePath(project.main || '');
  if (!(main in resources)) {
    throw readerError(READER_ERROR_CODES.TEX_MAIN_INVALID, `The main entry ${main} is not among the project resources`, { main });
  }
  return { main, resources };
}

/**
 * TeX source reader/editor. `compile()` always reports `tex_compile_unavailable`;
 * it exists so wiring can surface the contract instead of inventing a path.
 */
export function createTexSourceReader({ project } = {}) {
  const emitter = createEmitter(['selection', 'reference', 'viewchange', 'editor', 'error']);
  let ref = null;
  let state = null;
  let closed = false;
  let generation = 0;
  let position = { file: null, offset: 0 };
  let highlights = [];
  let dirty = false;

  function requireOpen() {
    if (closed) throw readerError(READER_ERROR_CODES.CLOSED, 'The reader is closed');
    if (!state || !ref) throw readerError(READER_ERROR_CODES.NOT_OPEN, 'No TeX project is open');
    return state;
  }

  function fileText(file) {
    const current = requireOpen();
    const path = normalizeResourcePath(file);
    if (!(path in current.resources)) {
      throw readerError(READER_ERROR_CODES.TEX_PATH_INVALID, `Unknown project file ${path}`, { file: path });
    }
    return { path, text: current.resources[path] };
  }

  function emitViewChange(reason) {
    emitter.emit('viewchange', {
      reason,
      document_id: ref ? ref.document_id : null,
      revision_id: ref ? ref.revision_id : null,
      file: position ? position.file : null,
      offset: position ? position.offset : 0,
      dirty,
      files: state ? Object.keys(state.resources) : [],
      main: state ? state.main : null,
      highlight_count: highlights.length,
      ready: state !== null,
    });
  }

  function fileMetadata() {
    const current = requireOpen();
    return Object.keys(current.resources).sort().map(path => ({
      path,
      role: path === current.main ? 'main' : 'resource',
      codepoint_length: [...current.resources[path]].length,
      utf16_length: current.resources[path].length,
      unit: 'codepoint',
    }));
  }

  return {
    async open(nextRef, { project: projectInput } = {}) {
      if (closed) throw readerError(READER_ERROR_CODES.CLOSED, 'The reader is closed');
      const bound = makeDocumentRevisionRef(nextRef || {});
      const token = ++generation;
      const source = projectInput || project;
      if (!source) throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'open() needs { main, resources }');
      const normalized = normalizeTexProject(source);
      if (token !== generation) throw readerError(READER_ERROR_CODES.ABORTED, 'The open call was aborted');
      state = normalized;
      ref = bound;
      position = { file: normalized.main, offset: 0 };
      highlights = [];
      dirty = false;
      emitViewChange('open');
      return {
        document_id: bound.document_id,
        revision_id: bound.revision_id,
        main: normalized.main,
        files: fileMetadata(),
      };
    },

    close() {
      closed = true;
      generation += 1;
      ref = null;
      state = null;
      highlights = [];
      position = { file: null, offset: 0 };
      dirty = false;
      emitter.clearListeners();
    },

    abort() {
      generation += 1;
      emitViewChange('abort');
    },

    clear() {
      state = null;
      ref = null;
      highlights = [];
      position = { file: null, offset: 0 };
      dirty = false;
      generation += 1;
      emitViewChange('clear');
    },

    /** Metadata for every project file: path, role and both unit lengths. */
    getFiles() {
      requireOpen();
      return fileMetadata();
    },

    getMain() {
      return requireOpen().main;
    },

    /** Canonical source text of one file. `file` is mandatory by contract. */
    getText(file = position.file) {
      return fileText(file).text;
    },

    getOutline() {
      const current = requireOpen();
      return Promise.resolve(Object.keys(current.resources).sort().map(path => ({
        id: `file:${path}`,
        title: path,
        file: path,
        role: path === current.main ? 'main' : 'resource',
        children: [],
      })));
    },

    loadFile(file) {
      const loaded = fileText(file);
      position = { file: loaded.path, offset: 0 };
      emitter.emit('editor', { type: 'load', file: loaded.path, text: loaded.text, unit: 'codepoint' });
      emitViewChange('load-file');
      return loaded;
    },

    /** Editor change notification (the adapter stores nothing itself). */
    setText(file, text) {
      if (typeof text !== 'string') throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'setText needs a string');
      const loaded = fileText(file);
      dirty = true;
      emitter.emit('editor', { type: 'change', file: loaded.path, text, unit: 'codepoint' });
      emitViewChange('edit');
    },

    /**
     * Build a `tex-source` anchor. `start`/`end` accept either unit; the draft is
     * always emitted with `unit: 'codepoint'` and its mandatory `file`.
     */
    selectRange(file, range, unit = 'utf16') {
      const loaded = fileText(file);
      if (!range || !Number.isInteger(range.start) || !Number.isInteger(range.end) || range.end < range.start) {
        throw readerError(READER_ERROR_CODES.INVALID_OFFSET, 'A selection requires integer start <= end', { range });
      }
      const codepoint = unit === 'utf16'
        ? { start: utf16ToCodepointIndex(loaded.text, range.start), end: utf16ToCodepointIndex(loaded.text, range.end) }
        : unit === 'codepoint'
          ? { start: range.start, end: range.end }
          : (() => {
            throw readerError(READER_ERROR_CODES.INVALID_OFFSET, `Unknown offset unit ${String(unit)}`, { unit });
          })();
      const utf16 = {
        start: codepointToUtf16Index(loaded.text, codepoint.start),
        end: codepointToUtf16Index(loaded.text, codepoint.end),
      };
      const draft = createReferenceDraft(ref, {
        selector: { type: 'tex-source', file: loaded.path, start: codepoint.start, end: codepoint.end, unit: 'codepoint' },
        quote: loaded.text.slice(utf16.start, utf16.end),
        prefix: prefixWindow(loaded.text, utf16.start, QUOTE_CONTEXT_LENGTH),
        suffix: suffixWindow(loaded.text, utf16.end, QUOTE_CONTEXT_LENGTH),
        extractor_version: EXTRACTOR_VERSION,
      });
      position = { file: loaded.path, offset: codepoint.start };
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

    /**
     * Strict search over one file or the whole project. Only codepoint ranges are
     * returned; a project-wide scan reports the files it visited.
     */
    async search(query, options = {}) {
      const current = requireOpen();
      const text = typeof query === 'string' ? query : query && query.text;
      if (typeof text !== 'string' || text.length === 0) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'search needs a non-empty query text');
      }
      const matchCase = Boolean(query && query.matchCase);
      const maxResults = Number.isInteger(options.maxResults) && options.maxResults > 0 ? options.maxResults : 200;
      const targets = options.file ? [fileText(options.file)] : Object.keys(current.resources).sort().map(path => keyed(current, path));
      const needle = matchCase ? text : text.toLowerCase();
      const results = [];
      for (const target of targets) {
        const haystack = matchCase ? target.text : target.text.toLowerCase();
        let index = 0;
        while (index + needle.length <= haystack.length) {
          const found = haystack.indexOf(needle, index);
          if (found === -1) break;
          const end = found + needle.length;
          results.push({
            index: results.length,
            file: target.path,
            source_range: {
              start: utf16ToCodepointIndex(target.text, found),
              end: utf16ToCodepointIndex(target.text, end),
              unit: 'codepoint',
            },
            quote: target.text.slice(found, end),
            prefix: target.text.slice(Math.max(0, found - QUOTE_CONTEXT_LENGTH), found),
            suffix: target.text.slice(end, end + QUOTE_CONTEXT_LENGTH),
          });
          if (results.length >= maxResults) {
            results.truncated = true;
            return results;
          }
          index = end;
        }
        await Promise.resolve();
      }
      results.truncated = false;
      return results;
    },

    /** Navigate to a file and (optionally) a codepoint offset within it. */
    async goTo(target = {}) {
      const loaded = fileText(target.file || position.file);
      const offset = target.offset ?? 0;
      if (!Number.isInteger(offset) || offset < 0 || offset > [...loaded.text].length) {
        throw readerError(READER_ERROR_CODES.INVALID_OFFSET, 'offset is outside the file', { target });
      }
      position = { file: loaded.path, offset };
      emitViewChange('navigate');
      return { ...position };
    },

    setZoom(level) {
      if (!level || (level.kind !== 'scale' && level.kind !== 'fit-width')) {
        throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'zoom must be {kind:"scale"|"fit-width"}', { level });
      }
      emitter.emit('viewchange', { reason: 'zoom', file: position.file, zoom: { ...level } });
    },

    setHighlight(anchors = []) {
      requireOpen();
      // A `tex-source` selector without `file` is rejected by normalizeSelector.
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
        ready: state !== null,
        file: position ? position.file : null,
        offset: position ? position.offset : 0,
        dirty,
        main: state ? state.main : null,
        files: state ? Object.keys(state.resources).sort() : [],
        highlight_count: highlights.length,
      };
    },

    /** Always rejects with `tex_compile_unavailable`; there is no fallback path. */
    compile() {
      return compileProject();
    },

    on(event, listener) {
      return emitter.on(event, listener);
    },
  };
}

function keyed(current, path) {
  return { path, text: current.resources[path] };
}

/**
 * Compiled-artifact reader: the shared PDF reader plus a binding that carries both
 * the source `revision_id` and the product `artifact_id`/`build_id`. Creating it
 * requires an injected PDF engine; without one it throws `pdf_engine_unavailable`.
 */
export function createTexCompiledReader(options = {}) {
  resolvePdfEngine(options); // fail fast, before any reader state exists
  const reader = createPdfReader(options);
  return {
    ...reader,
    /**
     * Open a compiled artifact. `artifact_id` and `build_id` are mandatory because a
     * product anchor is unverifiable without them.
     */
    async openArtifact(ref, load) {
      const bound = makeDocumentRevisionRef(ref || {});
      if (bound.artifact_id === undefined || bound.build_id === undefined) {
        throw readerError(
          READER_ERROR_CODES.INVALID_REFERENCE,
          'A compiled artifact needs both artifact_id and build_id alongside the source revision_id',
        );
      }
      const source = typeof load === 'function' ? await load(bound) : load;
      if (source === undefined) {
        throw readerError(READER_ERROR_CODES.LOAD_FAILED, 'openArtifact needs the artifact bytes or a loader');
      }
      return reader.open(bound, typeof source === 'object' && !(source instanceof ArrayBuffer) && !ArrayBuffer.isView(source)
        ? source
        : { data: source });
    },
  };
}