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
  const WORKSPACE = 'tlm.workspace';
  const RECOVERY = 'tlm.recovery';
  const IMPORT = 'tlm.share-import';
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
  async function requestLease(invitationCode = '', workspaceId = '') {
    const value = await post('/control/leases', {
      ...(workspaceId ? { workspace_id: workspaceId } : {}),
      ...(invitationCode.trim() ? { invitation_code: invitationCode.trim() } : {}),
    }, { auth: false });
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
      if (!managed && !inviteCode && runtime.mode === 'demo') throw new ApiError(401, { error: { message: '请输入应用体验码', code: 'invite_required' } });
      const issued = managed
        ? await post('/control/app-session', {}, { auth: false })
        : await post('/api/sessions', inviteCode ? { invite_code: inviteCode } : {}, { auth: false });
      sessionStorage.setItem(TOKEN, issued.access_token);
      await loadSession();
    }
    return runtime;
  }
  /* Full entry: lease (managed only) → wait for instance → runtime + app session. */
  async function enter(inviteCode = '', { onStep = () => {}, signal, shareCode = '', workspaceId = '' } = {}) {
    const checkpoint = () => { if (signal?.aborted) throw new DOMException('aborted', 'AbortError'); };
    checkpoint();
    if (await detect()) {
      const policy = await accessPolicy();
      if (policy.invitation_only && !inviteCode.trim()) {
        throw new ApiError(401, { error: { code: 'invitation_required', message: '请输入系统邀请码' } });
      }
      await ensureUser();
      let target = workspaceId;
      if (shareCode.trim()) target = (await redeemShare(shareCode.trim())).workspace.workspace_id;
      if (!target) target = sessionStorage.getItem(WORKSPACE);
      if (target) {
        try { await get(`/control/workspaces/${part(target)}`, { auth: false }); }
        catch (error) {
          if (![404, 410].includes(error.status)) throw error;
          sessionStorage.removeItem(WORKSPACE); target = '';
        }
      }
      if (!target) {
        const items = (await listWorkspaces()).items;
        target = items[0]?.workspace_id || (await createWorkspace('我的工作区')).workspace.workspace_id;
      }
      sessionStorage.setItem(WORKSPACE, target);
      checkpoint();
      let previous;
      try { previous = (await currentLease()).lease; }
      catch (error) { if (![401, 404, 410].includes(error.status)) throw error; }
      if (previous && previous.workspace_id !== target) await release();
      onStep('alloc');
      await requestLease(inviteCode, target);
      if (signal?.aborted) { await release(); checkpoint(); }
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
    if (!(await detect())) throw new ApiError(409, { message: '独立实例不支持项目分享，请使用控制面入口' });
    await ensureUser();
    let pending;
    try { pending = JSON.parse(sessionStorage.getItem(IMPORT)); } catch { /* fresh import */ }
    if (pending?.code !== code) pending = { code, key: requestKey() };
    sessionStorage.setItem(IMPORT, JSON.stringify(pending));
    const value = await post('/control/workspace-imports', { invitation_code: code },
      { auth: false, headers: { 'Idempotency-Key': pending.key } });
    sessionStorage.setItem(WORKSPACE, value.workspace.workspace_id);
    return value;
  }
  const accessPolicy = () => get('/control/access-policy', { auth: false });
  const listWorkspaces = () => get('/control/workspaces', { auth: false });
  const createWorkspace = (title) => post('/control/workspaces', { title }, { auth: false });
  async function ensureUser() {
    try { return await get('/control/users/me', { auth: false }); }
    catch (error) { if (error.status !== 401) throw error; }
    // Registration detaches anonymous compute. Never silently discard an open temporary project.
    try {
      const existing = await currentLease();
      if (!existing.lease.workspace_id) throw new ApiError(409, { message: '当前是临时工作区，请先导出内容并结束试用，再创建持久项目' });
    } catch (error) { if (![401, 404, 410].includes(error.status)) throw error; }
    const result = await post('/control/users', {}, { auth: false });
    sessionStorage.setItem(RECOVERY, JSON.stringify(result));
    return result;
  }
  async function login(userId, recoveryToken) {
    const result = await post('/control/users/session', { user_id: userId, recovery_token: recoveryToken }, { auth: false });
    sessionStorage.removeItem(WORKSPACE); sessionStorage.removeItem(TOKEN); sessionStorage.removeItem(BINDING);
    lease = null; session = null;
    return result;
  }
  async function createShare() {
    if (!(await detect())) throw new ApiError(409, { message: '独立实例不支持持久项目分享' });
    await currentLease();
    if (!lease.workspace_id) throw new ApiError(409, { message: '当前是临时工作区，请先导出内容并保存到持久项目后再分享' });
    return post(`/control/workspaces/${part(lease.workspace_id)}/invitations`, {}, { auth: false });
  }
  async function revokeShare(id) {
    if (!lease?.workspace_id) throw new ApiError(409, { message: '没有持久项目' });
    return request(`/control/workspaces/${part(lease.workspace_id)}/invitations/${part(id)}`, { method: 'DELETE', auth: false });
  }
  async function release() {
    try {
      if (sessionStorage.getItem(TOKEN)) {
        try { await del('/api/sessions/me'); }
        catch (error) { if (![401, 410].includes(error.status)) throw error; }
      }
    }
    finally {
      try {
        if (managed) {
          try { await request('/control/leases/current', { method: 'DELETE', auth: false }); }
          catch (error) { if (![401, 410].includes(error.status)) throw error; }
        }
      }
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
    accessPolicy, ensureUser, login, listWorkspaces, createWorkspace, createShare, revokeShare,
    get recovery() { try { return JSON.parse(sessionStorage.getItem(RECOVERY)); } catch { return null; } },
    takePendingProject: () => { const v = sessionStorage.getItem(PENDING); if (v) sessionStorage.removeItem(PENDING); return v; },
    cancel: (id) => post(`/api/runs/${part(id)}/cancel`, {}),
    hasToken: () => !!sessionStorage.getItem(TOKEN),
    get runtime() { return runtime; }, get session() { return session; }, get limits() { return limits; },
    get lease() { return lease; }, get managed() { return managed; },
  };
})();
