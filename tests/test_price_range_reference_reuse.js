'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { priceRange } = require('../lib/fund-analysis');
const { calendar, expectedSessions } = require('../lib/price-coverage');
const { conflictFor } = require('../lib/price-consistency');
const { buildReference, effectiveReference, factFor } = require('../lib/price-range-reference');
const { createPriceIndex, addPriceSources, applyPriceIndex, reuseCompletePrices } = require('../lib/reuse-prices');

const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const now = '2026-10-27T00:00:00Z', quoteAt = '2026-10-26T10:00:00.000001Z';
const identity = code => ({ market: 'CN', stockCode: code, currency: 'CNY', rangeStart: '2026-04-01',
  rangeEnd: phase.targetPeriodEnd, calendarId: calendar.id });
function source(provider, code) {
  if (provider === 'eastmoney') return 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + new URLSearchParams({
    secid: '1.' + code, klt: '101', fqt: '0', beg: '20260401', end: '20260630',
    fields1: 'f1,f2,f3,f4,f5,f6', fields2: 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61' });
  return 'https://query1.finance.yahoo.com/v8/finance/chart/' + code + '.SS?' + new URLSearchParams({
    period1: String(Date.parse('2026-04-01T00:00:00Z') / 1000),
    period2: String(Date.parse('2026-07-01T00:00:00Z') / 1000),
    interval: '1d', events: 'splits', includeAdjustedClose: 'false' });
}
function fund(code, low, high, provider, stockCode = '600001') {
  const id = identity(stockCode), url = source(provider, stockCode);
  const price = priceRange(id, { ...id, priceBasis: 'unadjusted', requestedStart: id.rangeStart,
    requestedEnd: id.rangeEnd, sourceUrl: url, sourceUrls: [url],
    sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: expectedSessions(id.market, id.rangeStart, id.rangeEnd).map(date => ({ date, low, high, close: low })) },
  { start: id.rangeStart, end: id.rangeEnd });
  Object.assign(price, { fetchedAt: quoteAt, dataStatus: 'fresh' });
  return { code, status: 'ok', retrievedAt: quoteAt, priceUpdatedAt: quoteAt,
    selectedReport: { periodEnd: phase.targetPeriodEnd }, rows: [{ ...id, priceRange: price }],
    summary: { observedPriceCount: 1 } };
}
const recoverPrice = (row, value) => { row.priceRange = structuredClone(value); return true; };
const reuse = (funds, sourceFunds = funds) => reuseCompletePrices(funds, { phase, now, recoverPrice, sourceFunds });
const price = value => value.rows[0].priceRange;
const withoutReference = value => {
  const copy = structuredClone(value);
  for (const row of copy.rows) delete row.priceRange.rangeReference;
  return copy;
};

test('different complete Eastmoney facts in one binary32 bucket survive deduplication and the next merge in all orders', () => {
  const a = fund('000001', 50.02, 100.1, 'eastmoney');
  const b = fund('000002', Math.fround(50.02), Math.fround(100.1), 'yahoo');
  const c = fund('000003', 50.02 + 1e-10, 100.1, 'eastmoney');
  assert.notEqual(price(a).low, price(c).low);
  assert.equal(Math.fround(price(a).low), Math.fround(price(c).low));
  const permutations = [[a, b, c], [a, c, b], [b, a, c], [b, c, a], [c, a, b], [c, b, a]];
  for (const order of permutations) {
    const label = order.map(item => item.code).join(','), retained = structuredClone(a), original = structuredClone(retained);
    // Only one independent portfolio survives, so its durable evidence must
    // retain the material difference after the other source rows disappear.
    const first = reuse([retained], structuredClone(order));
    assert.equal(first.conflictingPriceKeyCount, 1, label + ': first pass');
    assert.equal(effectiveReference(price(retained), identity('600001'), now), null, label);
    assert.ok(price(retained).globalPriceConflict, label);
    assert.equal(buildReference(price(retained).globalPriceConflict.facts, identity('600001'), now), null,
      label + ': durable witness must contain the material difference');
    const afterFirst = structuredClone(retained);
    const second = reuse([retained]);
    assert.equal(second.conflictingPriceKeyCount, 1, label + ': second pass');
    assert.equal(effectiveReference(price(retained), identity('600001'), now), null, label);
    assert.equal(price(retained).rangeReference, undefined, label);
    assert.deepEqual(retained, afterFirst, label + ': repeated merge is stable');
    for (const field of ['low', 'high', 'fetchedAt', 'sourceUrl', 'sourceUrls'])
      assert.deepEqual(price(retained)[field], price(original)[field], label + ': original ' + field);
    assert.equal(retained.priceUpdatedAt, original.priceUpdatedAt, label);
  }
});

test('a derived reference preserves raw quotes, witness and clocks and is idempotent', () => {
  const funds = [fund('000001', 73.46, 100.1, 'eastmoney'),
    fund('000002', Math.fround(73.46), Math.fround(100.1), 'yahoo')];
  const witness = conflictFor(funds.map(value => factFor(price(value))), identity('600001'), now);
  assert.ok(witness);
  for (const value of funds) price(value).globalPriceConflict = structuredClone(witness);
  const original = structuredClone(funds), first = reuse(funds);
  assert.equal(first.conflictingPriceKeyCount, 0);
  assert.equal(first.referenceAnnotatedRowCount, 2);
  assert.equal(first.referenceAnnotatedFundCount, 2);
  for (let index = 0; index < funds.length; index++) {
    assert.ok(effectiveReference(price(funds[index]), identity('600001'), now));
    assert.deepEqual(withoutReference(funds[index]), original[index]);
  }
  const admitted = structuredClone(funds), second = reuse(funds);
  assert.equal(second.referenceAnnotatedRowCount, 0);
  assert.equal(second.reusedPriceFundCount, 0);
  assert.deepEqual(second.mutatedFunds, []);
  assert.deepEqual(funds, admitted);
});

test('a sealed price index applies further funds by key without rescanning all keys', () => {
  const a = fund('000001', 73.46, 100.1, 'eastmoney');
  const b = fund('000002', Math.fround(73.46), Math.fround(100.1), 'yahoo');
  const unrelated = Array.from({ length: 30 }, (_, index) => fund(String(index + 3).padStart(6, '0'),
    10, 20, 'eastmoney', String(600002 + index)));
  const index = createPriceIndex(phase, now);
  addPriceSources(index, [a, b, ...unrelated]);
  applyPriceIndex(index, [], recoverPrice);
  assert.equal(index.sealed, true);
  const unexpectedScan = () => { throw new Error('SEALED_INDEX_RESCANNED'); };
  index.entries[Symbol.iterator] = unexpectedScan;
  index.entries.values = unexpectedScan;
  index.entries.keys = unexpectedScan;
  index.entries.entries = unexpectedScan;
  index.entries.forEach = unexpectedScan;
  for (const value of [a, b, ...unrelated]) {
    const result = applyPriceIndex(index, [value], recoverPrice);
    assert.equal(result.eligiblePriceKeyCount, 31);
    assert.equal(result.conflictingPriceKeyCount, 0);
  }
});
