const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../assets/runtime-workspace.js'), 'utf8');

function followFixture() {
  const context = {
    S: { doc: { id: 'one' }, view: { y: 140, z: 0.85 } },
    scroller: { scrollTop: 0 }, prefs: { follow: true }, drag: null,
    app: { classList: { contains: () => false } },
    applyView() {}, drawConnector() {}, savePrefs() {},
  };
  const start = source.indexOf('  let followPosition =');
  const end = source.indexOf('  /* Pointer:', start);
  vm.runInNewContext(source.slice(start, end) + '\nthis.touchCanvas = touchCanvas;', context);
  context.resetFollowPosition();
  return context;
}

test('scrolling keeps the source/card offset exact in both directions and off screen', () => {
  const c = followFixture();
  for (const top of [100, 700, 1800, 3200, 1700, 0]) {
    c.scroller.scrollTop = top;
    c.followScroll();
    assert.equal(c.S.view.y, 140 - top);
  }
});

test('canvas zoom does not scale the reading scroll delta', () => {
  for (const zoom of [0.35, 0.85, 1, 1.6]) {
    const c = followFixture();
    c.S.view.z = zoom;
    c.scroller.scrollTop = 650;
    c.followScroll();
    assert.equal(c.S.view.y, -510);
  }
});

test('manual movement establishes a new offset without a pause or snap', () => {
  const c = followFixture();
  c.scroller.scrollTop = 200; c.followScroll();
  c.S.view.y = 380; c.touchCanvas();
  c.scroller.scrollTop = 260; c.followScroll();
  assert.equal(c.S.view.y, 320);
});

test('disabled follow and document switches do not inherit old scroll deltas', () => {
  const c = followFixture();
  c.prefs.follow = false;
  c.scroller.scrollTop = 800; c.followScroll();
  assert.equal(c.S.view.y, 140);
  c.prefs.follow = true;
  c.scroller.scrollTop = 850; c.followScroll();
  assert.equal(c.S.view.y, 90);
  c.S.doc = { id: 'two' }; c.scroller.scrollTop = 0; c.followScroll();
  assert.equal(c.S.view.y, 90);
});

function projectFixture(managed) {
  const name = { textContent: '' }, usage = { textContent: '' };
  const calls = [];
  const context = {
    revision: 0, highlights: [],
    S: { projects: [{ id: 'app-project', title: '我的工作区' }], docs: [] },
    api: { managed, lease: { workspace_id: 'workspace-a' },
      async get(url, opts) { calls.push({ url, opts }); return { workspace: { title: url.endsWith('workspace-a') ? '项目甲' : '项目乙' } }; } },
    p: encodeURIComponent, sync: value => value,
    prefKey: () => 'view', hlKey: () => 'hl', readJSON: (key, fallback) => fallback,
    $: selector => selector === '#ws-name' ? name : usage,
    T: { sel: new Set() }, closeFind() {}, hideAsk() {},
    async refreshDocs() {}, async loadHistory() {}, async refreshGraph() {},
    docOf() {}, renderTree() {}, renderReader() {},
  };
  const start = source.indexOf('  async function openProject(id)');
  const end = source.indexOf('  async function loadProjects(', start);
  vm.runInNewContext(source.slice(start, end), context);
  return { context, name, calls };
}

test('managed project switches display the control workspace title without changing the app ID', async () => {
  const { context: c, name, calls } = projectFixture(true);
  await c.openProject('app-project');
  assert.equal(name.textContent, '项目甲');
  c.api.lease.workspace_id = 'workspace-b';
  await c.openProject('app-project');
  assert.equal(name.textContent, '项目乙');
  assert.equal(c.S.project.id, 'app-project');
  assert.equal(c.S.projects[0].title, '我的工作区');
  assert.equal(calls[1].url, '/control/workspaces/workspace-b');
  assert.equal(calls[1].opts.auth, false);
});

test('standalone project names do not call the control plane', async () => {
  const { context: c, name, calls } = projectFixture(false);
  await c.openProject('app-project');
  assert.equal(name.textContent, '我的工作区');
  assert.equal(calls.length, 0);
});
