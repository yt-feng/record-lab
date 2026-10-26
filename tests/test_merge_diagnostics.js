'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { checkpoint, diagnose } = require('../lib/merge-diagnostics');
test('checkpoints preserve the single JSON stdout contract even inside Actions', () => {
  const before = process.env.GITHUB_ACTIONS, write = process.stdout.write;
  let emitted = false;
  try {
    process.env.GITHUB_ACTIONS = 'true'; process.stdout.write = () => { emitted = true; return true; };
    checkpoint('inputs', 0); checkpoint('components', 500); checkpoint('done', 501);
  } finally { process.stdout.write = write; if (before === undefined) delete process.env.GITHUB_ACTIONS; else process.env.GITHUB_ACTIONS = before; }
  assert.equal(emitted, false);
});
test('typed errors expose only fixed metadata and allowlisted source locations', () => {
  checkpoint('components', 500);
  const error = new TypeError('PRIVATE_MESSAGE_MARKER');
  error.code = 'ENOSPC';
  error.stack = 'PRIVATE_STACK_MARKER\n at privateFunction (/private/location/scripts/merge-storage.js:176:18)\n at secret (/private/unknown.js:1:2)';
  const result = diagnose(error);
  assert.equal(result.errorClass, 'TYPE_ERROR'); assert.equal(result.systemCode, 'ENOSPC');
  assert.equal(result.phase, 'components'); assert.equal(result.completed, 500);
  assert.deepEqual(result.frames, [{ module: 'merge-storage.js', line: 176, column: 18 }]);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE|private|location|secret|unknown/);
  error.code = 'PRIVATE_CODE'; assert.equal(diagnose(error).systemCode, undefined);
});
test('invalid phases and oversized stacks cannot inject public strings', () => {
  checkpoint('prices', 1000); checkpoint('PRIVATE_PHASE', 2); checkpoint('inputs', -1);
  const error = new RangeError('hidden'); error.stack = 'hidden\n' + 'x'.repeat(9000) + '/merge-storage.js:1:2';
  const value = diagnose(error); assert.equal(value.phase, 'prices'); assert.equal(value.completed, 1000);
  assert.equal(value.errorClass, 'RANGE_ERROR'); assert.deepEqual(value.frames, []);
});
