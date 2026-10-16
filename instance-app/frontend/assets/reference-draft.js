/**
 * Unified reference draft schema shared by every browser reader adapter.
 *
 * Field names follow docs/document-readers.md §2 and docs/instance-app-api.md §6,
 * which are the single source of truth: the API clarifications win over earlier
 * reader-doc drafts, so the PDF selector canon here is a `pages[]` array plus
 * `coordinate_space: 'normalized-cropbox-unrotated-top-left'`. There is exactly
 * one PDF coordinate space in this module; `rotation` and `cropbox_transform`
 * are display-only and never rewrite stored rects.
 *
 * No dependencies. This module only builds and validates plain objects; it never
 * touches the DOM, the network, or reader state.
 */

import { READER_ERROR_CODES, readerError } from './reader-errors.js';

export const OFFSET_UNITS = Object.freeze(['codepoint', 'utf16']);

/** The one PDF coordinate space, per docs/instance-app-api.md §6. */
export const PDF_COORDINATE_SPACE = 'normalized-cropbox-unrotated-top-left';

export const SELECTOR_TYPES = Object.freeze(['text', 'pdf', 'tex-source', 'whole-document']);

// Context windows for canonical quotes are fixed-length by contract.
export const QUOTE_CONTEXT_LENGTH = 32;

function assertNonEmptyString(value, label, code = READER_ERROR_CODES.INVALID_ARGUMENT) {
  if (typeof value !== 'string' || value.length === 0) {
    throw readerError(code, `${label} must be a non-empty string`, { field: label });
  }
  return value;
}

function assertOffsetUnit(unit, code = READER_ERROR_CODES.INVALID_SELECTOR) {
  if (!OFFSET_UNITS.includes(unit)) {
    throw readerError(code, `unit must be one of ${OFFSET_UNITS.join('|')}`, { unit });
  }
  return unit;
}

function assertOffset(value, label, code = READER_ERROR_CODES.INVALID_SELECTOR) {
  if (!Number.isInteger(value) || value < 0) {
    throw readerError(code, `${label} must be a non-negative integer`, { field: label, value });
  }
  return value;
}

/**
 * A canonical relative path inside a TeX project (e.g. `main.tex`,
 * `sections/intro.tex`). Absolute paths and parent traversal are rejected here so
 * a malformed anchor cannot claim a location outside the project.
 */
export function normalizeResourcePath(file) {
  assertNonEmptyString(file, 'file', READER_ERROR_CODES.TEX_SOURCE_FILE_REQUIRED);
  const normalized = file.replace(/\\/g, '/').replace(/^\.\//, '');
  if (normalized.startsWith('/') || /^[A-Za-z]:/.test(normalized)) {
    throw readerError(READER_ERROR_CODES.TEX_PATH_INVALID, 'file must be a project-relative path', { file });
  }
  if (normalized.split('/').some(part => part === '..')) {
    throw readerError(READER_ERROR_CODES.TEX_PATH_INVALID, 'file must not traverse outside the project', { file });
  }
  return normalized;
}

function assertFinite(value, label) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, `${label} must be a finite number`, { field: label, value });
  }
  return value;
}

/**
 * Normalized CropBox rect: x/y/w/h in 0..1, origin top-left, x right y down.
 * Deliberately rejects out-of-range input instead of clamping, because a silently
 * clamped rect would move a highlight without telling anyone.
 */
export function normalizeRect(rect, label = 'rect') {
  if (!rect || typeof rect !== 'object') {
    throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, `${label} must be an object`, { rect });
  }
  const x = assertFinite(rect.x, `${label}.x`);
  const y = assertFinite(rect.y, `${label}.y`);
  const w = assertFinite(rect.w, `${label}.w`);
  const h = assertFinite(rect.h, `${label}.h`);
  if (x < 0 || y < 0 || w < 0 || h < 0 || x > 1 || y > 1 || w > 1 || h > 1) {
    throw readerError(
      READER_ERROR_CODES.INVALID_SELECTOR,
      `${label} values must lie in 0..1 of the unrotated CropBox`,
      { rect },
    );
  }
  return { x, y, w, h };
}

/** One `pages[]` entry: 1-based page number plus visual rects (and optional quads). */
export function normalizePdfPageAnchor(anchor, label = 'pages[]') {
  if (!anchor || typeof anchor !== 'object') {
    throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, `${label} must be an object`, { anchor });
  }
  if (!Number.isInteger(anchor.page) || anchor.page < 1) {
    throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, `${label}.page must be an integer >= 1`, { page: anchor.page });
  }
  const rects = (anchor.rects ?? []).map((rect, index) => normalizeRect(rect, `${label}[${index}].rects[]`));
  const quads = anchor.quads === undefined
    ? undefined
    : anchor.quads.map((rect, index) => normalizeRect(rect, `${label}[${index}].quads[]`));
  if (rects.length === 0 && quads === undefined) {
    throw readerError(
      READER_ERROR_CODES.INVALID_SELECTOR,
      `${label} for page ${anchor.page} needs rects[] or quads[]`,
      { page: anchor.page },
    );
  }
  return quads === undefined ? { page: anchor.page, rects } : { page: anchor.page, rects, quads };
}

