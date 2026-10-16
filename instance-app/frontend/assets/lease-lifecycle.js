const TERMINAL = new Set([401, 404, 409, 410]);

function documentUUID(crypto) {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function attachLeaseLifecycle(lease, {
  environment = globalThis,
  onLeaseLost = () => {},
} = {}) {
  if (!lease?.lease_id || !Number.isFinite(lease.expires_at)) {
    throw new TypeError('An issued lease with an absolute expires_at is required');
  }
  const env = environment;
  const base = `/control/leases/${encodeURIComponent(lease.lease_id)}/pages`;
  let pageId, active = false, disposed = false, lost = false;
  let heartbeatTimer, deadlineTimer, intervalMs = 20000;

  function clearHeartbeat() {
    env.clearTimeout(heartbeatTimer);
    heartbeatTimer = undefined;
  }

  function lose(error) {
    if (disposed || lost) return;
    lost = true;
    active = false;
    clearHeartbeat();
    env.clearTimeout(deadlineTimer);
    onLeaseLost(error);
  }

  async function post(url, body) {
    const response = await env.fetch(url, {
      method: 'POST', credentials: 'same-origin',
      ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) {
      const error = new Error('Compute lease presence request failed');
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function current(id) {
    return active && !disposed && !lost && id === pageId;
  }

  function schedule(id, connect = false) {
    clearHeartbeat();
    if (current(id)) {
      heartbeatTimer = env.setTimeout(() => {
        if (connect) return register(id).catch(() => {});
        return heartbeat(id);
      }, intervalMs);
    }
  }

  async function register(id) {
    try {
      const result = await post(base, { page_id: id });
      if (current(id)) {
        if (Number.isFinite(result.heartbeat_interval_ms) && result.heartbeat_interval_ms > 0) {
          intervalMs = Math.min(20000, result.heartbeat_interval_ms);
        }
        schedule(id);
      }
    } catch (error) {
      if (current(id)) {
        if (TERMINAL.has(error.status)) lose(error);
        else schedule(id, true);
      }
      throw error;
    }
  }

  async function heartbeat(id) {
    if (!current(id)) return;
    try {
      await post(`${base}/${encodeURIComponent(id)}/heartbeat`);
    } catch (error) {
      if (current(id) && TERMINAL.has(error.status)) lose(error);
    } finally {
      // Heartbeats only maintain page presence, never the absolute lease deadline.
      if (current(id)) schedule(id);
    }
  }

  function close() {
    if (!active) return;
    const url = `${base}/${encodeURIComponent(pageId)}/close`;
    active = false;
    clearHeartbeat();
    let queued = false;
    try { queued = env.navigator.sendBeacon(url); } catch { /* use keepalive below */ }
    if (!queued) {
      try {
        Promise.resolve(env.fetch(url, {
          method: 'POST', credentials: 'same-origin', keepalive: true,
        })).catch(() => {});
      } catch { /* server-side presence timeout is the final fallback */ }
    }
  }

  function begin() {
    if (env.Date.now() >= lease.expires_at * 1000) {
      const error = new Error('Compute lease expired');
      error.status = 401;
      lose(error);
      return Promise.resolve();
    }
    pageId = documentUUID(env.crypto);
    active = true;
    return register(pageId);
  }

  function show() {
    if (!active && !disposed && !lost) begin().catch(() => {});
  }

  env.addEventListener('pagehide', close);
  env.addEventListener('pageshow', show);
  deadlineTimer = env.setTimeout(() => {
    const error = new Error('Compute lease expired');
    error.status = 401;
    lose(error);
  }, Math.max(0, lease.expires_at * 1000 - env.Date.now()));

  const ready = begin();
  return {
    ready,
    dispose() {
      if (disposed) return;
      close();
      disposed = true;
      clearHeartbeat();
      env.clearTimeout(deadlineTimer);
      env.removeEventListener('pagehide', close);
      env.removeEventListener('pageshow', show);
    },
  };
}
