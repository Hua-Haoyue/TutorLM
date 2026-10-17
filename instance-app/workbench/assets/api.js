/* Real HTTP transport. Only the app access token is kept in sessionStorage.
   Deployment modes:
     standalone/local  – no lease, no session (trusted loopback)
     standalone/demo   – app session issued by invite code → Bearer
     managed           – control-plane lease (HttpOnly cookie) + app Bearer */
(function () {
  'use strict';
  const TOKEN = 'tlm.app-token';
  const BINDING = 'tlm.lease-binding';
  const PENDING = 'tlm.pending-project';
  let runtime, session, limits, lease, managed = null;

  class ApiError extends Error {
    constructor(status, body, retryAfter) {
      const err = body?.error || {};
      super(err.message || body?.message || (typeof body?.detail === 'string' ? body.detail : '') || `HTTP ${status}`);
      this.status = status;
      this.code = err.code || body?.code || '';
      this.details = err.details || {};
      this.retryAfter = retryAfter || 0;
    }
  }

  async function request(path, { method = 'GET', body, signal, headers = {}, auth = true } = {}) {
    const token = sessionStorage.getItem(TOKEN);
    let response;
    try {
      response = await fetch(path, {
        method, signal, credentials: 'same-origin', cache: 'no-store',
        headers: {
          ...(body !== undefined && !(body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
          ...(auth && token ? { Authorization: `Bearer ${token}` } : {}), ...headers,
        },
        body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new ApiError(0, { message: '无法连接服务器，请检查网络' });
    }
    const value = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, value, Number(response.headers?.get?.('Retry-After')) || 0);
    return value;
  }
  /* Binary body (e.g. a PDF original from /source). The Bearer travels in a header, never the URL. */
  async function bytes(path, { signal } = {}) {
    const token = sessionStorage.getItem(TOKEN);
    let response;
    try {
      response = await fetch(path, { signal, credentials: 'same-origin', cache: 'no-store', headers: token ? { Authorization: `Bearer ${token}` } : {} });
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new ApiError(0, { message: '无法连接服务器，请检查网络' });
    }
    if (!response.ok) throw new ApiError(response.status, await response.json().catch(() => ({})), Number(response.headers?.get?.('Retry-After')) || 0);
    return response.arrayBuffer();
  }
  const get = (path, options) => request(path, options);
  const post = (path, body, options) => request(path, { ...options, method: 'POST', body });
  const patch = (path, body) => request(path, { method: 'PATCH', body });
  const put = (path, body) => request(path, { method: 'PUT', body });
  const del = (path) => request(path, { method: 'DELETE' });
  const part = (id) => encodeURIComponent(id);
  const wait = (ms, signal) => new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('aborted', 'AbortError')); }, { once: true });
  });

  /* Only an explicit 404 on /control/health means "no control plane in front of us". */
  async function detect() {
    if (managed !== null) return managed;
    try { await get('/control/health', { auth: false }); managed = true; }
    catch (error) { if (error.status !== 404) throw error; managed = false; }
    return managed;
  }
  function bindLease(value) {
    const binding = `${value.lease.lease_id}:${value.lease.generation}`;
    if (sessionStorage.getItem(BINDING) !== binding) sessionStorage.removeItem(TOKEN);
    sessionStorage.setItem(BINDING, binding);
    lease = value.lease;
    return value;
  }
  async function currentLease() {
    return bindLease(await get('/control/leases/current', { auth: false }));
  }
  /* POST /control/leases (idempotent with a valid cookie). 503 carries Retry-After. */
  async function requestLease() {
    const value = await post('/control/leases', {}, { auth: false });
    // The lease token in the body is never stored: the HttpOnly cookie is the credential.
    return value?.lease ? bindLease(value) : currentLease();
  }
  /* A fresh lease is 'pending' while its container boots; poll until 'active'. */
  async function waitLeaseActive({ timeoutMs = 90000, onState = () => {}, signal } = {}) {
    const started = Date.now();
    for (;;) {
      const value = await currentLease();
      onState(value.lease.state);
      if (!value.lease.state || value.lease.state === 'active') return value;
      if (['expired', 'revoked', 'reclaimed'].includes(value.lease.state)) throw new ApiError(410, { message: '试用已结束，请重新申请' });
      if (Date.now() - started > timeoutMs) throw new ApiError(504, { message: '工作区启动超时，请稍后重试' });
      await wait(1000, signal);
    }
  }

  async function loadSession() {
    const me = await get('/api/sessions/me');
    session = me.session; limits = me.limits || me.session?.limits || null;
    return session;
  }
  /* Runtime discovery + app session. Every business route needs the app Bearer,
     whatever runtime.mode says; only how it is issued differs (invite code, or an
     implicit session the server opted into). Throws 401 code=invite_required
     when the server wants an invite and none was given. */
  async function connect(inviteCode) {
    await detect();
    if (managed && !lease) await currentLease();
    runtime = await get('/api/runtime', { auth: false });
    session = null; limits = null;
    if (sessionStorage.getItem(TOKEN)) {
      try { await loadSession(); }
      catch (error) {
        if (![401, 410].includes(error.status)) throw error;
        sessionStorage.removeItem(TOKEN);
      }
    }
    if (!session) {
      if (!inviteCode && runtime.mode === 'demo') throw new ApiError(401, { error: { message: '请输入应用体验码', code: 'invite_required' } });
      const issued = await post('/api/sessions', inviteCode ? { invite_code: inviteCode } : {}, { auth: false });
      sessionStorage.setItem(TOKEN, issued.access_token);
      await loadSession();
    }
    return runtime;
  }
  /* Full entry: lease (managed only) → wait for instance → runtime + app session. */
  async function enter(inviteCode, { onStep = () => {}, signal } = {}) {
    if (await detect()) {
      onStep('alloc');
      await requestLease();
      onStep('start');
      await waitLeaseActive({ signal, onState: (s) => onStep('start', s) });
    }
    onStep('ready');
    return connect(inviteCode);
  }
  /* Redeem a share code: the server deep-copies the shared workspace into a new
     project owned by the caller and returns its id. We remember that id so the
     workspace opens it once instead of the visitor's own empty workspace. */
  async function redeemShare(code) {
    const value = await post(`/api/shares/${part(code)}/redeem`, {}, { auth: !!sessionStorage.getItem(TOKEN) });
    const projectId = value?.project_id || value?.project?.id || value?.id || '';
    if (projectId) sessionStorage.setItem(PENDING, projectId);
    return value;
  }
  async function release() {
    try { if (sessionStorage.getItem(TOKEN)) await del('/api/sessions/me'); }
    finally {
      try { if (managed) await request('/control/leases/current', { method: 'DELETE', auth: false }); }
      finally {
        sessionStorage.removeItem(TOKEN); sessionStorage.removeItem(BINDING);
        session = null; lease = null;
      }
    }
  }
  /* Seconds until the earliest of lease expiry / session expiry; null when unbounded (local). */
  function secondsLeft() {
    const ends = [];
    if (lease?.expires_at) ends.push(Number(lease.expires_at) * 1000);
    if (session?.expires_at) ends.push(Date.parse(session.expires_at));
    return ends.length ? (Math.min(...ends) - Date.now()) / 1000 : null;
  }

  const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
  // randomUUID requires HTTPS; getRandomValues is also available on the current
  // HTTP trial entry. The idempotency key is not an authentication credential.
  function requestKey() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('');
  }
  /* Submit a Host run and poll it to a terminal state (the server does not stream deltas). */
  async function run(kind, input, { onRun = () => {}, signal, key = requestKey() } = {}) {
    let value = await post('/api/runs', { kind, input }, { signal, headers: { 'Idempotency-Key': key } });
    onRun(value);
    return follow(value, { onRun, signal });
  }
  async function follow(value, { onRun = () => {}, signal } = {}) {
    let delay = 250;
    while (!TERMINAL.has(value.state)) {
      await wait(delay, signal);
      delay = Math.min(1000, delay * 1.4);
      value = await get(`/api/runs/${part(value.id)}`, { signal });
      onRun(value);
    }
    if (value.state !== 'completed') {
      const msg = value.state === 'cancelled' ? '已取消' : (value.error?.message || value.error_message || `任务${value.state}`);
      throw Object.assign(new ApiError(409, { error: { message: msg, code: value.error_code || value.state } }), { run: value });
    }
    return value.result;
  }

  window.RuntimeAPI = {
    ApiError, TERMINAL, get, post, patch, put, del, part, bytes, run, follow, connect, enter, release, detect,
    currentLease, requestLease, waitLeaseActive, secondsLeft, loadSession, redeemShare,
    takePendingProject: () => { const v = sessionStorage.getItem(PENDING); if (v) sessionStorage.removeItem(PENDING); return v; },
    cancel: (id) => post(`/api/runs/${part(id)}/cancel`, {}),
    hasToken: () => !!sessionStorage.getItem(TOKEN),
    get runtime() { return runtime; }, get session() { return session; }, get limits() { return limits; },
    get lease() { return lease; }, get managed() { return managed; },
  };
})();
