import test from 'node:test';
import assert from 'node:assert/strict';
import { mountInvitationEntry } from '../assets/invitation-entry.js';

// 轻量 DOM fake：只需要组件用到的那一小部分能力，不引入任何第三方依赖。
class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.listeners = {};
    this.value = '';
    this.textContent = '';
    this.disabled = false;
    this.required = false;
    this.hidden = false;
    this.focused = false;
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  removeEventListener(name, listener) {
    const list = this.listeners[name];
    if (!list) return;
    const index = list.indexOf(listener);
    if (index >= 0) list.splice(index, 1);
  }
  focus() { this.focused = true; }
  dispatch(name, event = {}) {
    for (const listener of [...(this.listeners[name] || [])]) listener(event);
  }
  _find(predicate) {
    for (const child of this.children) {
      if (predicate(child)) return child;
      if (typeof child._find === 'function') {
        const found = child._find(predicate);
        if (found) return found;
      }
    }
    return null;
  }
  _all(predicate, out = []) {
    for (const child of this.children) {
      if (predicate(child)) out.push(child);
      if (typeof child._all === 'function') child._all(predicate, out);
    }
    return out;
  }
}

class FakeDocument {
  createElement(tag) { return new FakeElement(tag); }
}

const isTag = tag => node => node.tagName === tag;
const findInput = c => c._find(isTag('INPUT'));
const findSubmit = c => c._all(isTag('BUTTON')).find(b => b.className !== 'tutorlm-invitation__retry');
const findRetry = c => c._all(isTag('BUTTON')).find(b => b.className === 'tutorlm-invitation__retry');
const findLabel = c => c._find(isTag('LABEL'));
const typeOf = (node, cls) => node && node.className === cls;
const findByClass = (c, cls) => c._find(n => typeOf(n, cls));
const form = c => c.children[0];

function jsonResponse(status, payload) {
  return { ok: status >= 200 && status < 300, status, async json() { return payload; } };
}

function flush() { return new Promise(resolve => setTimeout(resolve, 0)); }

// 记录所有请求；policy 可以是对象、null（缺失字段）、或抛错的标记。
// policyFor(n) 可让策略随第 n 次访问变化（n 从 1 开始）；policy 为固定值时两者皆可。
function makeEnv({ policy = { invitation_only: false }, policyThrows = false, policyFor = null, routes = {} } = {}) {
  const calls = [];
  const errors = [];
  let uuidCounter = 0;
  const env = {
    document: new FakeDocument(),
    console: { error: (...args) => errors.push(args) },
    crypto: { randomUUID: () => `uuid-${++uuidCounter}` },
    AbortController: class {
      constructor() { this.signal = { aborted: false }; }
      abort() { this.signal.aborted = true; }
    },
    async fetch(url, options) {
      const body = options.body ? JSON.parse(options.body) : undefined;
      const headers = options.headers || {};
      calls.push({ url, options, body, headers });
      if (url === '/control/access-policy') {
        if (policyThrows) throw new TypeError('offline');
        const policyCalls = calls.filter(c => c.url === '/control/access-policy').length;
        const current = policyFor ? policyFor(policyCalls) : policy;
        return jsonResponse(200, current === null ? {} : current);
      }
      const handler = routes[url];
      if (handler) return handler({ body, headers, calls });
      if (url === '/control/workspace-imports') {
        return jsonResponse(201, { workspace: { workspace_id: 'ws-imported' } });
      }
      return jsonResponse(201, { lease: { lease_id: 'lease-1' } });
    },
  };
  return { env, calls, errors, nextUUID: () => `uuid-${uuidCounter}` };
}

const imports = calls => calls.filter(c => c.url === '/control/workspace-imports');
const leases = calls => calls.filter(c => c.url === '/control/leases');

test('loads policy and enables optional empty submission when not invitation-only', async () => {
  const { env, calls } = makeEnv({ policy: { invitation_only: false } });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  const result = await handle.ready;
  assert.equal(result.ok, true);
  assert.equal(result.invitationOnly, false);
  assert.equal(findInput(container).disabled, false);
  assert.equal(findSubmit(container).disabled, false);
  assert.equal(findInput(container).required, false);
  assert.equal(calls[0].url, '/control/access-policy');
  assert.equal(calls[0].options.credentials, 'same-origin');
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.deepEqual(leases(calls)[0].body, {});
  handle.dispose();
});

