/**
 * Reader adapter registry (browser-side ES module entry point).
 *
 * This module only assembles adapters. It is deliberately NOT wired into the
 * existing sync workbench: `lease-lifecycle.js` and `invitation-entry.js` are
 * untouched and nothing here is auto-installed. A later integration step calls
 * `createReaderRegistry(...)` once and routes by the document's detected format.
 *
 * PDF availability is honest: the PDF adapter requires an injected engine, so
 * `capabilities()` reports `pdf: false` and the resolver throws
 * `pdf_engine_unavailable` rather than importing a bundled/remote engine.
 * TeX compilation is likewise unavailable (`tex_compile_unavailable`).
 */

import { READER_ERROR_CODES, isReaderError, readerError } from './reader-errors.js';
import { createMarkdownReader } from './markdown-reader.js';
import { createPdfReader, resolvePdfEngine } from './pdf-reader.js';
import { compileProject, createTexCompiledReader, createTexSourceReader, normalizeTexProject } from './tex-reader.js';

export const SUPPORTED_FORMATS = Object.freeze(['md', 'txt', 'pdf', 'tex']);

const FORMAT_ALIASES = new Map([
  ['md', 'md'],
  ['markdown', 'md'],
  ['mkd', 'md'],
  ['txt', 'txt'],
  ['text', 'txt'],
  ['plain', 'txt'],
  ['tex', 'tex'],
  ['latex', 'tex'],
  ['pdf', 'pdf'],
]);

/** Detect a format from an explicit `format`, an extension, or a media type. */
export function detectFormat(value) {
  if (!value) return null;
  if (typeof value === 'string') {
    const token = value.toLowerCase().trim();
    if (FORMAT_ALIASES.has(token)) return FORMAT_ALIASES.get(token);
    const extension = token.split('.').pop();
    if (FORMAT_ALIASES.has(extension)) return FORMAT_ALIASES.get(extension);
    if (token === 'application/pdf') return 'pdf';
    return null;
  }
  if (typeof value === 'object') {
    if (value.format) return detectFormat(value.format);
    if (value.filename) return detectFormat(value.filename);
    if (value.extension) return detectFormat(value.extension);
    if (value.media_type) return detectFormat(value.media_type);
  }
  return null;
}

export function isReadableFormat(format) {
  return SUPPORTED_FORMATS.includes(format);
}

/**
 * Create the reader registry.
 *
 * options.loadText / options.renderer — passed to the markdown adapter.
 * options.engine / options.pdfjsLib / options.pdfjs — required for PDF.
 * options.environment — injected fetch/DOM environment (defaults to globalThis).
 */
export function createReaderRegistry(options = {}) {
  const environment = options.environment ?? globalThis;

  function markdown() {
    return createMarkdownReader({ environment, loadText: options.loadText, renderer: options.renderer });
  }

  function pdf() {
    // resolvePdfEngine throws pdf_engine_unavailable when nothing was injected.
    return createPdfReader({ ...options, environment });
  }

  function texSource() {
    return createTexSourceReader({ project: options.project });
  }

  function texCompiled() {
    return createTexCompiledReader({ ...options, environment });
  }

  const factories = { md: markdown, txt: markdown, pdf, tex: texSource };

  return {
    /** Capability snapshot the wiring can render before opening anything. */
    capabilities() {
      let pdfAvailable = true;
      let pdfReason;
      try {
        resolvePdfEngine(options);
      } catch (error) {
        pdfAvailable = false;
        pdfReason = error.code || READER_ERROR_CODES.ENGINE_UNAVAILABLE;
      }
      return {
        formats: [...SUPPORTED_FORMATS],
        readers: {
          md: { kind: 'markdown' },
          txt: { kind: 'markdown' },
          pdf: { kind: 'pdf', available: pdfAvailable, reason: pdfReason },
          tex: { kind: 'tex-source' },
          'tex-compiled': { kind: 'tex-compiled', available: pdfAvailable, reason: pdfReason },
        },
        tex_compile: { available: false, reason: READER_ERROR_CODES.TEX_COMPILE_UNAVAILABLE },
      };
    },

    /** Build an adapter for a format, or fail with `unsupported_format`. */
    create(format) {
      const resolved = detectFormat(format);
      const factory = resolved && factories[resolved];
      if (!factory) {
        throw readerError(READER_ERROR_CODES.UNSUPPORTED_FORMAT, `No reader adapter for ${String(format)}`, { format });
      }
      return factory();
    },

    /**
     * Open a document with the adapter matching its format. `open` receives the
     * adapter and the ref, so the caller controls the payload shape per format.
     */
    async open(format, ref, payload = {}) {
      const resolved = detectFormat(format);
      const factory = resolved && factories[resolved];
      if (!factory) {
        throw readerError(READER_ERROR_CODES.UNSUPPORTED_FORMAT, `No reader adapter for ${String(format)}`, { format });
      }
      const reader = factory();
      await reader.open(ref, payload);
      return reader;
    },

    /**
     * Open a TeX compiled artifact with the shared PDF reader, bound to both the
     * source revision and the product identity.
     */
    async openCompiled(ref, load) {
      const reader = texCompiled();
      await reader.openArtifact(ref, load);
      return reader;
    },

    /** Compilation is unavailable by contract; surfaced for callers that ask. */
    compileTex() {
      return compileProject();
    },

    /** Normalize a TeX project payload without opening a reader. */
    inspectTexProject(project) {
      return normalizeTexProject(project);
    },
  };
}

export { READER_ERROR_CODES, isReaderError, readerError };
export {
  createMarkdownReader,
  createPdfReader,
  createTexSourceReader,
  createTexCompiledReader,
  resolvePdfEngine,
  compileProject,
};
export {
  PDF_COORDINATE_SPACE,
  OFFSET_UNITS,
  SELECTOR_TYPES,
  createReferenceDraft,
  validateReferenceDraft,
  normalizeSelector,
  makeDocumentRevisionRef,
} from './reference-draft.js';
export {
  codepointLength,
  utf16Length,
  utf16ToCodepointIndex,
  codepointToUtf16Index,
  convertRange,
  sliceCodepoints,
} from './reader-offsets.js';