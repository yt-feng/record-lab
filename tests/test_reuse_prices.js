'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { priceRange } = require('../lib/fund-analysis');
const { expectedSessions, validCompletePrice } = require('../lib/price-coverage');
const { reuseCompletePrices } = require('../lib/reuse-prices');
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const interval = { start: '2026-04-01', end: phase.targetPeriodEnd };
const now = '2026-10-26T12:00:00Z', reportAt = '2026-10-25T10:00:00Z', quoteAt = '2026-10-26T10:00:00Z';
const clone = value => structuredClone(value);
const holding = { market: 'CN', stockCode: '600001', currency: 'CNY', shares: 100, weightPct: 5 };
function range(kind = 'complete', identity = holding) {
  const days = expectedSessions(identity.market, interval.start, interval.end);
  const series = kind === 'missing' ? null : { ...identity, priceBasis: 'unadjusted', requestedStart: interval.start,
    requestedEnd: interval.end, sourceUrl: 'https://example.org/prices', sourceUrls: ['https://example.org/prices'],
    sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: (kind === 'partial' ? days.slice(0, 1) : days).map(date => ({ date, low: 10, high: 20, close: 15 })) };
  const price = priceRange(identity, series, interval);
  if (series) Object.assign(price, { fetchedAt: quoteAt, dataStatus: 'fresh' });
  return price;
}
function fund(code, kind = 'complete', identity = holding) {
  const price = range(kind, identity);
  return { code, companyId: `company-${code}`, status: 'ok', asOf: '2026-10-25', retrievedAt: reportAt,
    selectedReport: { periodEnd: phase.targetPeriodEnd, strategyEvidenceRetrievedAt: reportAt, sourceUrl: 'https://example.org/report.pdf' },
    managerEvidenceRetrievedAt: reportAt, reportManagers: [{ name: '合成经理' }], priceUpdatedAt: kind === 'missing' ? undefined : quoteAt,
    rows: [{ ...identity, current: { shares: 100 }, previous: null, priceRange: price }], summary: { observedPriceCount: kind === 'missing' ? 0 : 1 } };
}
function recoverPrice(row, price, bound) {
  assert.equal(bound, price.fetchedAt);
  row.priceRange = clone(price);
  return true;
}
function reuse(funds, options = {}) { return reuseCompletePrices(funds, { phase, now, recoverPrice, ...options }); }
const complete = row => validCompletePrice(row.priceRange, row.market, row.stockCode, interval.start, interval.end);
const withoutConflict = value => { const result = clone(value); for (const item of Array.isArray(result) ? result : [result]) for (const row of item.rows) delete row.priceRange.globalPriceConflict; return result; };

test('same security-quarter is reused across companies without modifying report or manager evidence', () => {
  const donor = fund('000001'), target = fund('000002', 'missing'), original = clone(target), beforeDonor = clone(donor);
  const result = reuse([donor, target]);
  assert.equal(result.reusedPriceRowCount, 1); assert.equal(result.reusedPriceFundCount, 1);
  assert.deepEqual(result.mutatedFunds, [target]); assert.equal(complete(target.rows[0]), true);
  assert.equal(target.rows[0].priceRange.dataStatus, 'cached'); assert.equal(target.rows[0].priceRange.fetchedAt, quoteAt);
  assert.equal(target.priceUpdatedAt, quoteAt); assert.equal(target.summary.observedPriceCount, 1);
  for (const key of ['asOf', 'retrievedAt', 'selectedReport', 'reportManagers', 'managerEvidenceRetrievedAt']) assert.deepEqual(target[key], original[key]);
  assert.deepEqual(donor, beforeDonor);
  assert.deepEqual(reuse([donor, target]).mutatedFunds, []);
});

test('partial and prior-only target rows can upgrade, while an existing complete target remains exact', () => {
  const donor = fund('000001'), partial = fund('000002', 'partial'), completeTarget = fund('000003');
  partial.status = 'no-equities'; partial.rows[0].previous = partial.rows[0].current; partial.rows[0].current = null;
  const before = clone(completeTarget);
  assert.equal(reuse([donor, partial, completeTarget]).reusedPriceRowCount, 1);
  assert.equal(complete(partial.rows[0]), true); assert.equal(partial.rows[0].current, null);
  assert.deepEqual(completeTarget, before);
});