test('a missing or failed policy never fails open and exposes a retry button', async () => {
  const missing = makeEnv({ policy: null });
  const c1 = new FakeElement('div');
  const h1 = mountInvitationEntry(c1, { environment: missing.env });
  assert.equal((await h1.ready).ok, false);
  assert.equal(findInput(c1).disabled, true);
  assert.equal(findRetry(c1).hidden, false);
  h1.dispose();

  const failing = makeEnv({ policyThrows: true });
  const c2 = new FakeElement('div');
  const h2 = mountInvitationEntry(c2, { environment: failing.env });
  assert.equal((await h2.ready).ok, false);
  assert.equal(findInput(c2).disabled, true);
  assert.equal(findRetry(c2).hidden, false);
  h2.dispose();
});

test('refreshPolicy is public, retryable, and clears the retry button on success', async () => {
  let ok = false;
  const { env } = makeEnv({ routes: {} });
  const original = env.fetch;
  env.fetch = (url, options) => {
    if (url === '/control/access-policy' && !ok) return Promise.reject(new TypeError('offline'));
    return original(url, options);
  };
  const container = new FakeElement('div');
  const readyStates = [];
  const handle = mountInvitationEntry(container, { environment: env, onReady: s => readyStates.push(s) });
  assert.equal((await handle.ready).ok, false);
  assert.equal(findRetry(container).hidden, false);
  ok = true;
  const refreshed = await handle.refreshPolicy();
  assert.equal(refreshed.ok, true);
  assert.equal(refreshed.enabled, true);
  assert.equal(findInput(container).disabled, false);
  assert.equal(findRetry(container).hidden, true);
  assert.equal(readyStates.length, 2, 'onReady fires on initial load and on refresh');
  handle.dispose();
});

test('opts.workspaceId opens an existing project without a code when not invitation-only', async () => {
  const { env, calls } = makeEnv({ policy: { invitation_only: false } });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env, workspaceId: 'ws-existing' });
  await handle.ready;
  assert.equal(findInput(container).required, false, 'open mode does not require a code');
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.deepEqual(leases(calls)[0].body, { workspace_id: 'ws-existing' });
  assert.equal(imports(calls).length, 0);
  handle.dispose();
});

test('under invitation-only, workspaceId alone is not authorization: the code stays required', async () => {
  const { env, calls } = makeEnv({ policy: { invitation_only: true } });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env, workspaceId: 'ws-existing' });
  await handle.ready;
  assert.equal(findInput(container).required, true, 'an existing workspace is not an invitation grant');
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(leases(calls).length, 0, 'an empty submit with only workspaceId is blocked');
  handle.dispose();
});

test('share code imports once, then requests a lease with the new workspace_id', async () => {
  const { env, calls } = makeEnv({ policy: { invitation_only: false } });
  const container = new FakeElement('div');
  const imported = [];
  const leased = [];
  const handle = mountInvitationEntry(container, {
    environment: env,
    onImported: w => imported.push(w),
    onLease: r => leased.push(r),
  });
  await handle.ready;
  findInput(container).value = 'TLM-SHARE-abc';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.deepEqual(imports(calls)[0].body, { invitation_code: 'TLM-SHARE-abc' });
  assert.deepEqual(leases(calls)[0].body, { workspace_id: 'ws-imported' });
  assert.equal(findInput(container).value, '');
  assert.equal(imported.length, 1);
  assert.equal(imported[0].workspace_id, 'ws-imported');
  assert.equal(leased.length, 1);
  handle.dispose();
});

test('a lease failure after a successful import retries without re-cloning the source', async () => {
  let leaseFail = true;
  const { env, calls } = makeEnv({
    policy: { invitation_only: false },
    routes: {
      '/control/leases': () => (leaseFail ? jsonResponse(503, { error: { message: 'capacity' } }) : jsonResponse(201, { lease: { lease_id: 'lease-1' } })),
    },
  });
  const container = new FakeElement('div');
  const imported = [];
  const leased = [];
  const handle = mountInvitationEntry(container, {
    environment: env,
    onImported: w => imported.push(w),
    onLease: r => leased.push(r),
  });
  await handle.ready;
  const input = findInput(container);
  input.value = 'TLM-SHARE-abc';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(imports(calls).length, 1, 'imported exactly once');
  assert.equal(imported.length, 1, 'onImported fires even though the lease failed');
  assert.equal(input.value, 'TLM-SHARE-abc', 'code preserved for retry');
  assert.equal(findSubmit(container).disabled, false, 'form re-enabled for retry');

  leaseFail = false;
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(imports(calls).length, 1, 'retry reuses the cached clone, no second import');
  assert.equal(imported.length, 1, 'onImported is not repeated for a cached clone');
  assert.equal(leased.length, 1);
  assert.deepEqual(leases(calls)[1].body, { workspace_id: 'ws-imported' });
  assert.equal(input.value, '');
  handle.dispose();
});

