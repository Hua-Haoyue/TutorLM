/**
 * Reader adapter error helpers (browser-side ES module, no dependencies).
 *
 * Every failure a reader adapter reports carries a stable `code`, so the future
 * workspace wiring can branch on it instead of matching message text. Codes that
 * the backend contract already names are mirrored verbatim
 * (`tex_compile_unavailable`), so adapters never invent a second vocabulary.
 */

export const READER_ERROR_CODES = Object.freeze({
  // Injected PDF engine problems (no engine installed or engine rejected the input).
  ENGINE_UNAVAILABLE: 'pdf_engine_unavailable',
  LOAD_FAILED: 'pdf_load_failed',
  ENCRYPTED: 'pdf_encrypted',
  MALFORMED: 'pdf_malformed',
  // A page without an extractable text layer must stay explicit, never faked.
  NO_TEXT_LAYER: 'no_text_layer',
  // TeX compilation has no safe executor in this repository (see
  // docs/tex-compilation-security.md); the reader never falls back.
  TEX_COMPILE_UNAVAILABLE: 'tex_compile_unavailable',
  TEX_MAIN_INVALID: 'tex_main_invalid',
  TEX_PATH_INVALID: 'tex_path_invalid',
  TEX_SOURCE_FILE_REQUIRED: 'tex_source_file_required',
  // Lifecycle and identity problems.
  DOCUMENT_MISMATCH: 'reader_document_mismatch',
  NOT_OPEN: 'reader_not_open',
  CLOSED: 'reader_closed',
  ABORTED: 'reader_aborted',
  ABORTING: 'reader_aborting',
  // Contract violations.
  INVALID_SELECTOR: 'invalid_selector',
  INVALID_REFERENCE: 'invalid_reference',
  INVALID_OFFSET: 'invalid_offset',
  INVALID_ARGUMENT: 'invalid_argument',
  UNSUPPORTED_FORMAT: 'unsupported_format',
});

/**
 * Build an error carrying a reader error code.
 * @param {string} code one of READER_ERROR_CODES
 * @param {string} [message]
 * @param {object} [details]
 */
export function readerError(code, message, details) {
  const error = new Error(message || code);
  error.name = 'ReaderError';
  error.code = code;
  if (details !== undefined) error.details = details;
  return error;
}

/** True when `error` is a reader error, optionally of a specific code. */
export function isReaderError(error, code) {
  if (!error || typeof error !== 'object' || error.name !== 'ReaderError') return false;
  return code === undefined || error.code === code;
}

/** Wrap an unknown throwable without losing the original cause. */
export function toReaderError(error, code, message, details) {
  if (isReaderError(error)) return error;
  const wrapped = readerError(code, message || (error && error.message));
  wrapped.cause = error;
  if (details !== undefined) wrapped.details = details;
  return wrapped;
}