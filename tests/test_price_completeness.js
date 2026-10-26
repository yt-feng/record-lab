'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { priceRange } = require('../lib/fund-analysis');
const { expectedSessions, validCompletePrice, dateHash } = require('../lib/price-coverage');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { analyzeQuarter } = require('../scripts/analyze-data');
const { priceDescription } = require('../web/app');
const interval = { start: '2026-04-01', end: '2026-06-30' };
const holding = { market: 'CN', stockCode: '600001', stockName: '合成股票', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 };
const days = expectedSessions('CN', interval.start, interval.end);
const rows = () => days.map(date => ({ date, low: 10, high: 20, close: 15 }));
const series = (values = rows()) => ({ market: 'CN', stockCode: '600001', currency: 'CNY', priceBasis: 'unadjusted',
  requestedStart: interval.start, requestedEnd: interval.end, sourceUrl: 'https://example.org/prices', sourceUrls: ['https://example.org/prices'],
  sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 }, rows: values });
const complete = value => validCompletePrice(value, 'CN', '600001', interval.start, interval.end);
const config = { funds: [{ code: '000001' }], targetPeriodEnd: interval.end, baselinePeriodEnd: '2026-03-31' };
const now = '2026-10-26T12:00:00Z';
function input(values = rows(), retrievedAt = '2026-10-26T10:00:00Z') {
  return { asOf: '2026-10-26', retrievedAt, errors: [], barsByStock: { 'CN:600001': series(values) }, funds: [{
    code: '000001', name: '合成基金', company: '合成公司', managers: [], sourcePortfolioCode: '000001',
    shareClassCodes: ['000001'], portfolioSourceUrl: 'https://example.org/identity.pdf', reports: [{
      periodEnd: interval.end, publishedAt: '2026-07-20', kind: 'quarterly', coverage: 'top10', navYuan: 10000,
      sourceUrl: 'https://example.org/report.pdf', holdings: [holding] }] }] };
}
const catalog = { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
  funds: [{ code: '000001', name: '合成基金', companyCode: 'co1', companyName: '合成公司' }] };

test('official calendar counts distinguish all six market-quarter combinations', () => {
  for (const [market, q2, q3] of [['CN', 60, 65], ['HK', 59, 65], ['US', 62, 64]]) {
    assert.equal(expectedSessions(market, '2026-04-01', '2026-06-30').length, q2);
    assert.equal(expectedSessions(market, '2026-07-01', '2026-09-30').length, q3);
  }
  assert.equal(expectedSessions('HK', '2026-07-01', '2026-09-30')[0], '2026-07-02');
  assert.equal(days.includes('2026-05-09'), false); // Government make-up workdays are not exchange sessions.
  assert.equal(expectedSessions('CN', '2025-04-01', '2025-06-30'), null);
});

test('one bar, boundary bars, an interior gap, IPO and unexplained suspension stay partial', () => {
  for (const values of [[rows()[30]], [rows()[0], rows().at(-1)], rows().filter((_, index) => index !== 30), rows().slice(40), rows().filter((_, index) => index < 20 || index > 23)]) {
    const range = priceRange(holding, series(values), interval);
    assert.equal(range.status, 'observed'); assert.equal(range.completeness, 'partial');
    assert.equal(complete(range), false); assert.ok(range.missingSessionCount > 0);
    assert.equal(priceDescription(range).complete, false);
  }
});

test('complete proof binds exact dates, interval, currency, instrument and raw-price diagnostics', () => {
  const range = priceRange(holding, series(), interval);
  assert.equal(complete(range), true);
  assert.equal(range.expectedDateSetHash, dateHash(days));
  assert.equal(range.observedDateSetHash, range.expectedDateSetHash);
  assert.equal(range.isCost, false); assert.equal(priceDescription(range).complete, true);
  for (const mutation of [{ rangeEnd: '2026-09-30' }, { stockCode: '600002' }, { currency: 'HKD' },
    { observedDateSetHash: '0'.repeat(64) }, { rejectedRowCount: 1 }, { conflictingDateCount: 1 }, { missingSessionCount: 1 }]) {
    assert.equal(complete({ ...range, ...mutation }), false, JSON.stringify(mutation));
  }
  const extras = [series([...rows(), { date: '2026-04-04', low: 9, high: 25, close: 15 }]),
    series([...rows(), rows()[0]]), series([...rows(), { date: '2026-04-02', low: 50, high: 1 }]),
    { ...series(), requestedStart: '2026-04-02' }, { ...series(), sourceValidation: null }];
  for (const value of extras) assert.equal(complete(priceRange(holding, value, interval)), false);
});

test('complete price proof survives publication, merge receipt and aggregate analysis', () => {
  const snapshot = buildSnapshot(input(), { now, config });
  assert.equal(snapshot.coverage.priceComplete, 1);
  const merged = mergeSnapshots({ snapshots: [snapshot], config, catalog, now });
  const coverage = merged.receipt.funds[0].priceCoverage;
  assert.equal(coverage.coverageVersion, 2); assert.equal(coverage.priceAvailable, 1); assert.equal(coverage.priceComplete, 1);
  assert.deepEqual(coverage.missingStocks, []);
  const fund = merged.companySnapshots.co1.funds[0]; assert.equal(complete(fund.rows[0].priceRange), true);
  const aggregate = analyzeQuarter(merged.manifest, () => merged.companySnapshots.co1, { now });
  assert.equal(aggregate.stocks[0].priceRange.completeness, 'complete');
  assert.equal(aggregate.coverage.completePriceStocks, 1); assert.equal(aggregate.coverage.incompletePriceStocks, 0);
  const partial = mergeSnapshots({ snapshots: [buildSnapshot(input([rows()[30]]), { now, config })], config, catalog, now });
  assert.equal(partial.receipt.funds[0].priceCoverage.priceAvailable, 1);
  assert.equal(partial.receipt.funds[0].priceCoverage.priceComplete, 0);
  assert.equal(partial.receipt.funds[0].priceCoverage.missingStocks.length, 1);
});

test('a fresh partial response cannot replace a saved complete range', () => {
  const previous = buildSnapshot(input(), { now, config });
  const fresh = buildSnapshot(input([rows()[30]], '2026-10-26T11:00:00Z'), { now, config, previous });
  assert.equal(complete(fresh.funds[0].rows[0].priceRange), true);
  assert.equal(fresh.funds[0].rows[0].priceRange.dataStatus, 'cached');
  const merged = mergeSnapshots({ snapshots: [previous, buildSnapshot(input([rows()[30]], '2026-10-26T11:00:00Z'), { now, config })], config, catalog, now });
  assert.equal(complete(merged.companySnapshots.co1.funds[0].rows[0].priceRange), true);
});
