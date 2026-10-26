'use strict';
// Synthetic observations under the pinned public-source metadata.
const test = require('node:test');
const assert = require('node:assert/strict');
const { inceptionState, normalizeInceptionConflict, normalizeProfileInception } = require('../lib/historical-inception');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const source = 'https://www.csrc.gov.cn/csrc/c101900/c1029655/1029655/files/公募基金产品索引（截至20260630）.xlsx';
const now = new Date('2026-10-26T12:00:00Z');
const evidence = { schemaVersion: 1, fundCode: '000003', periodEnd: '2026-06-30', date: '2026-04-10',
  sourceId: 'csrc-fund-products-20260630', sourceUrl: source,
  contentHash: '0f6ac234b792bb6334c0c94ec37fa6c0548363c80017c946d11a5c7e95d3a9e0',
  sheet: 'Sheet1', row: 3, retrievedAt: '2026-10-26T00:00:00Z' };
const profile = (date = '2026-07-10', hour = '01') => ({ schemaVersion: 1, fundCode: '000003', date,
  sourceUrl: 'https://fundf10.eastmoney.com/jbgk_000003.html', retrievedAt: '2026-10-26T' + hour + ':00:00Z' });
const historical = { code: '000003', inceptionDate: evidence.date, inceptionSourceUrl: source, inceptionEvidence: evidence };
const conflict = { schemaVersion: 1, fundCode: '000003', periodEnd: '2026-06-30', status: 'conflicting-sources',
  historicalEvidence: evidence, profileEvidence: profile() };
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000003' }] };
const catalog = { companies: [], funds: [{ code: '000003' }] };
const report = { periodEnd: '2026-06-30', publishedAt: '2026-07-21', kind: 'quarterly',
  sourceUrl: 'https://example.org/quarter.pdf', coverage: 'top10', navYuan: 10000,
  holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY', shares: 100, weightPct: 1, marketValueYuan: 100 }] };
function snapshot(fund, hour, previous = null) {
  return buildSnapshot({ asOf: '2026-10-26', retrievedAt: '2026-10-26T' + hour + ':00:00Z', funds: [
    { code: '000003', reports: [report], ...fund }], barsByStock: {}, errors: [] }, { now, config, previous });
}
function merge(part, previous) {
  return mergeSnapshots({ snapshots: [part], now, config, catalog,
    previousManifest: previous?.manifest, previousCompanies: previous?.companySnapshots });
}
const mergedFund = result => Object.values(result.companySnapshots).flatMap(part => part.funds)[0];

test('empty metadata retains original official evidence through build and independent-part merge', () => {
  const old = snapshot(historical, '00');
  const next = snapshot({}, '02', old).funds[0];
  assert.deepEqual(next.inceptionEvidence, evidence);
  assert.equal(next.inceptionDate, evidence.date);
  const first = merge(old);
  const second = merge(snapshot({}, '02'), first);
  assert.deepEqual(mergedFund(second).inceptionEvidence, evidence);
});

test('explicit disagreement clears exemptions and survives empty attempts and historical-only retries', () => {
  const old = snapshot(historical, '00');
  const conflicting = { inceptionProfileEvidence: profile(), inceptionConflict: conflict };
  const built = snapshot(conflicting, '02', old);
  assert.equal(built.funds[0].inceptionDate, null);
  assert.equal(built.funds[0].inceptionEvidence, null);
  assert.deepEqual(built.funds[0].inceptionConflict, conflict);
  const empty = snapshot({}, '03', built);
  assert.deepEqual(empty.funds[0].inceptionConflict, conflict);
  const onlyHistory = snapshot(historical, '04', empty);
  assert.deepEqual(onlyHistory.funds[0].inceptionConflict, conflict);
  const first = merge(old);
  const second = merge(snapshot(conflicting, '02'), first);
  const third = merge(snapshot({}, '03'), second);
  assert.deepEqual(mergedFund(third).inceptionConflict, conflict);
  assert.equal(mergedFund(third).inceptionDate, null);
});

test('a later matching profile resolves disagreement without refreshing historical observation time', () => {
  const previous = { code: '000003', inceptionConflict: conflict, inceptionProfileEvidence: profile() };
  const state = inceptionState({ code: '000003', inceptionProfileEvidence: profile(evidence.date, '03') }, previous, now);
  assert.equal(state.inceptionConflict, null);
  assert.deepEqual(state.inceptionEvidence, evidence);
  assert.equal(state.inceptionProfileEvidence.retrievedAt, '2026-10-26T03:00:00Z');
  const stale = inceptionState({ code: '000003', inceptionProfileEvidence: profile(evidence.date, '00') }, previous, now);
  assert.deepEqual(stale.inceptionConflict, conflict);
  const tied = inceptionState({ code: '000003', inceptionProfileEvidence: profile(evidence.date, '01') }, previous, now);
  assert.deepEqual(tied.inceptionConflict, conflict);
});

test('unrelated code, noncanonical host/path and invalid profile timestamps cannot support inheritance', () => {
  const other = inceptionState({ code: '000004' }, historical, now);
  assert.equal(other.inceptionEvidence, null);
  for (const sourceUrl of ['https://example.org/jbgk_000003.html',
    'https://fundf10.eastmoney.com/extra/jbgk_000003.html',
    'https://fundf10.eastmoney.com/jbgk_000004.html']) {
    assert.equal(normalizeProfileInception({ ...profile(), sourceUrl }, '000003', now), null);
    assert.equal(normalizeInceptionConflict({ ...conflict, profileEvidence: { ...profile(), sourceUrl } }, '000003', now), null);
  }
  assert.equal(normalizeProfileInception({ ...profile(), retrievedAt: '2026-10-27T00:00:00Z' }, '000003', now), null);
  assert.equal(normalizeInceptionConflict(conflict, '000004', now), null);
});

test('all-failed PDF receipt carries only a source conflict without refreshing report or price observations', () => {
  const original = merge(snapshot(historical, '00'));
  const before = mergedFund(original);
  const failed = snapshot({ reports: [], inceptionConflict: conflict, inceptionProfileEvidence: profile() }, '02');
  assert.equal(failed.coverage.batchFreshFundCount, 0);
  assert.equal(failed.funds.length, 0);
  assert.equal(failed.batchResults[0].status, 'failed');
  assert.equal(failed.inceptionPatches.length, 1);
  const updated = merge(failed, original);
  const after = mergedFund(updated);
  assert.equal(updated.counts.acceptedFreshFundCount, 0);
  assert.equal(updated.counts.acceptedInceptionPatchCount, 1);
  assert.equal(after.retrievedAt, before.retrievedAt);
  assert.equal(updated.manifest.retrievedAt, original.manifest.retrievedAt);
  assert.equal(after.priceUpdatedAt, before.priceUpdatedAt);
  assert.deepEqual(after.selectedReport, before.selectedReport);
  assert.deepEqual(after.rows, before.rows);
  assert.deepEqual(after.inceptionConflict, conflict);
  assert.equal(after.inceptionDate, null);
  const consistent = snapshot({ ...historical, reports: [], inceptionProfileEvidence: profile(evidence.date, '03') }, '04');
  assert.equal(consistent.coverage.batchFreshFundCount, 0);
  const resolved = merge(consistent, updated);
  assert.equal(mergedFund(resolved).inceptionConflict, null);
  assert.deepEqual(mergedFund(resolved).inceptionEvidence, evidence);
  assert.equal(mergedFund(resolved).retrievedAt, before.retrievedAt);
  assert.equal(resolved.counts.acceptedFreshFundCount, 0);
});

test('metadata-only checks cannot create funds, transfer to aliases, cross periods, or fabricate profile timestamps', () => {
  const failed = snapshot({ reports: [], inceptionConflict: conflict, inceptionProfileEvidence: profile() }, '02');
  assert.equal(merge(failed).manifest.coverage.fundCount, 0);
  const original = merge(snapshot(historical, '00'));
  for (const changes of [{ code: '000004' }, { targetPeriodEnd: '2026-09-30' }, { observedAt: '2026-10-26T02:00:00Z' },
    { profileEvidence: { ...profile(), retrievedAt: '2026-10-26T03:00:00Z' }, observedAt: '2026-10-26T03:00:00Z' }]) {
    const altered = { ...failed, inceptionPatches: [{ ...failed.inceptionPatches[0], ...changes }] };
    const result = merge(altered, original);
    assert.equal(result.changed, false);
    assert.equal(result.counts.acceptedInceptionPatchCount, 0);
  }
  assert.throws(() => snapshot({ reports: [], ...historical }, '02'), { code: 'NO_VALID_FRESH_FUNDS' });
});
