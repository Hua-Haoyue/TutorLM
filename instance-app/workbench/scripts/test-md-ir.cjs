const { test } = require('node:test');
const assert = require('node:assert/strict');
const { changedRange, touches, shiftRanges, sourceCaret, firstContent } = require('../assets/md-ir.js');
const { createRenderer } = require('../assets/markdown.js');

test('changedRange finds the single edited span', () => {
  assert.deepEqual(changedRange('abcdef', 'abXYef'), { start: 2, oldEnd: 4, newEnd: 4 });
  assert.deepEqual(changedRange('abc', 'abc!'), { start: 3, oldEnd: 3, newEnd: 4 });
  assert.deepEqual(changedRange('hello world', 'hello'), { start: 5, oldEnd: 11, newEnd: 5 });
  assert.deepEqual(changedRange('aaa', 'aaaa'), { start: 3, oldEnd: 3, newEnd: 4 });
});

test('protected text cannot be changed, but text around it can', () => {
  const anchor = { start: 6, end: 11 };               // "world" in "hello world!"
  const at = (before, after) => touches(changedRange(before, after), anchor);
  assert.equal(at('hello world!', 'hello wrld!'), true, 'deleting inside');
  assert.equal(at('hello world!', 'hello worlds!'), false, 'appending at the end edge');
  assert.equal(at('hello world!', 'hello wo-rld!'), true, 'inserting inside');
  assert.equal(at('hello world!', 'hello, world!'), false, 'inserting before');
  assert.equal(at('hello world!', 'hello world?'), false, 'after');
  assert.equal(at('hello world!', '!'), true, 'deleting the whole block');
  assert.equal(at('hello world!', 'hello xorld!'), true, 'replacing a character');
});

test('ranges after an allowed edit move with it', () => {
  const ranges = [{ id: 'a', start: 0, end: 3 }, { id: 'b', start: 10, end: 14 }];
  const moved = shiftRanges(ranges, changedRange('abc defghi jklm', 'abc dXXXefghi jklm'));
  assert.deepEqual(moved.map((r) => [r.id, r.start, r.end]), [['a', 0, 3], ['b', 13, 17]]);
});

test('a click maps to the matching source position', () => {
  assert.equal(sourceCaret('## Hello **bold** world', 'Hello bold', 'Hello bold world'.length), '## Hello **bold'.length);
  assert.equal(sourceCaret('- item one', '', 8), 2);
  assert.equal(firstContent('### Title'), 4);
  assert.equal(firstContent('- [x] done'), 6);
});

test('math delimiters models emit render as inline and display math', () => {
  const B = String.fromCharCode(92);
  const r = createRenderer({ math: (m, display) => `[${display ? 'D' : 'I'}:${m}]` });
  const one = (s) => r.render(s, { blocks: false });
  assert.match(one(`a ${B}(x^2${B}) b`), /\[I:x\^2\]/);
  assert.match(one(`a ${B}[${B}int x${B}] b`), /math--display">\[D:\\int x\]/);
  assert.match(one(`${B}[\n a+b\n${B}]`), /<div class="math-block">\[D:a\+b\]<\/div>/);
  assert.match(one('inline $$E=mc^2$$ done'), /\[D:E=mc\^2\]/);
  assert.match(one(`not ${B}${B}( math`), /not \\\( math/);
  assert.match(one('| a | b |\n|---|---|\n| $x$ | 2 |'), /<td>.*\[I:x\]/);
});
