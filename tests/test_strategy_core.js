'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { strategySectionCoverageFor } = require('../lib/strategy-sections');
const samples = require('./fixtures/strategy-sections.json');
const clone = value => JSON.parse(JSON.stringify(value));
const now = '2026-09-29T12:00:00Z';
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001' }] };
const catalog = { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
  funds: [{ code: '000001', companyCode: 'co1', companyName: '合成公司' }] };
function snapshot(sample, at = '2026-09-29T10:00:00Z') {
  const holdings = [{ stockCode: '601318', stockName: '中国平安', market: 'CN', currency: 'CNY', shares: 100, weightPct: 10, marketValueYuan: 100 }];
  const quarter = { sourceUrl: 'https://example.org/strategy.pdf', periodEnd: '2026-06-30', kind: 'quarterly', publishedAt: '2026-07-20',
    coverage: 'top10', holdings, navYuan: 1000, ...clone(sample), strategyEvidenceRetrievedAt: at,
    narrative: { text: '我们增加中国平安的配置，因为估值较为合理。', sourceUrl: 'https://example.org/strategy.pdf', page: 7,
      periodStart: '2026-04-01', periodEnd: '2026-06-30' } };
  const interim = { sourceUrl: 'https://example.org/interim.pdf', periodEnd: '2026-06-30', kind: 'semiannual', publishedAt: '2026-08-28',
    coverage: 'full', holdings, navYuan: 1000 };
  return buildSnapshot({ asOf: '2026-09-29', retrievedAt: at, errors: [], barsByStock: {}, funds: [{ code: '000001', name: '合成基金',
    company: '合成公司', companyId: 'co1', managers: [], reports: [interim, quarter] }] }, { config, now });
}

test('full interim holdings and exact Q2 strategy source stay independent through normalization', () => {
  const fund = snapshot(samples.complete).funds[0], report = fund.selectedReport;
  assert.equal(report.kind, 'semiannual'); assert.equal(report.publishedAt, '2026-08-28');
  assert.equal(report.sourceUrl, 'https://example.org/interim.pdf');
  assert.equal(report.strategySectionCoverage.kind, 'quarterly');
  assert.equal(report.strategySectionCoverage.sourceUrl, 'https://example.org/strategy.pdf');
  assert.equal(report.strategySectionCoverage.publishedAt, '2026-07-20');
  assert.equal(report.strategySectionCoverage.periodStart, '2026-04-01');
  assert.equal(strategySectionCoverageFor(report.strategySectionCoverage).complete, true);
  assert.equal(report.strategyAnalysis.semanticAnalysisComplete, false);
  assert.equal(fund.narrative.sourceUrl, 'https://example.org/strategy.pdf');
});

test('normalization keeps useful text while absent or incomplete strategy proof stays partial', () => {
  for (const sample of [{}, samples['missing-end']]) {
    const fund = snapshot(sample).funds[0];
    assert.ok(fund.narrative.text);
    assert.equal(strategySectionCoverageFor(fund.selectedReport.strategySectionCoverage)?.complete || false, false);
  }
});

test('a new partial strategy extract cannot overwrite retained complete section evidence', () => {
  const old = mergeSnapshots({ catalog, config, now, snapshots: [snapshot(samples.complete)] });
  const merged = mergeSnapshots({ catalog, config, now, previousManifest: old.manifest, previousCompanies: old.companySnapshots,
    snapshots: [snapshot(samples['missing-end'], '2026-09-29T11:00:00Z')] });
  const report = merged.companySnapshots.co1.funds[0].selectedReport;
  assert.equal(report.coverage, 'full');
  assert.equal(strategySectionCoverageFor(report.strategySectionCoverage).complete, true);
  assert.equal(report.strategyEvidenceRetrievedAt, '2026-09-29T10:00:00Z');
});

test('complete strategy repair enriches the retained interim report with its own source and observation time', () => {
  const old = mergeSnapshots({ catalog, config, now, snapshots: [snapshot(samples['missing-end'])] });
  const merged = mergeSnapshots({ catalog, config, now, previousManifest: old.manifest, previousCompanies: old.companySnapshots,
    snapshots: [snapshot(samples.complete, '2026-09-29T11:00:00Z')] });
  const report = merged.companySnapshots.co1.funds[0].selectedReport;
  assert.equal(report.sourceUrl, 'https://example.org/interim.pdf');
  assert.equal(strategySectionCoverageFor(report.strategySectionCoverage).complete, true);
  assert.equal(report.strategyEvidenceRetrievedAt, '2026-09-29T11:00:00Z');
});
