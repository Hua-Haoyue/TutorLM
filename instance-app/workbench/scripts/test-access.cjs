const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function fixture({ gated = false, full = false, policyFailure = false } = {}) {
  const calls = [], storage = new Map();
  let lease = null;
  const context = {
    window: {}, FormData: class {}, AbortController, DOMException, Date,
    crypto: webcrypto, setTimeout, clearTimeout, console,
    sessionStorage: { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k) },
    async fetch(url, opts) {
      const body = opts.body ? JSON.parse(opts.body) : null;
      calls.push({ url, body, ...opts });
      let status = 200, data = {};
      if (url === '/control/health') data = { status: 'ok' };
      else if (url === '/control/access-policy') { status = policyFailure ? 503 : 200; data = { invitation_only: gated }; }
      else if (url === '/control/users/me') data = { user: { user_id: 'owner' } };
      else if (url === '/control/workspaces') data = { items: [{ workspace_id: 'workspace-1' }] };
      else if (url.startsWith('/control/workspaces/') && url.endsWith('/invitations')) data = { code: 'unique-share-string', invitation: { invitation_id: 'inv-1' } };
      else if (url.startsWith('/control/workspaces/')) data = { workspace: { workspace_id: url.split('/')[3] } };
      else if (url === '/control/workspace-imports') data = { workspace: { workspace_id: 'independent-copy' } };
      else if (url === '/control/leases/current' && opts.method === 'DELETE') { lease = null; status = 202; }
      else if (url === '/control/leases/current') { status = lease ? 200 : 401; data = { lease }; }
      else if (url === '/control/leases') {
        if (full) { status = 503; data = { error: { code: 'no_capacity' } }; }
        else { lease = { workspace_id: body.workspace_id, state: 'active', generation: 1, lease_id: 'lease-1' }; data = { lease }; }
      } else if (url === '/api/runtime') data = { mode: 'managed', plugins: [] };
      else if (url === '/control/app-session') data = { access_token: 'app-bearer', session: { workspace_id: lease.workspace_id } };
      else if (url === '/api/sessions/me') data = { session: { workspace_id: lease.workspace_id }, limits: {} };
      else throw new Error('Unexpected endpoint: ' + url);
      return { ok: status < 400, status, headers: { get: () => null }, json: async () => data };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets/api.js'), 'utf8'), context);
  return { api: context.window.RuntimeAPI, calls, storage };
}

test('open mode allocates durable workspace without a code and gets app Bearer through control plane', async () => {
  const { api, calls } = fixture();
  await api.enter('');
  const allocated = calls.find(x => x.url === '/control/leases');
  assert.deepEqual(JSON.parse(allocated.body), { workspace_id: 'workspace-1' });
  assert.ok(calls.some(x => x.url === '/control/app-session'));
  assert.ok(!calls.some(x => x.url === '/api/sessions'));
});

test('a system invitation is forwarded to slot allocation', async () => {
  const { api, calls } = fixture({ gated: true });
  await api.enter('TLM-SYS-v1-test');
  assert.equal(JSON.parse(calls.find(x => x.url === '/control/leases').body).invitation_code, 'TLM-SYS-v1-test');
});

test('invitation-only denies empty invitations even when a share string is supplied', async () => {
  const { api, calls } = fixture({ gated: true });
  await assert.rejects(api.enter('', { shareCode: 'share' }), error => error.code === 'invitation_required');
  assert.ok(!calls.some(x => x.url === '/control/workspace-imports' || x.url === '/control/leases'));
});

test('policy failures never fall back to open allocation', async () => {
  const { api, calls } = fixture({ policyFailure: true });
  await assert.rejects(api.enter(''));
  assert.ok(!calls.some(x => x.url === '/control/leases'));
});

test('share import copies before allocating the destination; share string is not a slot invitation', async () => {
  const { api, calls } = fixture();
  await api.enter('', { shareCode: 'unique-share-string' });
  const importIndex = calls.findIndex(x => x.url === '/control/workspace-imports');
  const leaseIndex = calls.findIndex(x => x.url === '/control/leases');
  assert.ok(importIndex < leaseIndex);
  assert.equal(JSON.parse(calls[leaseIndex].body).workspace_id, 'independent-copy');
  assert.equal(JSON.parse(calls[leaseIndex].body).invitation_code, undefined);
  assert.ok(calls[importIndex].headers['Idempotency-Key']);
});

test('capacity retry keeps the import idempotency key and does not invent extra copies', async () => {
  const { api, calls } = fixture({ full: true });
  await assert.rejects(api.enter('', { shareCode: 'share' }));
  await assert.rejects(api.enter('', { shareCode: 'share' }));
  const imports = calls.filter(x => x.url === '/control/workspace-imports');
  assert.equal(imports.length, 2);
  assert.equal(imports[0].headers['Idempotency-Key'], imports[1].headers['Idempotency-Key']);
});

test('share button API uses control workspace UUID, never app project id', async () => {
  const { api, calls } = fixture();
  await api.enter('');
  assert.equal((await api.createShare()).code, 'unique-share-string');
  assert.ok(calls.some(x => x.url === '/control/workspaces/workspace-1/invitations'));
  assert.ok(!calls.some(x => x.url.includes('/api/projects/') || x.url.includes('/api/shares/')));
});
