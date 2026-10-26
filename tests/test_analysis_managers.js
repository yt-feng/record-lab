'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSnapshot } = require('../scripts/build-data');
const { analyzeQuarter } = require('../scripts/analyze-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { evidenceFor } = require('../lib/report-managers');

const now = '2026-10-26T12:00:00Z', target = '2026-06-30', baseline = '2026-03-31';
const config = { targetPeriodEnd: target, baselinePeriodEnd: baseline, funds: [{ code: '000001' }] };
const manager = (name = '季度经理', changes = {}) => ({ name, role: '本基金基金经理', startDate: '2025-01-01', endDate: null,
  periodStart: '2026-04-01', periodEnd: target, evidence: { sourceUrl: 'https://example.org/quarter.pdf', page: 5, pages: [5],
    tablePage: 5, section: '4.1', text: `${name} 本基金基金经理 2025年1月1日 -`, method: 'explicit-report-manager-table', periodEnd: target }, ...changes });
const holding = { stockCode: '600001', stockName: '合成股票', market: 'CN', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 };
const report = changes => ({ fundCode: '000001', kind: 'quarterly', coverage: 'top10', sourceUrl: 'https://example.org/quarter.pdf',
  periodEnd: target, publishedAt: '2026-07-20', navYuan: 10000, holdings: [holding], ...changes });
function snapshot(reports, changes = {}, retrievedAt = '2026-10-26T10:00:00Z') {
  return buildSnapshot({ asOf: '2026-10-26', retrievedAt, funds: [{ code: '000001', companyId: 'co1', name: '合成基金', company: '合成公司',
    managers: [{ name: '当前经理' }], shareClassCodes: ['000001'], sourcePortfolioCode: '000001', portfolioSourceUrl: 'https://example.org/identity.pdf',
    reports: [...reports, report({ periodEnd: baseline, publishedAt: '2026-04-20', sourceUrl: 'https://example.org/baseline.pdf', managers: [manager('基期经理', { periodStart: '2026-01-01', periodEnd: baseline })] })],
    ...changes }], errors: [], barsByStock: {} }, { now, config });
}
function analysis(value, progress = null) {
  const parts = new Map(), path = './data/companies/co1.json';
  const manifest = { ...value, funds: [], companyFiles: [{ path }], coverage: { fundCount: 1, catalogFundCount: 1 } };
  const result = analyzeQuarter(manifest, () => value, { now, progress, writePart: (name, part) => parts.set(name, part) });
  return { result, portfolio: [...parts.values()].find(part => part.kind === 'portfolio-evidence').portfolios[0] };
}

test('Q2 keeps full interim holdings but prefers exact quarterly manager PDF and source evidence', () => {
  const value = snapshot([report({ kind: 'semiannual', coverage: 'full', publishedAt: '2026-08-28', sourceUrl: 'https://example.org/interim.pdf',
    managers: [manager('中报经理', { periodStart: '2026-01-01' })] }), report({ managers: [manager()], managerEvidenceRetrievedAt: now })]);
  const fund = value.funds[0];
  assert.equal(fund.selectedReport.sourceUrl, 'https://example.org/interim.pdf');
  assert.deepEqual(fund.reportManagers.map(item => item.name), ['季度经理']);
  assert.equal(fund.managerSourceUrl, 'https://example.org/quarter.pdf');
  assert.equal(fund.managerEvidenceRetrievedAt, now);
  const result = analysis(value);
  assert.equal(result.result.coverage.reportManagerRecords, 1);
  assert.equal(result.portfolio.reportManagers[0].evidence.page, 5);
  assert.equal(result.portfolio.managerSourceUrl, 'https://example.org/quarter.pdf');
});

test('interim fallback excludes managers who left before Q2 and retains incoming and departed quarter managers', () => {
  const list = [manager('一季离任', { periodStart: '2026-01-01', endDate: '2026-03-31' }),
    manager('二季离任', { periodStart: '2026-01-01', endDate: '2026-05-01' }),
    manager('二季新任', { periodStart: '2026-01-01', startDate: '2026-05-01' }),
    manager('三季新任', { periodStart: '2026-01-01', startDate: '2026-07-01' })];
  const value = snapshot([report({ kind: 'semiannual', coverage: 'full', publishedAt: '2026-08-28', managers: list })]);
  assert.deepEqual(value.funds[0].reportManagers.map(item => item.name), ['二季离任', '二季新任']);
  assert.deepEqual(value.funds[0].reportManagers.map(item => item.activeAtPeriodEnd), [false, true]);
});

test('current profile or overlapping tenure never satisfies PDF manager coverage', () => {
  const value = snapshot([report({})], { managers: [{ name: '任职期经理', startDate: '2025-01-01' }] });
  const { result, portfolio } = analysis(value, { phase: config, completionStatus: 'complete', research: { completeFunds: 1, applicableFunds: 1 } });
  assert.equal(portfolio.managerAttribution, 'tenure-overlap');
  assert.equal(result.stocks[0].reportManagerCount, 0);
  assert.equal(result.stocks[0].tenureOverlapManagerCount, 1);
  assert.equal(result.coverage.reportManagerRecords, 0);
  assert.equal(result.coverage.tenureOverlapManagerRecords, 1);
  assert.equal(result.coverage.managerEvidenceMissingRecords, 1);
  assert.equal(result.coverage.complete, false);
});

test('stale quarter, invalid pages, role, source and copied person evidence are rejected', () => {
  const good = manager();
  for (const bad of [manager('他人'), manager('季度经理', { role: '董事长' }),
    manager('季度经理', { evidence: { ...good.evidence, sourceUrl: 'https://127.0.0.1/report.pdf' } }),
    manager('季度经理', { evidence: { ...good.evidence, page: null } }),
    manager('季度经理', { evidence: { ...good.evidence, periodEnd: baseline } }),
    manager('季度经理', { evidence: { ...good.evidence, pages: [5, 7] } })]) {
    if (bad.name === '他人') bad.evidence = good.evidence;
    assert.equal(evidenceFor(bad, target), null);
  }
  assert.ok(evidenceFor(good, target));
  assert.equal(evidenceFor(good, '2026-09-30'), null);
});

test('manager backfill enriches retained stronger holdings and empty later extraction preserves evidence', () => {
  const catalog = { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
    funds: [{ code: '000001', name: '合成基金', companyCode: 'co1', companyName: '合成公司' }] };
  const large = report({ holdings: [holding, { ...holding, stockCode: '600002' }] });
  const first = mergeSnapshots({ catalog, config, now, snapshots: [snapshot([large])] });
  const recovered = snapshot([report({ managers: [manager()] })], {}, '2026-10-26T11:00:00Z');
  const second = mergeSnapshots({ catalog, config, now, snapshots: [recovered], previousManifest: first.manifest, previousCompanies: first.companySnapshots });
  const fund = second.companySnapshots.co1.funds[0];
  assert.equal(fund.selectedReport.holdings.length, 2);
  assert.equal(fund.reportManagers[0].name, '季度经理');
  assert.equal(fund.managerEvidenceRetrievedAt, recovered.retrievedAt);
  const empty = snapshot([large], {}, '2026-10-26T11:30:00Z');
  const third = mergeSnapshots({ catalog, config, now, snapshots: [empty], previousManifest: second.manifest, previousCompanies: second.companySnapshots });
  assert.equal(third.companySnapshots.co1.funds[0].reportManagers[0].name, '季度经理');
  assert.equal(third.companySnapshots.co1.funds[0].managerSourceUrl, 'https://example.org/quarter.pdf');
});