test('after share import, gated allocation still requires a system invitation', async () => {
  let leaseFail = true;
  const { env, calls } = makeEnv({
    policy: { invitation_only: true },
    routes: {
      '/control/leases': () => (leaseFail ? jsonResponse(503, { error: { message: 'capacity' } }) : jsonResponse(201, { lease: { lease_id: 'lease-1' } })),
    },
  });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  const input = findInput(container);
  input.value = 'TLM-SHARE-abc';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(findInput(container).required, true, 'sharing does not relax invitation-only');
  input.value = '';                       // user clears the code before retrying
  leaseFail = false;
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(imports(calls).length, 1, 'no re-import');
  assert.equal(leases(calls).length, 0, 'empty retry cannot allocate');
  input.value = 'TLM-SYS-test';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.deepEqual(leases(calls).at(-1).body, { workspace_id: 'ws-imported', invitation_code: 'TLM-SYS-test' });
  handle.dispose();
});

test('without Web Crypto the import fails locally with an explicit unsupported error', async () => {
  const { env, calls } = makeEnv({ policy: { invitation_only: true } });
  delete env.crypto;                      // no Web Crypto available
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  findInput(container).value = 'TLM-SHARE-abc';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(imports(calls).length, 0, 'no import request is attempted');
  const errorNode = findByClass(container, 'tutorlm-invitation__error');
  assert.match(errorNode.textContent, /Web Crypto/);
  handle.dispose();
});

test('Idempotency-Key never falls back to Math.random', async () => {
  const seen = [];
  const { env } = makeEnv({
    policy: { invitation_only: true },
    routes: {
      '/control/workspace-imports': ({ headers }) => {
        seen.push(headers['Idempotency-Key']);
        return jsonResponse(201, { workspace: { workspace_id: 'ws-x' } });
      },
    },
  });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  findInput(container).value = 'TLM-SHARE-abc';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(seen.length, 1);
  assert.equal(seen[0], 'uuid-1', 'the injected crypto.randomUUID value is used verbatim');
  assert.ok(!/^idem-/.test(seen[0]), 'no Math.random-based fallback key');
  handle.dispose();
});

test('the Idempotency-Key is reused within an import attempt and refreshed per new attempt', async () => {
  let importFails = true;
  const keys = [];
  const { env, calls } = makeEnv({
    policy: { invitation_only: true },
    routes: {
      '/control/workspace-imports': ({ body, headers }) => {
        keys.push(headers['Idempotency-Key']);
        if (importFails) return jsonResponse(500, { error: { message: 'boom' } });
        return jsonResponse(201, { workspace: { workspace_id: `ws-${body.invitation_code}` } });
      },
    },
  });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  const input = findInput(container);
  input.value = 'TLM-SHARE-one';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(keys.length, 1);
  assert.ok(keys[0], 'a key is attached');
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(keys.length, 2);
  assert.equal(keys[1], keys[0], 'same attempt reuses the same key');

  importFails = false;
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();

  input.value = 'TLM-SHARE-two';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.notEqual(keys.at(-1), keys[0], 'a new attempt gets a fresh key');
  handle.dispose();
});

test('re-validates policy on every submit, so a tightened policy blocks an empty submit', async () => {
  // mount 时为 false（可留空），提交时收紧为 true。
  const { env, calls } = makeEnv({ policyFor: n => ({ invitation_only: n > 1 }) });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  assert.equal(findInput(container).required, false);
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(leases(calls).length, 0, 'freshly-tightened policy blocks the empty submit');
  assert.equal(calls.filter(c => c.url === '/control/access-policy').length, 2, 'policy re-checked on submit');
  handle.dispose();
});

test('a non-share code is forwarded to the lease endpoint untouched', async () => {
  const { env, calls } = makeEnv({ policy: { invitation_only: true } });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  findInput(container).value = 'TLM-SYS-xyz';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(imports(calls).length, 0);
  assert.deepEqual(leases(calls)[0].body, { invitation_code: 'TLM-SYS-xyz' });
  handle.dispose();
});

