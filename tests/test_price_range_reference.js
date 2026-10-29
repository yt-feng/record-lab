'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { buildReference, normalizeReference, effectiveReference, factFor } = require('../lib/price-range-reference');
const { validUsableCompletePrice } = require('../lib/price-coverage');
const { conflictFor } = require('../lib/price-consistency');
const { mergeSnapshots, mergeFiles } = require('../scripts/merge-parts');
const { analyzeQuarter } = require('../scripts/analyze-data');
const { priceDescription } = require('../web/app');
const f = require('./price-reference-fixture');
test('directional endpoints classification is derived and rejects genuinely different or unknown facts', () => {
  const { facts, reference, witness, prices } = f.fixture(); assert.ok(reference);
  const raw = { ...prices[1], globalPriceConflict: witness, rangeReference: reference };
  const before = structuredClone(raw), effective = effectiveReference(raw, f.identity, f.now);
  assert.equal(effective.low, 73.46); assert.equal(effective.fetchedAt, f.fetchedAt); assert.deepEqual(raw, before);
  assert.equal(validUsableCompletePrice(raw, 'CN', '600001', f.identity.rangeStart, f.identity.rangeEnd), true);
  assert.equal(priceDescription(raw).complete, true); assert.equal(priceDescription(raw).conflictPending, false);
  assert.equal(priceDescription(raw).referenceCompatible, true);
  const variants = [f.price('yahoo', Math.fround(73.46) + 1e-10, Math.fround(100.1)),
    f.price('unknown', Math.fround(73.46), Math.fround(100.1)),
    f.price('eastmoney', 73.46 + 1e-10, 100.1), f.price('yahoo', Math.fround(73.46), 200)];
  for (const changed of variants) assert.equal(buildReference([facts[0], factFor(changed)], f.identity, f.now), null);
  assert.equal(buildReference([factFor(f.price('eastmoney', Math.fround(73.46), Math.fround(100.1))),
    factFor(f.price('yahoo'))], f.identity, f.now), null);
  assert.equal(buildReference([facts[1], factFor(f.price('yahoo', Math.fround(73.46) + 1e-10))], f.identity, f.now), null);
  const third = factFor(f.price('eastmoney', 73.46 + 1e-10));
  const thirdWitness = conflictFor([facts[0], third], f.identity, f.now);
  assert.equal(effectiveReference({ ...raw, globalPriceConflict: thirdWitness }, f.identity, f.now), null);
  assert.equal(effectiveReference({ ...raw, low: 73.46 + 1e-10 }, f.identity, f.now), null);
  assert.equal(normalizeReference({ ...reference, factSetDigest: '0'.repeat(64) }, f.identity, f.now), null);
  const partial = { ...f.price('eastmoney', 70, 100.1, true), rangeReference: reference };
  assert.equal(effectiveReference(partial, f.identity, f.now), null);
});
test('same fact selects latest microsecond observation deterministically without editing the source', () => {
  const { facts } = f.fixture(), later = structuredClone(facts[0]); later.fetchedAt = later.fetchedAt.replace('000001', '000002');
  const a = buildReference([...facts, later], f.identity, f.now), b = buildReference([later, ...facts], f.identity, f.now);
  assert.deepEqual(a, b); assert.equal(a.facts.find(x => x.factId === a.referenceFactId).fetchedAt, later.fetchedAt);
  assert.equal(facts[0].fetchedAt, f.fetchedAt);
});
test('disk merge persists derived-only recovery, receipts and analysis consume it, second pass is unchanged', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'price-reference-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const data = path.join(root, 'data'), parts = path.join(root, 'parts'); fs.mkdirSync(data); fs.mkdirSync(parts);
  const snapshots = [f.snapshot('000001', 'eastmoney'), f.snapshot('000002', 'yahoo')];
  const first = mergeSnapshots({ snapshots, catalog: f.catalog, config: f.config, now: f.now, reusePrices: false });
  // Emulate legacy durable conflict metadata before reference classification.
  const witness = f.fixture().witness;
  for (const doc of Object.values(first.companySnapshots)) for (const fund of doc.funds) fund.rows[0].priceRange.globalPriceConflict = witness;
  fs.mkdirSync(path.join(data, 'companies'));
  for (const file of first.manifest.companyFiles) fs.writeFileSync(path.join(data, 'companies', path.basename(file.path)),
    JSON.stringify(first.companySnapshots[path.basename(file.path, '.json')]));
  fs.writeFileSync(path.join(data, 'latest.json'), JSON.stringify(first.manifest));
  const originals = new Map(Object.values(first.companySnapshots).flatMap(x => x.funds).map(x => [x.code, structuredClone(x.rows[0].priceRange)]));
  const merged = mergeFiles({ parts, data, catalog: f.catalog, config: f.config, now: f.now,
    previousManifest: first.manifest, receiptCodes: ['000001', '000002'], reuseAcrossComponents: true });
  assert.equal(merged.changed, true); assert.equal(merged.counts.reusedPriceFundCount, 2);
  const docs = merged.manifest.companyFiles.map(file => JSON.parse(fs.readFileSync(path.join(data, 'companies', path.basename(file.path)))));
  for (const fund of docs.flatMap(x => x.funds)) {
    const range = structuredClone(fund.rows[0].priceRange); assert.ok(range.rangeReference); delete range.rangeReference;
    assert.deepEqual(range, originals.get(fund.code));
    const coverage = merged.receipt.funds.find(item => item.code === fund.code).priceCoverage;
    assert.equal(coverage.priceComplete, 1); assert.equal(coverage.priceConflicted, 0); assert.equal(coverage.referenceResolutions.length, 1);
  }
  const analysis = analyzeQuarter(merged.manifest, file => docs[merged.manifest.companyFiles.findIndex(x => x.path === file)], { now: f.now });
  assert.equal(analysis.stocks[0].priceRange.low, 73.46); assert.equal(analysis.coverage.completePriceStocks, 1);
  assert.equal(analysis.stocks[0].priceRange.rangeReferenceSummary.storedFactCount, 2);
  assert.equal(analysis.stocks[0].priceRange.sourceCount, 2);
  const again = mergeFiles({ parts, data, catalog: f.catalog, config: f.config, now: f.now, previousManifest: merged.manifest,
    receiptCodes: ['000001', '000002'], reuseAcrossComponents: true });
  assert.equal(again.changed, false); assert.equal(again.counts.reusedPriceFundCount, 0);
});
