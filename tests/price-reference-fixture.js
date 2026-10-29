'use strict';
// Synthetic exchange sessions only; never contacts a market-data endpoint.
const { priceRange } = require('../lib/fund-analysis');
const { expectedSessions, calendar } = require('../lib/price-coverage');
const { buildSnapshot } = require('../scripts/build-data');
const { conflictFor } = require('../lib/price-consistency');
const { buildReference, factFor } = require('../lib/price-range-reference');
const now = '2026-10-29T12:00:00Z', fetchedAt = '2026-10-28T10:00:00.000001Z';
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const identity = { market: 'CN', stockCode: '600001', currency: 'CNY', rangeStart: '2026-04-01',
  rangeEnd: phase.targetPeriodEnd, calendarId: calendar.id };
const holding = { market: 'CN', stockCode: '600001', stockName: '合成证券', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 };
const source = provider => provider === 'eastmoney' ? 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + new URLSearchParams({
  secid: '1.600001', klt: '101', fqt: '0', beg: '20260401', end: '20260630', fields1: 'f1,f2,f3,f4,f5,f6',
  fields2: 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61' }) : provider === 'yahoo' ?
  'https://query1.finance.yahoo.com/v8/finance/chart/600001.SS?' + new URLSearchParams({
    period1: String(Date.parse('2026-04-01T00:00:00Z') / 1000), period2: String(Date.parse('2026-07-01T00:00:00Z') / 1000),
    interval: '1d', events: 'splits', includeAdjustedClose: 'false' }) : 'https://example.org/prices';
function series(provider, low = 73.46, high = 100.1, partial = false) {
  return { ...holding, priceBasis: 'unadjusted', requestedStart: identity.rangeStart, requestedEnd: identity.rangeEnd,
    sourceUrl: source(provider), sourceUrls: [source(provider)], sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: expectedSessions('CN', identity.rangeStart, identity.rangeEnd).slice(partial ? 1 : 0)
      .map(date => ({ date, low, high, close: low })) };
}
function price(provider = 'eastmoney', low = 73.46, high = 100.1, partial = false) {
  return { ...priceRange(holding, series(provider, low, high, partial), { start: identity.rangeStart, end: identity.rangeEnd }), fetchedAt, dataStatus: 'fresh' };
}
function fixture() {
  const prices = [price(), price('yahoo', Math.fround(73.46), Math.fround(100.1))];
  const facts = prices.map(factFor), reference = buildReference(facts, identity, now), witness = conflictFor(facts, identity, now);
  return { identity, prices, facts, reference, witness, now, fetchedAt, phase };
}
const catalog = { fundCount: 2, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 2 }],
  funds: ['000001', '000002'].map(code => ({ code, name: '合成基金', companyCode: 'co1', companyName: '合成公司' })) };
const config = { ...phase, funds: catalog.funds };
function snapshot(code, provider) {
  return buildSnapshot({ asOf: '2026-10-28', retrievedAt: fetchedAt, errors: [], barsByStock: {
    'CN:600001': series(provider, provider === 'yahoo' ? Math.fround(73.46) : 73.46, provider === 'yahoo' ? Math.fround(100.1) : 100.1) },
  funds: [{ code, name: '合成基金', company: '合成公司', reports: [
    { periodEnd: phase.targetPeriodEnd, publishedAt: '2026-07-20', kind: 'quarterly', coverage: 'top10', navYuan: 10000,
      sourceUrl: `https://example.org/${code}.pdf`, holdings: [holding] }] }] }, { now, config: { ...config, funds: [{ code }] } });
}
module.exports = { now, fetchedAt, phase, identity, holding, source, price, fixture, catalog, config, snapshot };
if (require.main === module) process.stdout.write(JSON.stringify(fixture()));