test('conflicting complete sources block propagation regardless of input ordering', () => {
  const a = fund('000001'), b = fund('000002'), target = fund('000003', 'missing');
  b.rows[0].priceRange.low = 9;
  for (const order of [[a, b, target], [target, b, a]]) {
    const records = clone(order), result = reuse(records);
    assert.equal(result.conflictingPriceKeyCount, 1); assert.equal(result.reusedPriceRowCount, 0);
    assert.deepEqual(withoutConflict(records), order);
    assert.ok(records.every(item => item.rows[0].priceRange.globalPriceConflict));
  }
});

test('a valid partial extreme outside the complete source range is retained as a conflict', () => {
  for (const change of [{ low: 9 }, { high: 21 }]) {
    const donor = fund('000001'), target = fund('000002', 'partial');
    Object.assign(target.rows[0].priceRange, change);
    const before = clone(target), result = reuse([donor, target]);
    assert.equal(result.partialConflictRowCount, 1); assert.equal(result.reusedPriceRowCount, 0);
    assert.deepEqual(withoutConflict(target), before);
    assert.ok(target.rows[0].priceRange.globalPriceConflict);
  }
});

test('market, instrument, currency and exact window are not interchangeable', () => {
  const donor = fund('000001');
  const targets = [fund('000002', 'missing', { ...holding, stockCode: '600002' }),
    fund('000003', 'missing', { ...holding, market: 'HK', stockCode: '00700', currency: 'HKD' })];
  for (const mutation of [{ currency: 'USD' }, { rangeStart: '2026-04-02' }, { rangeEnd: '2026-09-30' }]) {
    const target = fund('000004', 'missing'); Object.assign(target.rows[0].priceRange, mutation); targets.push(target);
  }
  const wrongQuarter = fund('000005', 'missing'); wrongQuarter.selectedReport.periodEnd = '2026-09-30'; targets.push(wrongQuarter);
  assert.equal(reuse([donor, ...targets]).reusedPriceRowCount, 0);
  assert.equal(reuse([donor, fund('000006', 'missing')], { phase: { ...phase, targetPeriodEnd: '2026-06-29' } }).reusedPriceRowCount, 0);
});

test('incomplete, invalid, private or unstamped donor facts cannot propagate', () => {
  const mutations = [{ coverageVersion: 1 }, { observedDateSetHash: '0'.repeat(64) }, { rejectedRowCount: 1 },
    { priceBasis: 'adjusted' }, { sourceUrl: 'https://localhost/quote' }, { sourceUrl: 'https://127.0.0.1/quote' },
    { sourceUrl: 'https://example.internal/quote' }, { sourceUrls: ['https://example.org/?token='] },
    { fetchedAt: null }, { fetchedAt: '2026-10-27T10:00:00Z' }, { fetchedAt: '2026-06-29T10:00:00Z' }];
  for (const mutation of mutations) {
    const donor = fund('000001'), target = fund('000002', 'missing'); Object.assign(donor.rows[0].priceRange, mutation);
    assert.equal(reuse([donor, target]).reusedPriceRowCount, 0, JSON.stringify(mutation));
  }
  assert.equal(reuse([fund('000001', 'partial'), fund('000002', 'missing')]).reusedPriceRowCount, 0);
  const unbound = fund('000001'); delete unbound.priceUpdatedAt;
  assert.equal(reuse([unbound, fund('000002', 'missing')]).reusedPriceRowCount, 0);
});

test('consistent sources choose the newest actual timestamp and preserve existing later fund price time', () => {
  const a = fund('000001'), b = fund('000002'), target = fund('000003', 'missing');
  a.rows[0].priceRange.fetchedAt = '2026-10-26T17:30:00+08:00'; // 09:30Z, earlier despite its lexical order.
  a.rows[0].priceRange.sourceUrl = 'https://example.org/older';
  b.rows[0].priceRange.sourceUrl = 'https://example.org/newer';
  target.priceUpdatedAt = '2026-10-26T11:00:00Z';
  const result = reuse([a, b, target]);
  assert.equal(result.reusedPriceRowCount, 1); assert.equal(target.rows[0].priceRange.sourceUrl, 'https://example.org/newer');
  assert.equal(target.rows[0].priceRange.fetchedAt, quoteAt); assert.equal(target.priceUpdatedAt, '2026-10-26T11:00:00Z');
});