test('a consumer callback throwing is surfaced via onError and never mislabelled as an API failure', async () => {
  const { env } = makeEnv({ policy: { invitation_only: false } });
  const container = new FakeElement('div');
  const surfaced = [];
  const handle = mountInvitationEntry(container, {
    environment: env,
    onLease() { throw new Error('consumer exploded'); },
    onError: (error, context) => surfaced.push({ error, context }),
  });
  await handle.ready;
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(surfaced.length, 1);
  assert.equal(surfaced[0].context, 'onLease');
  assert.match(surfaced[0].error.message, /consumer exploded/);
  const errorNode = findByClass(container, 'tutorlm-invitation__error');
  assert.equal(errorNode.textContent, '', 'a callback failure does not show an API error');
  const statusNode = findByClass(container, 'tutorlm-invitation__status');
  assert.equal(statusNode.textContent, '已取得计算资源。', 'the lease result is still reported');
  handle.dispose();
});

test('a callback throwing falls back to console.error when onError is absent', async () => {
  const { env, errors } = makeEnv({ policy: { invitation_only: false } });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, {
    environment: env,
    onImported() { throw new Error('nope'); },
  });
  await handle.ready;
  findInput(container).value = 'TLM-SHARE-abc';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(errors.length, 1);
  handle.dispose();
});

test('multiple mounts get unique DOM ids so label/aria associations stay correct', async () => {
  const { env } = makeEnv({ policy: { invitation_only: false } });
  const a = new FakeElement('div');
  const b = new FakeElement('div');
  const ha = mountInvitationEntry(a, { environment: env });
  const hb = mountInvitationEntry(b, { environment: env });
  await Promise.all([ha.ready, hb.ready]);
  const inputA = findInput(a);
  const inputB = findInput(b);
  assert.notEqual(inputA.id, inputB.id);
  assert.equal(inputA.getAttribute('aria-describedby'), inputA.id.replace('-code', '-hint') + ' ' + inputA.id.replace('-code', '-status') + ' ' + inputA.id.replace('-code', '-error'));
  assert.equal(findLabel(a).getAttribute('for'), inputA.id);
  assert.equal(findLabel(b).getAttribute('for'), inputB.id);
  ha.dispose();
  hb.dispose();
});

test('the code input is capped at the backend length limit', async () => {
  const { env } = makeEnv({ policy: { invitation_only: false } });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  const input = findInput(container);
  assert.equal(input.maxLength, 256);
  assert.equal(input.getAttribute('maxlength'), '256');
  handle.dispose();
});

test('the submit controls are disabled while a request is in flight', async () => {
  let releaseLease;
  const { env } = makeEnv({ policy: { invitation_only: false } });
  const original = env.fetch;
  env.fetch = (url, options) => {
    if (url === '/control/leases') {
      return new Promise(resolve => { releaseLease = () => resolve(jsonResponse(201, { lease: { lease_id: 'lease-1' } })); });
    }
    return original(url, options);
  };
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  assert.equal(findSubmit(container).disabled, true);
  assert.equal(findInput(container).disabled, true);
  releaseLease();
  await flush();
  assert.equal(findSubmit(container).disabled, false);
  handle.dispose();
});

test('dispose aborts in-flight work and detaches the form', async () => {
  const env = { document: new FakeDocument(), fetch: () => new Promise(() => {}) };
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  const mountedForm = form(container);
  assert.equal(container.children.length, 1);
  assert.equal(mountedForm.listeners.submit.length, 1);
  handle.dispose();
  assert.equal(container.children.length, 0, 'form detached from the container');
  assert.equal(mountedForm.listeners.submit.length, 0, 'listeners removed on dispose');
  assert.equal(mountedForm.parentNode, null);
});

test('server strings are never written as HTML', async () => {
  const { env } = makeEnv({
    policy: { invitation_only: true },
    routes: {
      '/control/leases': () => ({ ok: false, status: 422, async json() { return { error: { message: '<img src=x onerror=alert(1)>' } }; } }),
    },
  });
  const container = new FakeElement('div');
  const handle = mountInvitationEntry(container, { environment: env });
  await handle.ready;
  findInput(container).value = 'TLM-SYS-xyz';
  form(container).dispatch('submit', { preventDefault() {} });
  await flush();
  const errorNode = findByClass(container, 'tutorlm-invitation__error');
  assert.equal(errorNode.textContent, '<img src=x onerror=alert(1)>');
  assert.equal('innerHTML' in errorNode, false, 'only textContent is used');
  handle.dispose();
});

test('rejects a non-element container', () => {
  assert.throws(() => mountInvitationEntry(null, {}), TypeError);
});


