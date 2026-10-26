'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { managerCoverageFor, completeManagerCoverage } = require('../lib/report-managers');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const cases = require('./fixtures/manager-table-coverage.json');
const clone = value => JSON.parse(JSON.stringify(value));
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001' }] };
const now = '2026-09-29T12:00:00Z';
const catalog = { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
  funds: [{ code: '000001', companyCode: 'co1', companyName: '合成公司' }] };
function snapshot(sample, time = '2026-09-29T10:00:00Z', separateInterim = false) {
  const report = { sourceUrl: 'https://example.org/holdings.pdf', periodEnd: '2026-06-30', publishedAt: '2026-07-20',
    kind: 'quarterly', coverage: 'top10', navYuan: 1000,
    holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY', shares: 100, weightPct: 10, marketValueYuan: 100 }],
    managers: sample.managers, managerTableCoverage: sample.coverage, managerEvidenceRetrievedAt: time };
  const reports = separateInterim ? [{ ...report, kind: 'semiannual', coverage: 'full', sourceUrl: 'https://example.org/interim.pdf',
    publishedAt: '2026-08-28', managers: [], managerTableCoverage: null }, report] : [report];
  return buildSnapshot({ asOf: '2026-09-29', retrievedAt: time, errors: [], barsByStock: {},
    funds: [{ code: '000001', name: '合成基金', company: '合成公司', companyId: 'co1', managers: [], reports }] }, { config, now });
}

test('complete table proof binds all output rows and rejects subset-only claims', () => {
  const sample = cases.complete;
  assert.equal(managerCoverageFor(sample.coverage, sample.managers, '2026-06-30').complete, true);
  assert.equal(managerCoverageFor(sample.coverage, sample.managers.slice(0, 1), '2026-06-30').complete, false);
  assert.equal(completeManagerCoverage(snapshot(sample).funds[0]), true);
  const malformed = clone(sample.coverage); malformed.tables[0].endSection = '4.1';
  assert.equal(managerCoverageFor(malformed, sample.managers, '2026-06-30').complete, false);
  const undated = clone(sample.coverage); delete undated.publishedAt;
  assert.equal(managerCoverageFor(undated, sample.managers, '2026-06-30').complete, false);
});

test('all six partial-table counterexamples retain valid names but cannot count complete', () => {
  for (const name of ['invalid-date', 'missing-date', 'unknown-page', 'no-end-boundary', 'internal-six-page-cap', 'unsupported-name']) {
    const sample = cases[name], fund = snapshot(sample).funds[0];
    assert.equal(fund.reportManagers.length, 1, name);
    assert.equal(fund.managerAttribution, 'report', name);
    assert.equal(fund.managerCoverageStatus, 'partial', name);
    assert.equal(completeManagerCoverage(fund), false, name);
    const forged = clone(sample.coverage); forged.complete = true;
    assert.equal(managerCoverageFor(forged, sample.managers, '2026-06-30').complete, false, name);
  }
});

test('interim table remains complete after source-proven prequarter departure is filtered', () => {
  const fund = snapshot(cases.interim).funds[0];
  assert.equal(fund.reportManagers.length, 1);
  assert.equal(fund.reportManagers[0].name, '李华');
  assert.equal(fund.managerTableCoverage.outputManagerCount, 2);
  assert.equal(completeManagerCoverage(fund), true);
});

test('partial updates never replace a retained complete manager table', () => {
  const old = mergeSnapshots({ catalog, config, now, snapshots: [snapshot(cases.complete)] });
  const update = mergeSnapshots({ catalog, config, now, previousManifest: old.manifest, previousCompanies: old.companySnapshots,
    snapshots: [snapshot(cases['invalid-date'], '2026-09-29T11:00:00Z')] });
  const fund = update.companySnapshots.co1.funds[0];
  assert.deepEqual(fund.reportManagers.map(item => item.name).sort(), ['张明', '李华']);
  assert.equal(completeManagerCoverage(fund), true);
});

test('complementary partial observations keep valid names without inventing full-table proof', () => {
  const old = mergeSnapshots({ catalog, config, now, snapshots: [snapshot(cases['invalid-date'])] });
  const update = mergeSnapshots({ catalog, config, now, previousManifest: old.manifest, previousCompanies: old.companySnapshots,
    snapshots: [snapshot(cases['other-partial'], '2026-09-29T11:00:00Z')] });
  const fund = update.companySnapshots.co1.funds[0];
  assert.deepEqual(fund.reportManagers.map(item => item.name).sort(), ['张明', '李华']);
  assert.equal(completeManagerCoverage(fund), false);
  assert.equal(fund.managerCoverageStatus, 'partial');
});

test('merging quarterly manager proof keeps its original publication date apart from interim holdings', () => {
  const old = mergeSnapshots({ catalog, config, now, snapshots: [snapshot(cases['invalid-date'], undefined, true)] });
  const incoming = snapshot(cases.complete, '2026-09-29T11:00:00Z', true);
  assert.equal(incoming.funds[0].selectedReport.sourceUrl, 'https://example.org/interim.pdf');
  assert.equal(completeManagerCoverage(incoming.funds[0]), true);
  const update = mergeSnapshots({ catalog, config, now, previousManifest: old.manifest, previousCompanies: old.companySnapshots,
    snapshots: [incoming] });
  const fund = update.companySnapshots.co1.funds[0];
  assert.equal(fund.selectedReport.publishedAt, '2026-08-28');
  assert.equal(fund.managerTableCoverage.publishedAt, '2026-07-20');
  assert.equal(fund.managerTableCoverage.kind, 'quarterly');
  assert.equal(fund.managerTableCoverage.sourceUrl, cases.complete.coverage.sourceUrl);
  assert.equal(completeManagerCoverage(fund), true);
});