test('callback rejection or loss of proof cannot partially mutate the target', () => {
  for (const callback of [(row) => { row.priceRange.low = 1; return false; },
    (row, value) => { row.priceRange = { ...value, observedDateSetHash: '0'.repeat(64) }; return true; },
    (row, value) => { row.priceRange = { ...value, low: 1 }; return true; }]) {
    const donor = fund('000001'), target = fund('000002', 'missing'), before = clone(target);
    assert.equal(reuse([donor, target], { recoverPrice: callback }).reusedPriceRowCount, 0);
    assert.deepEqual(target, before);
  }
});

test('all pre-deduplication sources are checked but counts include only final published targets', () => {
  const donor = fund('000001'), discardedTarget = fund('000002', 'missing'), target = fund('000003', 'missing');
  const before = clone(discardedTarget);
  const result = reuse([target], { sourceFunds: [donor, discardedTarget, target] });
  assert.equal(result.reusedPriceFundCount, 1); assert.equal(result.reusedPriceRowCount, 1);
  assert.deepEqual(discardedTarget, before); assert.equal(complete(target.rows[0]), true);
  const conflict = fund('000004'); conflict.rows[0].priceRange.low = 9;
  const other = fund('000005', 'missing');
  assert.equal(reuse([other], { sourceFunds: [donor, conflict] }).conflictingPriceKeyCount, 1);
  assert.equal(complete(other.rows[0]), false);
});

const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const catalog = { companies: [{ code: 'co1', name: '合成甲', fundCount: 1 }, { code: 'co2', name: '合成乙', fundCount: 1 }],
  funds: [{ code: '000001', companyCode: 'co1' }, { code: '000002', companyCode: 'co2' }] };
const config = { ...phase, funds: catalog.funds };
function snapshot(code, kind, at) {
  const company = code === '000001' ? 'co1' : 'co2';
  const reports = [['2026-06-30', '2026-07-20'], ['2026-03-31', '2026-04-20']].map(([periodEnd, publishedAt]) => ({
    periodEnd, publishedAt, kind: 'quarterly', coverage: 'top10', navYuan: 10000,
    sourceUrl: `https://example.org/${code}-${periodEnd}.pdf`, holdings: [holding] }));
  const days = expectedSessions('CN', interval.start, interval.end);
  const barsByStock = kind === 'missing' ? {} : { 'CN:600001': { market: 'CN', stockCode: '600001', currency: 'CNY',
    priceBasis: 'unadjusted', requestedStart: interval.start, requestedEnd: interval.end, sourceUrl: 'https://example.org/prices',
    retrievedAt: at, sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: (kind === 'partial' ? days.slice(0, 1) : days).map(date => ({ date, low: 10, high: 20, close: 15 })) } };
  return buildSnapshot({ asOf: at.slice(0, 10), retrievedAt: at, barsByStock, errors: [],
    funds: [{ code, name: '合成基金', companyId: company, reports }] }, { config, catalog, now });
}
function retainedPair(targetKind = 'missing') {
  const donor = snapshot('000001', 'complete', quoteAt), target = snapshot('000002', targetKind, reportAt);
  const first = mergeSnapshots({ snapshots: [donor], config, catalog, now });
  first.companySnapshots.co2 = target;
  return { first, target: target.funds[0] };
}

