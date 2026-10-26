'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeLifecycleEvidence } = require('../lib/fund-analysis');
const { buildSnapshot, isValidNotApplicable } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const ui = require('../web/app');
const fixture = require('./fixtures/lifecycle.json');
const clone = value => JSON.parse(JSON.stringify(value));
const options = { code: '000001', periodEnd: '2026-06-30', asOf: '2026-09-29' };
const config = { funds: [{ code: '000001' }], targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const now = '2026-09-29T12:00:00Z';

test('shared lifecycle validator preserves original proof and never invents termination date', () => {
  assert.deepEqual(normalizeLifecycleEvidence(fixture, options), fixture);
  assert.equal(isValidNotApplicable(fixture, { ...options, asOf: options.asOf }), true);
  const value = normalizeLifecycleEvidence(fixture, options);
  assert.equal(value.dateBasis, 'lastOperationDate');
  assert.equal(value.terminationDate, null);
  assert.equal(value.lastOperationDate, '2026-05-07');
});

test('rejects conditional, distributor, missing-proof and wrong-period lifecycle claims', () => {
  for (const quote of ['若未达到资产标准，本基金基金合同自动终止。', '本基金基金合同可能终止。',
    '终止基金销售机构服务。', '本基金基金合同自动终止，具体情形尚待确认。']) {
    const value = clone(fixture); value.quote = quote; value.evidence[0].quote = quote;
    assert.equal(normalizeLifecycleEvidence(value, options), null);
  }
  for (const alter of [value => { delete value.sourceTextHash; }, value => { value.page = 2; },
    value => { value.identityFundCodes = ['000009']; }, value => { value.evidence.pop(); },
    value => { value.dateBasis = 'terminationDate'; }, value => { value.publishedAt = '2026-05-06'; },
    value => { value.sourceUrl = 'https://127.0.0.1/report'; }]) {
    const value = clone(fixture); alter(value); assert.equal(normalizeLifecycleEvidence(value, options), null);
  }
  assert.equal(normalizeLifecycleEvidence(fixture, { ...options, periodEnd: '2026-09-30' }), null);
  const exact = clone(fixture); exact.periodEnd = exact.date;
  assert.equal(normalizeLifecycleEvidence(exact, { ...options, periodEnd: exact.date }), null);
});

test('retains liquidation report period ending date with its limited meaning', () => {
  const value = clone(fixture); value.eventType = 'final-liquidation';
  value.finalLiquidationEnd = '2026-06-15'; value.finalLiquidationEndBasis = 'liquidation-report-period-end';
  value.publishedAt = '2026-06-22';
  value.evidence.push({ field: 'finalLiquidationEnd', page: 1, quote: '自2026年5月8日起至2026年6月15日止为本基金清算报告期' });
  assert.equal(normalizeLifecycleEvidence(value, options).finalLiquidationEnd, '2026-06-15');
  delete value.finalLiquidationEndBasis;
  assert.equal(normalizeLifecycleEvidence(value, options), null);
});

test('lifecycle proof survives build, partition merge, receipt and frontend without becoming zero holdings', () => {
  const input = { asOf: options.asOf, retrievedAt: '2026-09-29T10:00:00Z', errors: [], barsByStock: {}, funds: [
    { code: '000001', name: '合成基金', company: '合成公司', companyId: 'co1', managers: [], reports: [],
      inceptionDate: '2023-01-01', inceptionSourceUrl: 'https://example.org/profile', notApplicableEvidence: fixture }] };
  const snapshot = buildSnapshot(input, { config, now });
  assert.deepEqual(snapshot.funds[0].notApplicableEvidence, fixture);
  assert.equal(snapshot.funds[0].inceptionDate, '2023-01-01');
  assert.equal(snapshot.funds[0].summary.knownPositionYuan, null);
  assert.equal(snapshot.coverage.noEquitiesFundCount, 0);
  assert.equal(snapshot.coverage.establishedAfterPeriodFundCount, 0);
  assert.equal(snapshot.coverage.terminatedBeforePeriodFundCount, 1);
  const catalog = { fundCount: 2, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 2 }],
    funds: [{ code: '000001', companyCode: 'co1' }, { code: '000002', companyCode: 'co1' }] };
  const merged = mergeSnapshots({ catalog, config, now, snapshots: [snapshot] });
  assert.equal(merged.manifest.coverage.catalogFundCount, 2);
  assert.equal(merged.manifest.coverage.terminatedBeforePeriodFundCount, 1);
  assert.equal(merged.manifest.coverage.noEquitiesFundCount, 0);
  assert.deepEqual(merged.receipt.funds[0].notApplicableEvidence, fixture);
  const published = merged.companySnapshots.co1;
  assert.equal(ui.validateDataset(published), published);
  published.funds[0].notApplicableEvidence.sourceTextHash = null;
  assert.throws(() => ui.validateDataset(published), /INVALID_DATASET/);
});

test('unknown source outcome cannot create an exemption or erase previous observations', () => {
  const input = { asOf: options.asOf, retrievedAt: now, errors: [], barsByStock: {},
    funds: [{ code: '000001', name: '合成基金', reports: [], notApplicableEvidence: { reason: 'TERMINATED_BEFORE_PERIOD' } }] };
  assert.throws(() => buildSnapshot(input, { config, now }), { code: 'NO_VALID_FRESH_FUNDS' });
});

test('frontend keeps founded-after and terminated-before counts distinct', () => {
  const progress = { notApplicableFunds: 3, funds: [
    { status: 'not-applicable', notApplicableEvidence: fixture },
    { status: 'not-applicable', notApplicableEvidence: { reason: 'ESTABLISHED_AFTER_PERIOD' } },
    { status: 'not-applicable' }] };
  assert.deepEqual(ui.notApplicableBreakdown(progress), { establishedAfterPeriodFunds: 1,
    terminatedBeforePeriodFunds: 1, otherNotApplicableFunds: 1 });
});
