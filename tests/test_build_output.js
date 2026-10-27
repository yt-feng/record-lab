'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { buildSnapshot, writeSnapshotOutput, MAX_OUTPUT_BYTES } = require('../scripts/build-data');

const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
const withDirectory = fn => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'record-output-test-'));
  try { fn(directory); } finally { fs.rmSync(directory, { recursive: true, force: true }); }
};
const fixture = () => ({
  schemaVersion: 1, retrievedAt: '2026-09-29T10:00:00+00:00',
  coverage: { fundCount: 1, holdingCount: 1, failedFundCount: 0 }, errors: [],
  funds: [{ code: '000001', name: '合成基金😀', reportRetrievedAt: '2026-09-28T10:00:00Z',
    priceUpdatedAt: '2026-09-29T09:00:00Z', managers: [{ name: '测试经理' }],
    selectedReport: { periodEnd: '2026-06-30', sourceUrl: 'https://example.org/report.pdf',
      navYuan: 1234567.89, narrative: { text: '原样短证据。\n含换行与空字符\u0000。' } },
    rows: [{ stockCode: '600001', current: { shares: 100, weightPct: 5.5, marketValueYuan: 500 },
      previous: null, priceRange: { low: 8, high: 12, fetchedAt: '2026-09-29T08:00:00Z' } }] }],
});

test('compact output keeps every nested field, value and original clock without raw NUL bytes', () => withDirectory(directory => {
  const value = fixture(); const original = structuredClone(value);
  const output = path.join(directory, 'part.json');
  const diagnostics = writeSnapshotOutput(value, output);
  const bytes = fs.readFileSync(output);
  assert.equal(bytes.toString(), `${JSON.stringify(value)}\n`);
  assert.deepEqual(JSON.parse(bytes), original);
  assert.deepEqual(value, original);
  assert.equal(bytes.includes(0), false);
  assert.equal(diagnostics.outputBytes, bytes.length);
  assert.equal(diagnostics.outputSha256, sha256(bytes));
  assert.equal(diagnostics.serialization, 'compact-json');
  assert.equal(diagnostics.maxOutputBytes, 25_000_000);
  assert.equal(MAX_OUTPUT_BYTES, 25_000_000);
  assert.ok(bytes.length < Buffer.byteLength(`${JSON.stringify(value, null, 2)}\n`));
  assert.deepEqual(fs.readdirSync(directory), ['part.json']);
}));

test('a fixture that exceeds its ceiling when indented fits losslessly as compact JSON', () => withDirectory(directory => {
  const value = fixture(); const compactBytes = Buffer.byteLength(`${JSON.stringify(value)}\n`);
  const prettyBytes = Buffer.byteLength(`${JSON.stringify(value, null, 2)}\n`);
  assert.ok(prettyBytes > compactBytes);
  const output = path.join(directory, 'part.json');
  writeSnapshotOutput(value, output, { maxOutputBytes: compactBytes });
  assert.deepEqual(JSON.parse(fs.readFileSync(output)), value);
  assert.equal(fs.statSync(output).size, compactBytes);
}));

test('UTF-8 ceiling counts CJK, astral characters and the newline; exact fits and one byte over rejects', () => withDirectory(directory => {
  const output = path.join(directory, 'part.json'); const value = { text: '中😀' };
  const serialized = `${JSON.stringify(value)}\n`; const bytes = Buffer.byteLength(serialized);
  assert.ok(bytes > serialized.length);
  writeSnapshotOutput(value, output, { maxOutputBytes: bytes });
  const prior = fs.readFileSync(output);
  assert.throws(() => writeSnapshotOutput(value, output, { maxOutputBytes: bytes - 1 }), error => {
    assert.equal(error.code, 'OUTPUT_TOO_LARGE');
    assert.deepEqual(error.outputDiagnostics, { serialization: 'compact-json', outputBytes: bytes,
      maxOutputBytes: bytes - 1, outputSha256: sha256(serialized) });
    assert.equal(JSON.stringify(error.outputDiagnostics).includes(value.text), false);
    assert.equal(error.message, 'OUTPUT_TOO_LARGE');
    return true;
  });
  assert.deepEqual(fs.readFileSync(output), prior);
  assert.deepEqual(fs.readdirSync(directory), ['part.json']);
}));

test('oversize new output does not create directories, partial parts, or temporary files', () => withDirectory(directory => {
  const output = path.join(directory, 'absent', 'part.json');
  assert.throws(() => writeSnapshotOutput(fixture(), output, { maxOutputBytes: 1 }), { code: 'OUTPUT_TOO_LARGE' });
  assert.deepEqual(fs.readdirSync(directory), []);
}));