test('a global disagreement lowers fund receipts and Python quality without rewriting source facts or spending old lanes', () => {
  const a = snapshot('000001', 'complete', quoteAt), b = snapshot('000002', 'complete', quoteAt);
  b.funds[0].rows[0].priceRange.low = 9;
  const original = clone([a.funds[0], b.funds[0]]);
  const merged = mergeSnapshots({ snapshots: [a, b], config, catalog, now, receiptCodes: ['000001', '000002'] });
  const after = Object.values(merged.companySnapshots).flatMap(item => item.funds);
  assert.equal(merged.counts.conflictingPriceKeyCount, 1);
  assert.equal(merged.counts.conflictMarkedFundCount, 2);
  assert.equal(merged.counts.reusedPriceFundCount, 0);
  assert.equal(merged.manifest.coverage.priceComplete, 0);
  assert.deepEqual(merged.receipt.funds.map(item => [item.priceCoverage.priceComplete, item.priceCoverage.priceConflicted, item.priceCoverage.missingStocks]), [[0, 1, []], [0, 1, []]]);
  for (let i = 0; i < after.length; i++) {
    assert.equal(complete(after[i].rows[0]), true);
    assert.deepEqual(withoutConflict(after[i]).rows, original[i].rows);
    for (const key of ['retrievedAt', 'selectedReport', 'reportManagers']) assert.deepEqual(after[i][key], original[i][key]);
  }
  const script = `import json,sys,datetime as dt
sys.path.insert(0,'scripts')
from traversal import price_coverage, report_quality, normalize_enrichment, normalize_quality, apply_coverage, enrichment_eligible, quality_eligible, merge_quality_observation, QUALITY_FIELDS, MANAGER_TABLE_COVERAGE_VERSION, STRATEGY_SECTION_COVERAGE_VERSION
v=json.load(sys.stdin); now=dt.datetime.fromisoformat(v['now'].replace('Z','+00:00'))
fund=v['fund']; coverage=price_coverage(fund,now)
assert coverage['priceComplete']==0 and coverage['priceConflicted']==1 and coverage['missingStocks']==[]
q=report_quality(fund,v['phase'],now); assert q['fields']['prices']=='partial' and q['priceConflictOnly']
prior={**coverage,'attempts':2,'totalAttempts':5,'lastAttemptAt':'2026-10-25T10:00:00Z'}
enrichment=normalize_enrichment(prior,'complete',now)
assert enrichment['status']=='conflict' and enrichment['attempts']==2 and enrichment['totalAttempts']==5
oldq={**q,'fields':{key:'complete' for key in QUALITY_FIELDS},'attempts':2,'totalAttempts':7,'managerAttempts':1,'strategyAttempts':1}
oldq['fields']['prices']='partial'
quality=normalize_quality(oldq,'complete',now)
assert quality['status']=='conflict' and quality['attempts']==2 and quality['managerAttempts']==1 and quality['strategyAttempts']==1
state={'status':'complete','attribution':'direct','representativeCode':fund['code'],'code':fund['code'],'enrichment':enrichment,'quality':quality}
assert enrichment_eligible({fund['code']:state})==[] and quality_eligible({fund['code']:state})==[]
late={**coverage,'priceComplete':1,'priceConflicted':0,'conflictingStocks':[]}
apply_coverage(state,late)
assert state['enrichment']['status']=='conflict' and state['enrichment']['priceComplete']==0 and state['enrichment']['attempts']==2
assert merge_quality_observation(quality,{**q,'fields':{key:'complete' for key in QUALITY_FIELDS},'priceConflictOnly':False})['fields']['prices']=='partial'
print('conflicts-isolated')`;
  const output = require('node:child_process').execFileSync('python3', ['-c', script], { cwd: require('node:path').resolve(__dirname, '..'), encoding: 'utf8', input: JSON.stringify({ fund: after[0], phase, now }) });
  assert.equal(output.trim(), 'conflicts-isolated');
  const replay = mergeSnapshots({ previousManifest: merged.manifest, previousCompanies: merged.companySnapshots, config, catalog, now });
  assert.equal(replay.changed, false); assert.equal(replay.counts.conflictingPriceKeyCount, 1);
});

test('retained conflict witnesses cannot self-clear when only one original fund remains published', () => {
  const a = fund('000001'), b = fund('000002'); b.rows[0].priceRange.low = 9;
  reuse([a, b]);
  const retained = clone(a), before = clone(retained);
  const result = reuse([retained]);
  assert.equal(result.conflictingPriceKeyCount, 1); assert.equal(result.conflictMarkedRowCount, 0);
  assert.deepEqual(retained, before);
});

test('a malformed partial extreme cannot hide the valid partial witness from an earlier partition', () => {
  const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
  const completeFund = fund('000001'), bad = fund('000002', 'partial'), valid = fund('000003', 'partial');
  bad.rows[0].priceRange.low = 1; bad.rows[0].priceRange.observedDateSetHash = '0'.repeat(64);
  valid.rows[0].priceRange.low = 9;
  const index = createPriceIndex(phase, now);
  addPriceSources(index, [bad, valid]); addPriceSources(index, [completeFund]);
  assert.equal(applyPriceIndex(index, [completeFund], recoverPrice).conflictingPriceKeyCount, 1);
  assert.deepEqual(completeFund.rows[0].priceRange.globalPriceConflict.facts.map(item => item.price.low).sort((a, b) => a - b), [9, 10]);
});

