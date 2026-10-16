/**
 * Minimal synchronous event emitter shared by the reader adapters.
 *
 * The adapter contract in docs/document-readers.md emits `selection`,
 * `reference`, `viewchange` and `error`; `on()` returns an unsubscribe function.
 * A throwing consumer callback must never corrupt reader state, so listener
 * failures are contained here.
 */

export function createEmitter(events) {
  const allowed = new Set(events);
  const listeners = new Map([...allowed].map(name => [name, new Set()]));

  function assertEvent(name) {
    if (!allowed.has(name)) throw new TypeError(`Unknown reader event: ${String(name)}`);
  }

  return {
    on(name, listener) {
      assertEvent(name);
      if (typeof listener !== 'function') throw new TypeError('A listener function is required');
      listeners.get(name).add(listener);
      return () => listeners.get(name).delete(listener);
    },
    emit(name, payload) {
      assertEvent(name);
      for (const listener of [...listeners.get(name)]) {
        try {
          listener(payload);
        } catch {
          // A consumer callback failure is not a reader failure.
        }
      }
    },
    listenerCount(name) {
      assertEvent(name);
      return listeners.get(name).size;
    },
    clearListeners() {
      for (const set of listeners.values()) set.clear();
    },
  };
}