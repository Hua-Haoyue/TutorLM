/**
 * Codepoint <-> UTF-16 offset conversion.
 *
 * docs/document-readers.md §3.2: the backend canonical unit is the Unicode
 * codepoint, while the browser DOM/JS unit is the UTF-16 code unit. Both units
 * exist side by side and must never be mixed inside one anchor, so every
 * conversion happens here and splits inside a surrogate pair are rejected
 * instead of silently producing a half-surrogate.
 *
 * No dependencies; pure string math on the exact string that was scanned.
 */

import { READER_ERROR_CODES, readerError } from './reader-errors.js';

function isHighSurrogate(code) {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLowSurrogate(code) {
  return code >= 0xdc00 && code <= 0xdfff;
}

/** Number of UTF-16 code units (== `String.prototype.length`). */
export function utf16Length(text) {
  return assertText(text).length;
}

/** Number of Unicode codepoints (scalar values) in `text`. */
export function codepointLength(text) {
  return assertText(text).length === 0 ? 0 : [...assertText(text)].length;
}

function assertText(text) {
  if (typeof text !== 'string') {
    throw readerError(READER_ERROR_CODES.INVALID_ARGUMENT, 'Expected a string');
  }
  return text;
}

function assertIndex(index, limit, label) {
  if (!Number.isInteger(index) || index < 0 || index > limit) {
    throw readerError(
      READER_ERROR_CODES.INVALID_OFFSET,
      `${label} ${String(index)} is outside 0..${limit}`,
      { index, limit, label },
    );
  }
  return index;
}

/**
 * Convert a UTF-16 index into a codepoint index.
 * Throws INVALID_OFFSET when the index lands inside a surrogate pair.
 */
export function utf16ToCodepointIndex(text, utf16Index) {
  assertText(text);
  assertIndex(utf16Index, text.length, 'utf16 index');
  let unit = 0;
  let codepoint = 0;
  while (unit < utf16Index) {
    const code = text.charCodeAt(unit);
    const width = isHighSurrogate(code) && isLowSurrogate(text.charCodeAt(unit + 1)) ? 2 : 1;
    if (unit + width > utf16Index) {
      throw readerError(
        READER_ERROR_CODES.INVALID_OFFSET,
        `utf16 index ${utf16Index} splits a surrogate pair`,
        { index: utf16Index },
      );
    }
    unit += width;
    codepoint += 1;
  }
  return codepoint;
}

/**
 * Convert a codepoint index into a UTF-16 index.
 * Throws INVALID_OFFSET when the index splits a surrogate pair.
 */
export function codepointToUtf16Index(text, codepointIndex) {
  assertText(text);
  if (!Number.isInteger(codepointIndex) || codepointIndex < 0) {
    throw readerError(
      READER_ERROR_CODES.INVALID_OFFSET,
      `codepoint index ${String(codepointIndex)} is invalid`,
      { index: codepointIndex },
    );
  }
  let unit = 0;
  let codepoint = 0;
  while (codepoint < codepointIndex) {
    const code = text.charCodeAt(unit);
    const width = isHighSurrogate(code) && isLowSurrogate(text.charCodeAt(unit + 1)) ? 2 : 1;
    if (unit + width > text.length) {
      throw readerError(
        READER_ERROR_CODES.INVALID_OFFSET,
        `codepoint index ${codepointIndex} splits a surrogate pair`,
        { index: codepointIndex },
      );
    }
    unit += width;
    codepoint += 1;
  }
  return unit;
}

/** Convert a UTF-16 `[start, end)` range into a codepoint range. */
export function utf16ToCodepointRange(text, start, end) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || end < start) {
    throw readerError(READER_ERROR_CODES.INVALID_OFFSET, 'A UTF-16 range requires start <= end', { start, end });
  }
  return {
    start: utf16ToCodepointIndex(text, start),
    end: utf16ToCodepointIndex(text, end),
  };
}

/** Convert a codepoint `[start, end)` range into a UTF-16 range. */
export function codepointToUtf16Range(text, start, end) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || end < start) {
    throw readerError(READER_ERROR_CODES.INVALID_OFFSET, 'A codepoint range requires start <= end', { start, end });
  }
  return {
    start: codepointToUtf16Index(text, start),
    end: codepointToUtf16Index(text, end),
  };
}

/** Slice by codepoint offsets; never returns a half-surrogate. */
export function sliceCodepoints(text, start, end) {
  assertText(text);
  const units = codepointToUtf16Range(text, start, end);
  return text.slice(units.start, units.end);
}

/**
 * Move a UTF-16 range between units, always resolving boundaries on codepoint
 * edges. `unit` is the unit of the incoming range; the result reports both.
 */
export function convertRange(text, range, unit) {
  if (!range || !Number.isInteger(range.start) || !Number.isInteger(range.end) || range.end < range.start) {
    throw readerError(READER_ERROR_CODES.INVALID_OFFSET, 'A range requires integer start <= end', { range });
  }
  if (unit === 'utf16') {
    const codepoint = utf16ToCodepointRange(text, range.start, range.end);
    return { utf16: { start: range.start, end: range.end }, codepoint, unit: 'utf16' };
  }
  if (unit === 'codepoint') {
    const utf16 = codepointToUtf16Range(text, range.start, range.end);
    return { utf16, codepoint: { start: range.start, end: range.end }, unit: 'codepoint' };
  }
  throw readerError(READER_ERROR_CODES.INVALID_OFFSET, `Unknown offset unit ${String(unit)}`, { unit });
}