'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), crypto = require('node:crypto');
const { writeMergeReceipt, MAX_FUNDS } = require('../lib/merge-receipt');
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const receipt = count => ({ schemaVersion: 1, phase, generatedAt: '2026-10-29T12:00:00Z',
  requestedCodes: Array.from({ length: count }, (_, index) => String(index + 1).padStart(6, '0')),
  funds: Array.from({ length: count }, (_, index) => ({ code: String(index + 1).padStart(6, '0'),
    status: 'complete', sourceEvidence: 'synthetic '.repeat(20) })),
  pricePatches: [{ code: '000001', accepted: false }], strategyClaims: [], extraMetadata: { originalClock: '2026-10-28T01:00:00Z' } });
function setup(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'merge-receipt-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return path.join(root, 'merge-receipt.json');
}
test('small receipts retain exact legacy bytes; paged receipts preserve all rows and metadata', t => {
  const file = setup(t), original = receipt(6);
  writeMergeReceipt(file, original);
  assert.equal(fs.readFileSync(file, 'utf8'), JSON.stringify(original) + '\n');
  const result = writeMergeReceipt(file, original, { legacyLimit: 500, pageLimit: 800 });
  assert.ok(result.pageCount > 1);
  const manifest = JSON.parse(fs.readFileSync(file));
  const { funds, ...metadata } = original;
  assert.deepEqual(manifest.receipt, metadata);
  const reconstructed = [];
  for (const page of manifest.fundPages) {
    const raw = fs.readFileSync(path.join(path.dirname(file), page.path));
    assert.equal(raw.length, page.byteLength); assert.ok(raw.length <= 800);
    assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), page.sha256);
    const value = JSON.parse(raw); assert.deepEqual(value.phase, original.phase);
    assert.equal(value.generatedAt, original.generatedAt); reconstructed.push(...value.funds);
  }
  assert.deepEqual(reconstructed, funds);
  assert.equal(manifest.totalPageBytes, manifest.fundPages.reduce((sum, row) => sum + row.byteLength, 0));
  assert.equal(manifest.fundCount, funds.length);
  assert.deepEqual(original, receipt(6));
});
test('invalid or oversized receipts cannot replace an existing receipt manifest', t => {
  const file = setup(t), original = receipt(1); writeMergeReceipt(file, original);
  const before = fs.readFileSync(file);
  const variants = [receipt(MAX_FUNDS + 1), { ...receipt(2), funds: receipt(2).funds.reverse() },
    { ...receipt(2), requestedCodes: ['000001', '000001'] },
    { ...receipt(0), requestedCodes: [100001] },
    { ...receipt(1), funds: [{ code: '000002' }] },
    { ...receipt(1), funds: [{ code: '000001', data: 'x'.repeat(1000) }] }, receipt(65)];
  for (const value of variants) {
    assert.throws(() => writeMergeReceipt(file, value, { legacyLimit: 100, pageLimit: 500 }),
      error => ['MERGE_RECEIPT_INVALID', 'MERGE_RECEIPT_TOO_LARGE'].includes(error.code));
    assert.deepEqual(fs.readFileSync(file), before);
  }
});
test('a page write failure leaves the old manifest readable and directory symlinks are refused', t => {
  const file = setup(t), original = receipt(1); writeMergeReceipt(file, original);
  const before = fs.readFileSync(file), target = path.join(path.dirname(file), 'merge-receipt-pages');
  fs.writeFileSync(target, 'not a directory');
  assert.throws(() => writeMergeReceipt(file, receipt(6), { legacyLimit: 100, pageLimit: 800 }),
    error => error.code === 'MERGE_RECEIPT_INVALID');
  assert.deepEqual(fs.readFileSync(file), before);
  fs.unlinkSync(target); fs.symlinkSync(path.dirname(file), target);
  assert.throws(() => writeMergeReceipt(file, receipt(6), { legacyLimit: 100, pageLimit: 800 }),
    error => error.code === 'MERGE_RECEIPT_INVALID');
  assert.deepEqual(fs.readFileSync(file), before);
});
test('receipt failure during actual merge leaves the original production manifest and pages intact', t => {
  const file = setup(t), root = path.dirname(file), parts = path.join(root, 'parts'), data = path.join(root, 'data');
  fs.mkdirSync(parts); fs.mkdirSync(data);
  const f = require('./price-reference-fixture');
  const { mergeFiles } = require('../scripts/merge-parts');
  fs.writeFileSync(path.join(parts, 'part-0.json'), JSON.stringify(f.snapshot('000001', 'eastmoney')));
  const first = mergeFiles({ parts, data, catalog: f.catalog, config: f.config, now: f.now });
  const manifestPath = path.join(data, 'latest.json'), beforeManifest = fs.readFileSync(manifestPath);
  const companyRoot = path.join(data, 'companies');
  const beforePages = new Map(fs.readdirSync(companyRoot).map(name => [name, fs.readFileSync(path.join(companyRoot, name))]));
  fs.writeFileSync(path.join(parts, 'part-0.json'), JSON.stringify(f.snapshot('000002', 'eastmoney')));
  const codec = require('../lib/merge-receipt'), originalWrite = codec.writeMergeReceipt;
  const storage = require.resolve('../scripts/merge-storage');
  try {
    codec.writeMergeReceipt = (name, value) => originalWrite(name, value, { legacyLimit: 1, pageLimit: 256 });
    delete require.cache[storage];
    assert.throws(() => mergeFiles({ parts, data, catalog: f.catalog, config: f.config, now: f.now,
      previousManifest: first.manifest }), error => error.code === 'MERGE_RECEIPT_TOO_LARGE');
  } finally { codec.writeMergeReceipt = originalWrite; delete require.cache[storage]; }
  assert.deepEqual(fs.readFileSync(manifestPath), beforeManifest);
  assert.deepEqual(fs.readdirSync(companyRoot).sort(), [...beforePages.keys()].sort());
  for (const [name, payload] of beforePages) assert.deepEqual(fs.readFileSync(path.join(companyRoot, name)), payload);
});
