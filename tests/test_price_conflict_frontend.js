'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { priceDescription, priceConflictSources, exportCsv } = require('../web/app');
const { priceRange } = require('../lib/fund-analysis');
const { expectedSessions } = require('../lib/price-coverage');

function completeRange() {
  const interval = { start: '2026-04-01', end: '2026-06-30' };
  return priceRange({ market: 'CN', stockCode: '600001', currency: 'CNY' }, {
    market: 'CN', stockCode: '600001', currency: 'CNY', priceBasis: 'unadjusted',
    requestedStart: interval.start, requestedEnd: interval.end,
    sourceUrl: 'https://example.org/prices', sourceUrls: ['https://example.org/prices'],
    sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: expectedSessions('CN', interval.start, interval.end).map(date => ({ date, low: 10, high: 20, close: 15 }))
  }, interval);
}

test('conflict metadata never displays a calendar-complete source as accepted complete', () => {
  const original = completeRange();
  assert.equal(priceDescription(original).complete, true);
  for (const metadata of [null, false, {}, { status: 'unresolved' }]) {
    const range = { ...original, globalPriceConflict: metadata };
    const view = priceDescription(range);
    assert.equal(view.observed, true);
    assert.equal(view.complete, false);
    assert.equal(view.conflictPending, true);
    assert.match(view.note, /来源冲突待核验/);
    assert.equal(view.value, priceDescription(original).value);
    assert.equal(range.low, original.low);
    assert.equal(range.high, original.high);
  }
});

test('CSV retains original extrema and labels unresolved conflicts separately from missing sessions', () => {
  const range = { ...completeRange(), globalPriceConflict: { status: 'unresolved' } };
  const csv = exportCsv([{ code: '000001', name: '合成基金', managers: [],
    rows: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY',
      change: { kind: 'unchanged' }, position: {}, priceRange: range }] }]);
  assert.match(csv, /"来源冲突待核验"/);
  assert.match(csv, /"10","20"/);
  assert.match(csv, /区间价格不代表持有成本/);
});

test('source comparisons preserve original observation times and exclude unrelated or unsafe facts', () => {
  const range = completeRange();
  const original = '2026-10-26T10:00:00Z';
  const fact = { price: { ...range }, sourceUrl: 'https://example.org/quote-a', fetchedAt: original };
  range.globalPriceConflict = { facts: [fact, { ...fact, price: { ...range, low: 9 }, sourceUrl: 'https://example.org/quote-b' }] };
  const result = priceConflictSources(range);
  assert.equal(result.length, 2);
  assert.equal(result[0].fetchedAt, original);
  assert.match(result[1].interval, /^9/);
  for (const changed of [{ sourceUrl: 'javascript:alert(1)' }, { fetchedAt: 'yesterday' },
    { price: { ...range, rangeEnd: '2026-09-30' } }, { price: { ...range, stockCode: '600002' } }]) {
    assert.equal(priceConflictSources({ ...range, globalPriceConflict: { facts: [{ ...fact, ...changed }, fact] } }).length, 1);
  }
  assert.deepEqual(priceConflictSources({ ...range, globalPriceConflict: { facts: [fact, fact, fact] } }), []);
});