test('programmatic tests can lower the ceiling but no caller can raise or disable it', () => withDirectory(directory => {
  for (const maxOutputBytes of [MAX_OUTPUT_BYTES + 1, Infinity, NaN, 0, -1, 1.5, true, '25000000', null]) {
    assert.throws(() => writeSnapshotOutput({}, path.join(directory, 'part.json'), { maxOutputBytes }), { code: 'INVALID_ARGUMENTS' });
  }
  assert.deepEqual(fs.readdirSync(directory), []);
}));

test('serialization failure is fixed and preserves previous bytes', () => withDirectory(directory => {
  const output = path.join(directory, 'part.json'); const prior = '{"old":true}\n';
  fs.writeFileSync(output, prior);
  const circular = { text: 'private fixture text' }; circular.self = circular;
  for (const value of [circular, { amount: 1n }, undefined]) {
    assert.throws(() => writeSnapshotOutput(value, output), error => {
      assert.equal(error.code, 'OUTPUT_WRITE_FAILED');
      assert.equal(error.message, 'OUTPUT_WRITE_FAILED');
      return true;
    });
    assert.equal(fs.readFileSync(output, 'utf8'), prior);
  }
  assert.deepEqual(fs.readdirSync(directory), ['part.json']);
}));

test('an existing temporary file is never deleted or used to replace a prior part', () => withDirectory(directory => {
  const output = path.join(directory, 'part.json');
  const temporary = `${output}.tmp-${process.pid}`;
  fs.writeFileSync(output, '{"old":true}\n'); fs.writeFileSync(temporary, 'unrelated temporary content');
  assert.throws(() => writeSnapshotOutput(fixture(), output), { code: 'OUTPUT_WRITE_FAILED' });
  assert.equal(fs.readFileSync(output, 'utf8'), '{"old":true}\n');
  assert.equal(fs.readFileSync(temporary, 'utf8'), 'unrelated temporary content');
}));

test('failed rename removes only its own temporary file and preserves the existing target', () => withDirectory(directory => {
  const output = path.join(directory, 'part.json'); fs.mkdirSync(output);
  fs.writeFileSync(path.join(output, 'retained.txt'), 'retained');
  assert.throws(() => writeSnapshotOutput(fixture(), output), { code: 'OUTPUT_WRITE_FAILED' });
  assert.equal(fs.readFileSync(path.join(output, 'retained.txt'), 'utf8'), 'retained');
  assert.deepEqual(fs.readdirSync(directory), ['part.json']);
}));

test('actual CLI emits only fixed counts, byte limit and digest for the exact normalized output', () => withDirectory(directory => {
  const input = path.join(directory, 'input.json'); const output = path.join(directory, 'part.json');
  const configPath = path.join(directory, 'config.json');
  const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001' }] };
  const raw = { asOf: '2026-09-29', retrievedAt: '2026-09-29T10:00:00Z', funds: [{ code: '000001', name: '合成基金😀',
    reports: [{ fundCode: '000001', periodEnd: '2026-06-30', publishedAt: '2026-07-20', kind: 'quarterly',
      sourceUrl: 'https://example.org/report.pdf', navYuan: 10000, coverage: 'top10',
      holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 }] }],
    managers: [], shareClassCodes: ['000001'] }], barsByStock: {}, errors: [], coverage: {} };
  fs.writeFileSync(input, JSON.stringify(raw)); fs.writeFileSync(configPath, JSON.stringify(config));
  const expected = buildSnapshot(raw, { config });
  const cli = spawnSync(process.execPath, [path.join(__dirname, '..', 'scripts/build-data.js'),
    '--input', input, '--output', output, '--config', configPath], { encoding: 'utf8' });
  assert.equal(cli.status, 0); assert.equal(cli.stderr, '');
  const bytes = fs.readFileSync(output); const diagnostics = JSON.parse(cli.stdout);
  assert.deepEqual(JSON.parse(bytes), expected);
  assert.equal(bytes.toString(), `${JSON.stringify(expected)}\n`);
  assert.deepEqual(diagnostics, { status: 'ok', fundCount: expected.coverage.fundCount,
    failedFundCount: expected.coverage.failedFundCount, holdingCount: expected.coverage.holdingCount,
    errorCount: expected.errors.length, serialization: 'compact-json', outputBytes: bytes.length,
    maxOutputBytes: 25_000_000, outputSha256: sha256(bytes) });
  assert.equal(cli.stdout.includes('合成'), false); assert.equal(cli.stdout.includes(directory), false);
  const noOverride = spawnSync(process.execPath, [path.join(__dirname, '..', 'scripts/build-data.js'),
    '--max-output-bytes', '50000000'], { encoding: 'utf8' });
  assert.equal(noOverride.status, 1);
  assert.deepEqual(JSON.parse(noOverride.stdout), { status: 'failed', errorCode: 'INVALID_ARGUMENTS' });
}));
