'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const { rebuild, sourceProjection } = require('../scripts/rebuild_stored_price_references');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { applyPriceIndex } = require('../lib/reuse-prices');
const fixture = require('./price-reference-fixture');
const read = file => JSON.parse(fs.readFileSync(file));
function setup(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'stored-reference-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const data = path.join(root, 'web/data/periods/2026Q2'), companies = path.join(data, 'companies');
  fs.mkdirSync(companies, { recursive: true });
  const snapshot = mergeSnapshots({ snapshots: [fixture.snapshot('000001', 'eastmoney'), fixture.snapshot('000002', 'yahoo')],
    catalog: fixture.catalog, config: fixture.config, now: fixture.now, reusePrices: false });
  const originals = new Map();
  for (const [key, doc] of Object.entries(snapshot.companySnapshots)) {
    for (const fund of doc.funds) {
      fund.rows[0].priceRange.globalPriceConflict = fixture.fixture().witness;
      originals.set(fund.code, structuredClone(fund));
    }
    fs.writeFileSync(path.join(companies, key + '.json'), JSON.stringify(doc));
  }
  fs.writeFileSync(path.join(data, 'latest.json'), JSON.stringify(snapshot.manifest));
  fs.writeFileSync(path.join(data, 'catalog.json'), JSON.stringify(fixture.catalog));
  return { root, data, originals, manifest: snapshot.manifest };
}
test('cloud rebuild persists only derived endpoints and preserves source clocks, then is idempotent', t => {
  const { root, data, originals, manifest } = setup(t);
  const result = rebuild(root, '2026Q2', { now: fixture.now, pageLimits: { maxFunds: 1, maxBytes: 100000 } });
  assert.equal(result.marketDataRequests, 0); assert.equal(result.checkedFundCount, 2);
  assert.equal(result.referenceAnnotatedRowCount, 2); assert.equal(result.afterPriceComplete, 2);
  const next = read(path.join(data, 'latest.json'));
  for (const key of ['asOf', 'retrievedAt', 'priceUpdatedAt']) assert.equal(next[key], manifest[key]);
  for (const item of next.companyFiles) {
    assert.equal(item.partCount, 2);
    for (const fund of read(path.join(data, item.path.slice('./data/'.length))).funds) {
      assert.equal(sourceProjection(fund), sourceProjection(originals.get(fund.code)));
      const range = structuredClone(fund.rows[0].priceRange); assert.ok(range.rangeReference); delete range.rangeReference;
      assert.deepEqual(range, originals.get(fund.code).rows[0].priceRange);
    }
  }
  const bytes = fs.readFileSync(path.join(data, 'latest.json'));
  const again = rebuild(root, '2026Q2', { now: fixture.now, pageLimits: { maxFunds: 1, maxBytes: 100000 } });
  assert.equal(again.changed, false); assert.equal(again.referenceAnnotatedRowCount, 0);
  assert.deepEqual(fs.readFileSync(path.join(data, 'latest.json')), bytes);
});
test('a raw price mutation aborts before replacing any published page or manifest', t => {
  const { root, data } = setup(t), file = path.join(data, 'latest.json'), before = fs.readFileSync(file);
  const existing = fs.readdirSync(path.join(data, 'companies')).sort();
  assert.throws(() => rebuild(root, '2026Q2', { now: fixture.now,
    apply(index, funds, recover) { const result = applyPriceIndex(index, funds, recover);
      if (funds.length) funds[0].rows[0].priceRange.low += 0.01; return result; } }), /SOURCE_OBSERVATION_CHANGED/);
  assert.deepEqual(fs.readFileSync(file), before);
  assert.deepEqual(fs.readdirSync(path.join(data, 'companies')).sort(), existing);
});
