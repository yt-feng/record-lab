'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildSnapshot } = require('../scripts/build-data');
const { analyzeQuarter } = require('../scripts/analyze-data');
const { createAnalysisSpool } = require('../scripts/analysis-spool');
const { expectedSessions, validCompletePrice } = require('../lib/price-coverage');
const { reuseCompletePrices } = require('../lib/reuse-prices');

const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const now = '2026-10-26T12:00:00Z', at = '2026-10-26T11:00:00Z';
const stock = { market: 'CN', stockCode: '600001', currency: 'CNY', stockName: '合成股票',
  shares: 100, weightPct: 5, marketValueYuan: 500 };

function fixture(observations) {
  const docs = {}, sources = [];
  observations.forEach(([low, high, complete = true], i) => {
    const code = String(i + 1).padStart(6, '0');
    const sourceUrl = `https://example.org/prices-${code}`;
    sources.push(sourceUrl);
    const report = { fundCode: code, kind: 'quarterly', coverage: 'top10', navYuan: 10000, holdings: [stock],
      sourceUrl: `https://example.org/${code}.pdf`, periodEnd: phase.targetPeriodEnd, publishedAt: '2026-07-20' };
    const sessions = expectedSessions('CN', '2026-04-01', phase.targetPeriodEnd);
    const series = { ...stock, priceBasis: 'unadjusted', requestedStart: '2026-04-01', requestedEnd: phase.targetPeriodEnd,
      sourceUrl, retrievedAt: at, sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
      rows: (complete ? sessions : sessions.slice(0, 1)).map(date => ({ date, low, high, close: (low + high) / 2 })) };
    const snapshot = buildSnapshot({ asOf: '2026-10-26', retrievedAt: at, errors: [], barsByStock: { 'CN:600001': series },
      funds: [{ code, name: '合成基金', companyId: 'co1', shareClassCodes: [code], sourcePortfolioCode: code,
        portfolioSourceUrl: 'https://example.org/identity.pdf', reports: [report, { ...report,
          periodEnd: phase.baselinePeriodEnd, publishedAt: '2026-04-20', sourceUrl: `https://example.org/${code}-prior.pdf` }] }] },
    { now, config: { ...phase, funds: [{ code }] } });
    assert.equal(validCompletePrice(snapshot.funds[0].rows[0].priceRange, 'CN', '600001', '2026-04-01', phase.targetPeriodEnd), complete);
    docs[`./data/companies/co-${code}.json`] = snapshot;
  });
  return { docs, sources, manifest: { schemaVersion: 1, sourceMode: 'actions', ...phase, asOf: '2026-10-26',
    retrievedAt: at, coverage: { fundCount: observations.length, catalogFundCount: observations.length },
    companyFiles: Object.keys(docs).map(path => ({ path })) } };
}

function analyzeBoth(input) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analysis-price-conflicts-'));
  const original = structuredClone(input.docs), memoryParts = new Map(), diskParts = new Map();
  const spool = createAnalysisSpool(path.join(root, 'spool'), { maxBufferBytes: 500 });
  try {
    const memory = analyzeQuarter(input.manifest, file => input.docs[file], { now,
      writePart: (file, value) => memoryParts.set(file, value) });
    const disk = analyzeQuarter(input.manifest, file => input.docs[file], { now, spool,
      writePart: (file, value) => diskParts.set(file, value) });
    assert.deepEqual(disk, memory);
    assert.deepEqual(diskParts, memoryParts);
    assert.deepEqual(input.docs, original, 'analysis must preserve original price intervals and evidence');
    assert.deepEqual(memory.stocks[0].priceRange.sourceUrls, [...input.sources].sort());
    assert.equal(memory.stocks[0].priceRange.sourceCount, input.sources.length);
    assert.equal(memory.analysisVersion, 6);
    assert.equal(memory.coverage.contributionCount, input.sources.length);
    assert.equal(memory.stocks[0].priceRange.isCost, false);
    return memory;
  } finally { spool.dispose(); fs.rmSync(root, { recursive: true, force: true }); }
}

for (const [label, observations] of [
  ['wide complete first', [[0.9, 2.1], [1, 2]]],
  ['narrow complete first', [[1, 2], [0.9, 2.1]]],
  ['small complete difference', [[1, 2], [0.9999999, 2.0000001]]],
  ['small complete difference reversed', [[0.9999999, 2.0000001], [1, 2]]],
  ['low-only complete difference', [[1, 2], [0.9, 2]]],
  ['high-only complete difference', [[1, 2.1], [1, 2]]],
]) {
  test(`${label}: conflicting complete sources stay partial in memory and disk analysis`, () => {
    const input = fixture(observations), result = analyzeBoth(input), price = result.stocks[0].priceRange;
    assert.equal(price.conflictingObservations, true);
    assert.equal(price.completeness, 'partial');
    assert.equal(price.low, Math.min(...observations.map(row => row[0])));
    assert.equal(price.high, Math.max(...observations.map(row => row[1])));
    assert.equal(result.coverage.completePriceStocks, 0);
    assert.equal(result.coverage.incompletePriceStocks, 1);
    const reuse = reuseCompletePrices(Object.values(input.docs).flatMap(doc => doc.funds), {
      phase, now, recoverPrice: () => assert.fail('conflicting sources cannot propagate') });
    assert.equal(reuse.conflictingPriceKeyCount, 1);
  });
}

test('matching complete intervals accept narrower partial observations in either order', () => {
  for (const observations of [[[1, 2], [1, 2], [1.2, 1.8, false]], [[1.2, 1.8, false], [1, 2], [1, 2]]]) {
    const result = analyzeBoth(fixture(observations)), price = result.stocks[0].priceRange;
    assert.equal(price.conflictingObservations, false);
    assert.equal(price.completeness, 'complete');
    assert.equal(price.low, 1); assert.equal(price.high, 2);
    assert.equal(result.coverage.completePriceStocks, 1);
    assert.equal(result.coverage.incompletePriceStocks, 0);
  }
});

test('partial observations outside a complete interval remain unresolved, including small differences', () => {
  for (const outside of [[0.9, 1.8, false], [1.2, 2.1, false], [0.9999999, 2, false]]) {
    for (const observations of [[[1, 2], outside], [outside, [1, 2]]]) {
      const result = analyzeBoth(fixture(observations)), price = result.stocks[0].priceRange;
      assert.equal(price.conflictingObservations, true);
      assert.equal(price.completeness, 'partial');
      assert.equal(price.low, Math.min(1, outside[0]));
      assert.equal(price.high, Math.max(2, outside[1]));
      assert.equal(result.coverage.completePriceStocks, 0);
    }
  }
});