test('missing local quotes and fresh replacements retain an existing unresolved key witness', () => {
  const a = snapshot('000001', 'complete', quoteAt), b = snapshot('000002', 'complete', quoteAt);
  b.funds[0].rows[0].priceRange.low = 9;
  const first = mergeSnapshots({ snapshots: [a, b], config, catalog, now });
  const retained = clone(first.companySnapshots.co1), marker = retained.funds[0].rows[0].priceRange.globalPriceConflict;
  retained.funds[0].rows[0].priceRange = { ...range('missing'), globalPriceConflict: marker };
  retained.funds[0].summary.observedPriceCount = 0;
  const replay = mergeSnapshots({ previousManifest: first.manifest, previousCompanies: { co1: retained }, config, catalog, now });
  assert.equal(replay.counts.conflictingPriceKeyCount, 1); assert.equal(replay.counts.conflictMarkedFundCount, 0);
  const fresh = snapshot('000001', 'complete', '2026-10-26T11:00:00Z');
  const replaced = mergeSnapshots({ snapshots: [fresh], previousManifest: first.manifest,
    previousCompanies: { co1: first.companySnapshots.co1 }, config, catalog, now });
  assert.equal(replaced.manifest.coverage.priceComplete, 0);
  assert.ok(replaced.companySnapshots.co1.funds[0].rows[0].priceRange.globalPriceConflict);
  assert.equal(replaced.companySnapshots.co1.funds[0].retrievedAt, fresh.retrievedAt);
  assert.equal(replaced.companySnapshots.co1.funds[0].rows[0].priceRange.fetchedAt, fresh.funds[0].rows[0].priceRange.fetchedAt);
});

test('partial extreme in a later source partition marks earlier complete source and all missing recipients', () => {
  const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
  const a = fund('000001'), b = fund('000002', 'partial'), target = fund('000003', 'missing');
  b.rows[0].priceRange.low = 9;
  const index = createPriceIndex(phase, now);
  addPriceSources(index, [target, a]); addPriceSources(index, [b]);
  for (const item of [a, b, target]) {
    const before = clone(item), result = applyPriceIndex(index, [item], recoverPrice);
    assert.equal(result.conflictingPriceKeyCount, 1); assert.equal(result.reusedPriceRowCount, 0);
    assert.deepEqual(withoutConflict(item), before);
    assert.deepEqual(item.rows[0].priceRange.globalPriceConflict.types, ['partial-extreme']);
  }
});

test('reuse alone publishes changed company pages and truthful receipt coverage without request receipts', () => {
  const { first, target } = retainedPair(), before = clone(target);
  const merged = mergeSnapshots({ previousManifest: first.manifest, previousCompanies: first.companySnapshots,
    config, catalog, now, receiptCodes: ['000002'] });
  assert.equal(merged.changed, true); assert.equal(merged.counts.acceptedFreshFundCount, 0);
  assert.equal(merged.counts.acceptedPricePatchCount, 0); assert.equal(merged.counts.reusedPriceRowCount, 1);
  assert.equal(merged.counts.reusedPriceFundCount, 1); assert.deepEqual(merged.receipt.pricePatches, []);
  assert.equal(merged.receipt.funds[0].priceCoverage.priceComplete, 1);
  assert.equal(merged.receipt.funds[0].attempted, undefined);
  const after = merged.companySnapshots.co2.funds[0];
  assert.equal(after.priceUpdatedAt, quoteAt); assert.equal(after.rows[0].priceRange.fetchedAt, quoteAt);
  assert.equal(after.rows[0].priceRange.dataStatus, 'cached');
  assert.equal(after.retrievedAt, before.retrievedAt); assert.equal(after.asOf, before.asOf);
  assert.deepEqual(after.selectedReport, before.selectedReport); assert.deepEqual(after.previousReport, before.previousReport);
  const replay = mergeSnapshots({ previousManifest: merged.manifest, previousCompanies: merged.companySnapshots,
    config, catalog, now, receiptCodes: ['000002'] });
  assert.equal(replay.changed, false); assert.equal(replay.counts.reusedPriceRowCount, 0);
  assert.equal(replay.receipt.funds[0].priceCoverage.priceComplete, 1);
});

