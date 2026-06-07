import test from 'node:test';
import assert from 'node:assert/strict';

test('boolean coercion for stream flag', () => {
  const toStream = Boolean(true);
  assert.equal(toStream, true);
});
