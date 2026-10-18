const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../assets/runtime-workspace.js'), 'utf8');
const start = source.indexOf('  let drag = null;', source.indexOf('/* Pointer: drag node'));
const end = source.indexOf("  surface.addEventListener('wheel'", start);

function fixture() {
  const events = {}, windowEvents = {}, documentEvents = {}, classes = new Set(), capture = new Set();
  const node = { id: 'one', metadata: { position: { x: 10, y: 20 } } };
  const nodeEl = { dataset: { id: node.id }, style: {}, classList: { add() {}, remove() {} } };
  const selection = { anchorNode: 'canvas-text', focusNode: 'canvas-text', removeAllRanges() { this.anchorNode = this.focusNode = null; } };
  const c = {
    S: { view: { x: 0, y: 0, z: 1 }, selected: 'one' }, layoutBusy: false, layoutPlugin: null, saved: 0,
    window: { getSelection: () => selection, addEventListener: (type, fn) => { windowEvents[type] = fn; } },
    document: { hidden: false, addEventListener: (type, fn) => { documentEvents[type] = fn; } },
    surface: {
      addEventListener: (type, fn) => { events[type] = fn; }, contains: n => n === 'canvas-text',
      setPointerCapture: id => capture.add(id), hasPointerCapture: id => capture.has(id),
      releasePointerCapture: id => capture.delete(id),
      classList: { add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)) },
    },
    nodeOf: () => node, posOf: n => n.metadata.position, meta: n => n.metadata,
    touchCanvas() {}, applyView() {}, drawEdges() {}, drawConnector() {},
    savePrefs() { c.saved++; }, persistPosition: async () => {}, sync: p => Promise.resolve(p), fail: e => { throw e; },
    selectNode: id => { c.S.selected = id; },
  };
  vm.runInNewContext(source.slice(start, end), c);
  function pointer(type, overrides = {}) {
    const e = {
      type, pointerId: 1, button: 0, buttons: 1, isPrimary: true, clientX: 0, clientY: 0,
      target: { closest: () => null }, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...overrides,
    };
    events[type](e);
    return e;
  }
  return { c, pointer, events, windowEvents, documentEvents, classes, capture, selection, node,
    nodeTarget: { closest: s => s === '.gnode' ? nodeEl : null } };
}

test('canvas owns the pointer gesture and cancels native drag ghosts', () => {
  const f = fixture();
  assert.equal(f.pointer('pointerdown').defaultPrevented, true);
  assert.equal(f.selection.anchorNode, null);
  assert.equal(f.pointer('dragstart').defaultPrevented, true);
  f.pointer('pointermove', { clientX: 35, clientY: 20 });
  f.pointer('pointerup');
  assert.equal(f.c.S.view.x, 35);
  assert.equal(f.c.S.view.y, 20);
  assert.equal(f.classes.size, 0);
  assert.equal(f.capture.size, 0);
  assert.equal(f.c.saved, 1);
});

test('capture loss, cancel, blur and a hidden tab release the gesture without deselecting', () => {
  for (const kind of ['lostpointercapture', 'pointercancel', 'blur', 'visibilitychange']) {
    const f = fixture();
    f.pointer('pointerdown');
    if (kind === 'blur') f.windowEvents.blur({ type: kind });
    else if (kind === 'visibilitychange') { f.c.document.hidden = true; f.documentEvents[kind](); }
    else f.pointer(kind);
    assert.equal(f.c.S.selected, 'one', kind);
    assert.equal(f.classes.size, 0, kind);
    assert.equal(f.capture.size, 0, kind);
    assert.equal(f.pointer('pointerdown').defaultPrevented, true, kind);
    f.pointer('pointermove', { clientX: 25 });
    f.pointer('pointerup');
    assert.equal(f.c.S.view.x, 25, kind);
  }
});

test('secondary pointers cannot end or move the active gesture', () => {
  const f = fixture();
  f.pointer('pointerdown');
  f.pointer('pointermove', { pointerId: 2, clientX: 80 });
  f.pointer('pointerup', { pointerId: 2 });
  assert.equal(f.c.S.view.x, 0);
  assert.equal(f.classes.has('canvas-dragging'), true);
  f.pointer('pointermove', { clientX: 30 });
  f.pointer('pointerup');
  assert.equal(f.c.S.view.x, 30);
  assert.equal(f.classes.size, 0);
});

test('text, controls, inspector, and gestures owned by another tool do not pan', () => {
  for (const overrides of [
    { target: { closest: s => s === '.inspector' ? {} : null } },
    { target: { closest: s => s.includes('button, input') ? {} : null } },
    { defaultPrevented: true }, { isPrimary: false }, { button: 2 },
  ]) {
    const f = fixture();
    f.pointer('pointerdown', overrides);
    f.pointer('pointermove', { clientX: 50 });
    assert.equal(f.c.S.view.x, 0);
    assert.equal(f.classes.size, 0);
    assert.equal(f.selection.anchorNode, 'canvas-text');
  }
});

test('node header dragging keeps zoom scaling and cleans up when mouse buttons are lost', () => {
  const f = fixture();
  f.c.S.view.z = 0.5;
  f.pointer('pointerdown', { target: f.nodeTarget });
  f.pointer('pointermove', { clientX: 30, clientY: 15 });
  assert.equal(f.node.metadata.position.x, 70);
  assert.equal(f.node.metadata.position.y, 50);
  f.pointer('pointermove', { buttons: 0 });
  assert.equal(f.classes.size, 0);
  assert.equal(f.capture.size, 0);
});
