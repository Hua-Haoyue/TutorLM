import test from 'node:test';
import assert from 'node:assert/strict';
import { attachLeaseLifecycle } from '../assets/lease-lifecycle.js';

function fixture({ beacon = true } = {}) {
  const calls = [], beacons = [], timers = new Map(), listeners = new Map();
  let counter = 0, now = 1000000;
  const env = {
    Date: { now: () => now }, crypto: { randomUUID: () => `page-${++counter}` },
    navigator: { sendBeacon(url) { beacons.push(url); return beacon; } },
    async fetch(url, options) {
      calls.push({ url, options });
      return { ok: true, status: 200, async json() { return { heartbeat_interval_ms: 20000 }; } };
    },
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name) { listeners.delete(name); },
    setTimeout(callback, delay) { const id = ++counter; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  return {
    env, calls, beacons, timers, listeners,
    advance(value) { now += value; },
    async fire(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `No timer at ${delay}ms`);
      timers.delete(entry[0]);
      await entry[1].callback();
    },
    lease: { lease_id: 'lease-A', expires_at: 1100 },
  };
}

test('attaches a document and emits only presence heartbeats', async () => {
  const f = fixture();
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  await lifecycle.ready;
  assert.equal(f.calls[0].url, '/control/leases/lease-A/pages');
  assert.ok(JSON.parse(f.calls[0].options.body).page_id);
  await f.fire(20000);
  assert.match(f.calls[1].url, /\/heartbeat$/);
  assert.equal(f.calls[1].options.credentials, 'same-origin');
  assert.ok(!f.calls.some(call => /touch/.test(call.url)));
  lifecycle.dispose();
});

test('pagehide sends one beacon for the precise lease and document', async () => {
  const f = fixture();
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  await lifecycle.ready;
  f.listeners.get('pagehide')({ persisted: false });
  f.listeners.get('pagehide')({ persisted: false });
  assert.equal(f.beacons.length, 1);
  assert.match(f.beacons[0], /^\/control\/leases\/lease-A\/pages\/page-\d+\/close$/);
  assert.ok(![...f.timers.values()].some(timer => timer.delay === 20000));
  lifecycle.dispose();
});

test('beacon failure falls back to fetch keepalive', async () => {
  const f = fixture({ beacon: false });
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  await lifecycle.ready;
  f.listeners.get('pagehide')({});
  assert.equal(f.calls.at(-1).options.keepalive, true);
  assert.equal(f.calls.at(-1).options.method, 'POST');
  lifecycle.dispose();
});

test('back-forward restoration uses a new document identity', async () => {
  const f = fixture();
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  await lifecycle.ready;
  const previous = JSON.parse(f.calls[0].options.body).page_id;
  f.listeners.get('pagehide')({ persisted: true });
  f.listeners.get('pageshow')({ persisted: true });
  await Promise.resolve();
  const next = JSON.parse(f.calls.at(-1).options.body).page_id;
  assert.notEqual(next, previous);
  lifecycle.dispose();
});

test('the absolute deadline stops heartbeats regardless of activity', async () => {
  const f = fixture();
  const failures = [];
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env, onLeaseLost: error => failures.push(error) });
  await lifecycle.ready;
  f.advance(100000);
  await f.fire(100000);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].status, 401);
  assert.equal(f.timers.size, 0);
  lifecycle.dispose();
});

test('a terminal heartbeat response stops lifecycle and reports loss', async () => {
  const f = fixture();
  const failures = [];
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env, onLeaseLost: error => failures.push(error) });
  await lifecycle.ready;
  f.env.fetch = async () => ({ ok: false, status: 401 });
  await f.fire(20000);
  assert.equal(failures.length, 1);
  assert.equal(f.timers.size, 0);
  lifecycle.dispose();
});

test('late attach response after pagehide cannot restart the heartbeat', async () => {
  const f = fixture();
  let complete;
  f.env.fetch = () => new Promise(resolve => { complete = resolve; });
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  f.listeners.get('pagehide')({});
  complete({ ok: true, async json() { return { heartbeat_interval_ms: 20000 }; } });
  await lifecycle.ready;
  assert.ok(![...f.timers.values()].some(timer => timer.delay === 20000));
  lifecycle.dispose();
});

test('transient attach failures are retried without extending the deadline', async () => {
  const f = fixture();
  const original = f.env.fetch;
  f.env.fetch = async () => { throw new TypeError('offline'); };
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  await assert.rejects(lifecycle.ready, /offline/);
  f.env.fetch = original;
  await f.fire(20000);
  assert.equal(f.calls.length, 1);
  assert.ok([...f.timers.values()].some(timer => timer.delay === 100000));
  lifecycle.dispose();
});

test('secure random bytes generate a UUID when randomUUID is unavailable', async () => {
  const f = fixture();
  f.env.crypto = { getRandomValues(bytes) { bytes.fill(7); return bytes; } };
  const lifecycle = attachLeaseLifecycle(f.lease, { environment: f.env });
  await lifecycle.ready;
  const page = JSON.parse(f.calls[0].options.body).page_id;
  assert.match(page, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  lifecycle.dispose();
});