/** Validate and canonicalize a selector; multi-page selections must use `pages[]`. */
export function normalizeSelector(selector) {
  if (!selector || typeof selector !== 'object') {
    throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, 'selector must be an object', { selector });
  }
  switch (selector.type) {
    case 'text':
      return {
        type: 'text',
        start: assertOffset(selector.start, 'start'),
        end: assertOffsetEnd(selector.start, selector.end),
        unit: assertOffsetUnit(selector.unit),
      };
    case 'tex-source':
      return {
        type: 'tex-source',
        file: normalizeResourcePath(selector.file),
        start: assertOffset(selector.start, 'start'),
        end: assertOffsetEnd(selector.start, selector.end),
        unit: assertOffsetUnit(selector.unit),
      };
    case 'pdf': {
      if (!Array.isArray(selector.pages) || selector.pages.length === 0) {
        throw readerError(
          READER_ERROR_CODES.INVALID_SELECTOR,
          'a pdf selector requires a non-empty pages[] array',
          { selector },
        );
      }
      const normalized = {
        type: 'pdf',
        pages: selector.pages.map((anchor, index) => normalizePdfPageAnchor(anchor, `pages[${index}]`)),
        coordinate_space: PDF_COORDINATE_SPACE,
      };
      // Display-only fields are preserved verbatim, never derived from rects.
      if (selector.rotation !== undefined) normalized.rotation = assertFinite(selector.rotation, 'rotation');
      if (selector.cropbox_transform !== undefined) {
        if (!Array.isArray(selector.cropbox_transform) || selector.cropbox_transform.some(v => typeof v !== 'number')) {
          throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, 'cropbox_transform must be a number array', { selector });
        }
        normalized.cropbox_transform = [...selector.cropbox_transform];
      }
      return normalized;
    }
    case 'whole-document':
      return { type: 'whole-document' };
    default:
      throw readerError(
        READER_ERROR_CODES.INVALID_SELECTOR,
        `selector.type must be one of ${SELECTOR_TYPES.join('|')}`,
        { type: selector.type },
      );
  }
}

function assertOffsetEnd(start, end) {
  if (!Number.isInteger(end) || end < 0 || (Number.isInteger(start) && end < start)) {
    throw readerError(READER_ERROR_CODES.INVALID_SELECTOR, 'end must be an integer >= start', { end, start });
  }
  return end;
}

/**
 * Normalize a canonical quote: line endings unified to `\n`, no rendering
 * decoration. Kept deliberately small; the backend still re-verifies the quote.
 */
export function normalizeQuote(text) {
  if (typeof text !== 'string') {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'quote must be a string');
  }
  return text.replace(/\r\n?/g, '\n');
}

/** A fixed-length context window ending at `index`. */
export function prefixWindow(text, index, length = QUOTE_CONTEXT_LENGTH) {
  if (typeof text !== 'string' || !Number.isInteger(index)) {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'prefixWindow needs text and an integer index');
  }
  return text.slice(Math.max(0, index - length), index);
}

/** A fixed-length context window starting at `index`. */
export function suffixWindow(text, index, length = QUOTE_CONTEXT_LENGTH) {
  if (typeof text !== 'string' || !Number.isInteger(index)) {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'suffixWindow needs text and an integer index');
  }
  return text.slice(index, index + length);
}

export function makeDocumentRevisionRef({ document_id, revision_id, artifact_id, build_id }) {
  assertNonEmptyString(document_id, 'document_id', READER_ERROR_CODES.INVALID_REFERENCE);
  assertNonEmptyString(revision_id, 'revision_id', READER_ERROR_CODES.INVALID_REFERENCE);
  const ref = { document_id, revision_id };
  if (artifact_id !== undefined) ref.artifact_id = assertNonEmptyString(artifact_id, 'artifact_id');
  if (build_id !== undefined) ref.build_id = assertNonEmptyString(build_id, 'build_id');
  return ref;
}

/**
 * Build a `ReferenceDraft`. `document_id` and `revision_id` always come from the
 * ref bound at open time and cannot be overridden by a selection, so an anchor
 * can never drift onto another document or revision.
 */
export function createReferenceDraft(ref, {
  selector,
  quote = '',
  prefix = '',
  suffix = '',
  extractor_version,
  created_at,
} = {}) {
  const base = makeDocumentRevisionRef(ref || {});
  const draft = {
    document_id: base.document_id,
    revision_id: base.revision_id,
    selector: normalizeSelector(selector),
    quote: normalizeQuote(quote),
    prefix: normalizeQuote(prefix),
    suffix: normalizeQuote(suffix),
  };
  if (base.artifact_id !== undefined) draft.artifact_id = base.artifact_id;
  if (base.build_id !== undefined) draft.build_id = base.build_id;
  if (extractor_version !== undefined) draft.extractor_version = assertNonEmptyString(extractor_version, 'extractor_version');
  draft.created_at = created_at ?? new Date(0).toISOString();
  return draft;
}

/**
 * Validate that a draft (or a persisted anchor) still carries the invariants:
 * a bound revision, a canonical selector, and `artifact_id`/`build_id` paired for
 * product references. Never rewrites the binding to a newer revision.
 */
export function validateReferenceDraft(draft) {
  if (!draft || typeof draft !== 'object') {
    throw readerError(READER_ERROR_CODES.INVALID_REFERENCE, 'a reference draft is required');
  }
  makeDocumentRevisionRef(draft);
  const selector = normalizeSelector(draft.selector);
  if (selector.type === 'pdf' && (draft.artifact_id === undefined || draft.build_id === undefined)) {
    throw readerError(
      READER_ERROR_CODES.INVALID_REFERENCE,
      'a compiled-PDF reference must carry both artifact_id and build_id',
      { selector_type: selector.type },
    );
  }
  if (draft.quote === undefined || draft.quote === null || typeof draft.quote !== 'string') {
    throw readerError(
      READER_ERROR_CODES.INVALID_REFERENCE,
      'quote is required: a locator without canonical text stays an explicit whole-document draft',
    );
  }
  return { ...draft, selector, quote: normalizeQuote(draft.quote) };
}