test('a conflicting retained complete source publishes only the conflict metadata', () => {
  const { first } = retainedPair('complete');
  first.companySnapshots.co2.funds[0].rows[0].priceRange.low = 9;
  const merged = mergeSnapshots({ previousManifest: first.manifest, previousCompanies: first.companySnapshots, config, catalog, now });
  assert.equal(merged.changed, true); assert.equal(merged.counts.conflictingPriceKeyCount, 1);
  assert.equal(merged.counts.conflictMarkedFundCount, 2); assert.equal(merged.manifest.coverage.priceComplete, 0);
  assert.equal(merged.counts.reusedPriceRowCount, 0); assert.deepEqual(merged.receipt.pricePatches, []);
});

test('a partial target conflict survives the actual publication recovery callback', () => {
  const { first } = retainedPair('partial');
  first.companySnapshots.co2.funds[0].rows[0].priceRange.high = 21;
  const merged = mergeSnapshots({ previousManifest: first.manifest, previousCompanies: first.companySnapshots,
    config, catalog, now, receiptCodes: ['000002'] });
  assert.equal(merged.changed, true); assert.equal(merged.counts.partialConflictRowCount, 1);
  assert.equal(merged.receipt.funds[0].priceCoverage.priceComplete, 0);
});

test('Python traversal hydrates reused complete prices without incrementing any attempt budget', () => {
  const { first, target } = retainedPair();
  const merged = mergeSnapshots({ previousManifest: first.manifest, previousCompanies: first.companySnapshots, config, catalog, now });
  const script = `import json,sys
sys.path[:0]=['scripts','tests']
import traversal,test_traversal as support
p=json.load(sys.stdin)
config={**p['config'],'requireReportQuality':True}
event=support.event('000002',at=p['before']['retrievedAt'],company='co2')
previous=traversal.merge_progress(p['catalog'],None,config,[event],p['now'],retained=[p['before']])
prior=next(s for s in previous['funds'] if s['code']=='000002')
updated=traversal.merge_progress(p['catalog'],previous,config,[],p['now'],retained=[p['after']])
state=next(s for s in updated['funds'] if s['code']=='000002')
assert state['enrichment']['priceComplete']==1
assert state['quality']['fields']['prices']=='complete'
for field in ('totalAttempts','lastAttemptAt'): assert state[field]==prior[field]
for field in ('attempts','totalAttempts','lastAttemptAt'): assert state['enrichment'][field]==prior['enrichment'][field]
for field in ('attempts','totalAttempts','managerAttempts','managerTotalAttempts','strategyAttempts','strategyTotalAttempts'): assert state['quality'].get(field)==prior['quality'].get(field)
print('hydration-no-new-attempts')`;
  const { execFileSync } = require('node:child_process');
  const output = execFileSync('python3', ['-c', script], { cwd: require('node:path').resolve(__dirname, '..'), encoding: 'utf8',
    input: JSON.stringify({ config, catalog, now, before: target, after: merged.companySnapshots.co2.funds[0] }) });
  assert.equal(output.trim(), 'hydration-no-new-attempts');
});

test('mixed timezone observations retain the latest instant through company envelopes and later price patches', () => {
  const phaseConfig = { ...phase, funds: ['000001', '000002', '000003', '000004']
    .map((code, index) => ({ code, companyCode: index < 2 ? 'co1' : 'co2' })) };
  const directory = { companies: [{ code: 'co1', fundCount: 2 }, { code: 'co2', fundCount: 2 }], funds: phaseConfig.funds };
  const olderOffset = '2026-10-26T17:30:00+08:00'; // 09:30Z, earlier than quoteAt.
  function one(code, stockCode, at, quotes) {
    const identity = { ...holding, stockCode };
    const reports = [['2026-06-30', '2026-07-20'], ['2026-03-31', '2026-04-20']].map(([periodEnd, publishedAt]) => ({
      periodEnd, publishedAt, kind: 'quarterly', coverage: 'top10', sourceUrl: `https://example.org/${code}-${periodEnd}.pdf`,
      holdings: [identity], navYuan: 10000 }));
    const barsByStock = quotes ? { [`CN:${stockCode}`]: { ...identity, priceBasis: 'unadjusted', requestedStart: interval.start,
      requestedEnd: interval.end, sourceUrl: 'https://example.org/quotes', sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
      rows: expectedSessions('CN', interval.start, interval.end).map(date => ({ date, low: 10, high: 20, close: 15 })) } } : {};
    return buildSnapshot({ asOf: at.slice(0, 10), retrievedAt: at, errors: [], barsByStock,
      funds: [{ code, companyId: code < '000003' ? 'co1' : 'co2', reports }] }, { config: phaseConfig, catalog: directory, now });
  }
  const first = mergeSnapshots({ snapshots: [one('000001', '600001', olderOffset, true), one('000002', '600002', quoteAt, true)],
    config: phaseConfig, catalog: directory, now });
  const targets = one('000003', '600001', reportAt, false);
  targets.funds.push(one('000004', '600002', reportAt, false).funds[0]); first.companySnapshots.co2 = targets;
  const merged = mergeSnapshots({ previousManifest: first.manifest, previousCompanies: first.companySnapshots,
    config: phaseConfig, catalog: directory, now });
  assert.equal(merged.counts.reusedPriceRowCount, 2);
  assert.equal(merged.companySnapshots.co2.priceUpdatedAt, quoteAt);
  const replay = mergeSnapshots({ previousManifest: merged.manifest, previousCompanies: merged.companySnapshots,
    config: phaseConfig, catalog: directory, now });
  assert.equal(replay.changed, false); assert.equal(replay.counts.reusedPriceRowCount, 0);
  const target = merged.companySnapshots.co2.funds.find(fund => fund.code === '000003');
  const binding = require('../lib/price-patches').bindingFor(target, phaseConfig);
  const incoming = { schemaVersion: 1, sourceMode: 'actions', asOf: '2026-10-26', retrievedAt: quoteAt, ...phase,
    funds: [], pricePatches: [{ schemaVersion: 1, priceOnly: true, code: target.code, binding,
      attemptedStockKeys: ['CN:600001'], attemptedAt: '2026-10-26T09:59:00Z', prices: [{ market: 'CN', stockCode: '600001',
        priceRange: { ...target.rows[0].priceRange, fetchedAt: quoteAt } }] }] };
  const updated = mergeSnapshots({ snapshots: [incoming], previousManifest: merged.manifest, previousCompanies: merged.companySnapshots,
    config: phaseConfig, catalog: directory, now });
  assert.equal(updated.companySnapshots.co2.funds.find(fund => fund.code === target.code).priceUpdatedAt, quoteAt);
  assert.equal(updated.receipt.pricePatches[0].accepted, true);
});

test('partitioned price reuse matches one pass and isolates stored observations from callers', () => {
  const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
  const donor = fund('000001'), target = fund('000002', 'missing'), target2 = fund('000003', 'partial');
  const expected = clone([target, target2]);
  const completePass = reuse(expected, { sourceFunds: [donor, target, target2] });
  const index = createPriceIndex(phase, now);
  for (const partition of [[target], [donor], [target2]]) addPriceSources(index, partition);
  // Disk-backed callers may release or reuse their input objects after indexing.
  donor.rows[0].priceRange.low = 1;
  const first = applyPriceIndex(index, [target], recoverPrice);
  const second = applyPriceIndex(index, [target2], recoverPrice);
  assert.deepEqual([target, target2], expected);
  assert.equal(first.reusedPriceRowCount + second.reusedPriceRowCount, completePass.reusedPriceRowCount);
  assert.equal(first.eligiblePriceKeyCount, 1); assert.equal(second.eligiblePriceKeyCount, 1);
  assert.throws(() => addPriceSources(index, [fund('000004')]), /PRICE_INDEX_ALREADY_APPLIED/);
});

test('conflicting source in the last input partition blocks reuse in every output partition', () => {
  const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
  const a = fund('000001'), b = fund('000002'); b.rows[0].priceRange.low = 9;
  const index = createPriceIndex(phase, now);
  addPriceSources(index, [a]); addPriceSources(index, []); addPriceSources(index, [b]);
  for (const target of [fund('000003', 'missing'), fund('000004', 'partial')]) {
    const before = clone(target), result = applyPriceIndex(index, [target], recoverPrice);
    assert.equal(result.conflictingPriceKeyCount, 1); assert.equal(result.reusedPriceRowCount, 0);
    assert.deepEqual(withoutConflict(target), before);
  }
});
