'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizePortfolioIdentity, retainPortfolioIdentity } = require('../lib/portfolio-identity');
const { normalizeReport } = require('../lib/fund-analysis');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const fixture = require('./fixtures/portfolio-identity.json');
const reitFixture = require('./fixtures/portfolio-identity-reit.json');
const reitInterimFixture = require('./fixtures/portfolio-identity-reit-interim.json');
const clone = value => JSON.parse(JSON.stringify(value));
const now = '2026-10-26T12:00:00Z';
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001' }] };
const catalog = { companies: [{ code: 'co1', name: '合成机构', fundCount: 2 }],
  funds: ['000001', '000002'].map(code => ({ code, companyCode: 'co1', companyName: '合成机构' })) };
function report(changes = {}) {
  return { ...fixture.report, coverage: 'top10', navYuan: 10000,
    holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY',
      shares: 100, weightPct: 1, marketValueYuan: 100 }],
    portfolioIdentityEvidence: clone(fixture.proof), ...changes };
}
function snapshot(reports = [report()], options = {}) {
  return buildSnapshot({ asOf: '2026-10-26', retrievedAt: options.retrievedAt || '2026-10-26T10:00:00Z', barsByStock: {}, errors: [],
    funds: [{ code: '000001', name: '合成基金', company: '合成机构', companyId: 'co1',
      sourcePortfolioCode: '000001', shareClassCodes: ['000001', '000002'],
      portfolioSourceUrl: fixture.report.sourceUrl, managers: [], reports }] }, { now, config, ...options });
}

test('Python-generated proof survives JS validation and report normalization', () => {
  const value = normalizePortfolioIdentity(fixture.proof, fixture.report, now);
  assert.deepEqual(value, fixture.proof);
  const normalized = normalizeReport(report());
  assert.deepEqual(normalized.portfolioIdentityEvidence, value);
  assert.deepEqual(value.shareClassCodes, ['000001', '000002']);
  assert.equal(value.shareClassUniverseComplete, false);
  assert.equal(value.shareClassSpecificMetricsProvided, false);
  assert.deepEqual(value.identitySection.pages, [2, 3]);
});

test('bounded REIT fund and trading codes survive matching JS validation without class or holding inference', () => {
  const value = normalizePortfolioIdentity(reitFixture.proof, reitFixture.report, now);
  assert.deepEqual(value, reitFixture.proof);
  assert.equal(value.sourcePortfolioCode, '000008');
  assert.deepEqual(value.shareClassCodes, []);
  assert.equal(value.eligibleForShareClassExpansion, false);
  const normalized = normalizeReport({ ...reitFixture.report, holdings: [], coverage: 'unknown', portfolioIdentityEvidence: value });
  assert.deepEqual(normalized.portfolioIdentityEvidence, value);
  assert.deepEqual(normalized.holdings, []);
  assert.equal(normalized.noEquitiesConfirmed, false);
  assert.equal(normalized.navYuan, null);
});

test('REIT normalization rejects generic code proof, mismatched pair, missing boundaries and inferred classes', () => {
  const changes = [
    p => { p.method = 'pdf-explicit-fund-code-rows-v1'; },
    p => { p.rows = p.rows.slice(0, 1); },
    p => { p.rows[1].text = '交易代码 000009'; p.rows[1].codes = ['000009']; },
    p => { p.rows[1].text = '基金代码 000008'; },
    p => { p.rows[1].field = 'shareClassCodes'; p.rows[1].text = '下属分类基金的交易代码 000008'; },
    p => { p.shareClassCodes = ['000008']; },
    p => { p.identitySection.subheading = '2.1 基金基本情况'; },
    p => { p.identitySection.subheadingPage = 3; },
    p => { p.identitySection.subheadingAdjacent = false; },
    p => { p.identitySection.endHeading = '§3 主要财务指标和不动产基金运作情况'; },
    p => { p.kind = 'semiannual'; p.reportPeriodEvidence.text = '2026年中期报告'; },
  ];
  for (const change of changes) {
    const value = clone(reitFixture.proof); change(value);
    assert.equal(normalizePortfolioIdentity(value, reitFixture.report, now), null);
  }
  assert.equal(normalizePortfolioIdentity(reitFixture.proof, { ...reitFixture.report, fundCode: '000009' }, now), null);
});

test('REIT interim identity preserves its exact introduction parent without weakening quarterly boundaries', () => {
  const value = normalizePortfolioIdentity(reitInterimFixture.proof, reitInterimFixture.report, now);
  assert.deepEqual(value, reitInterimFixture.proof);
  assert.equal(value.kind, 'semiannual');
  assert.deepEqual(value.identitySection.pages, [5, 6]);
  assert.deepEqual(value.shareClassCodes, []);
  for (const fixture of [reitFixture, reitInterimFixture]) {
    const bad = clone(fixture.proof);
    bad.identitySection.heading = fixture === reitFixture ? '§2不动产基金简介' : '§2不动产基金产品概况';
    assert.equal(normalizePortfolioIdentity(bad, fixture.report, now), null);
  }
  const bad = clone(reitInterimFixture.proof);
  bad.kind = 'annual'; bad.periodEnd = '2026-12-31';
  assert.equal(normalizePortfolioIdentity(bad, null, now), null);
});

test('rejects unmatched reports, future receipts, corrupt rows, unknown pages and inflated scope', () => {
  for (const changes of [
    { sourceUrl: 'https://example.org/other.pdf' }, { periodEnd: '2026-09-30' }, { publishedAt: '2026-07-22' },
    { kind: 'semiannual' }, { retrievedAt: '2026-10-27T00:00:00Z' }, { retrievedAt: '2026-07-20T00:00:00Z' },
    { pdfSha256: null }, { sourceTextHash: 'not-a-hash' }, { shareClassUniverseComplete: true },
    { shareClassSpecificMetricsProvided: true }, { identitySection: { ...fixture.proof.identitySection, pageNumbersVerified: false } },
  ]) assert.equal(normalizePortfolioIdentity({ ...fixture.proof, ...changes }, fixture.report, now), null);
  assert.equal(normalizePortfolioIdentity(fixture.proof, { ...fixture.report, fundCode: '000003' }, now), null);
  const invalid = clone(fixture.proof);
  invalid.rows[1].codes.push('000003');
  invalid.shareClassCodes.push('000003');
  assert.equal(normalizePortfolioIdentity(invalid, fixture.report, now), null);
  const clean = normalizePortfolioIdentity({ ...fixture.proof, privateDebug: 'discard' }, fixture.report, now);
  assert.ok(!Object.hasOwn(clean, 'privateDebug'));
});

test('main code alone does not infer class codes or historical universe completeness', () => {
  const single = { ...clone(fixture.proof), rows: [fixture.proof.rows[0]], shareClassCodes: [],
    eligibleForShareClassExpansion: true };
  const value = normalizePortfolioIdentity(single, fixture.report, now);
  assert.deepEqual(value.shareClassCodes, []);
  assert.equal(value.eligibleForShareClassExpansion, false);
});

test('same-report empty extraction retains original receipt, but unrelated source cannot inherit proof', () => {
  const empty = report({ portfolioIdentityEvidence: null });
  assert.equal(retainPortfolioIdentity(empty, report()), true);
  assert.equal(empty.portfolioIdentityEvidence.retrievedAt, fixture.proof.retrievedAt);
  const unrelated = report({ sourceUrl: 'https://example.org/new.pdf', portfolioIdentityEvidence: null });
  assert.equal(retainPortfolioIdentity(unrelated, report()), false);
  assert.equal(unrelated.portfolioIdentityEvidence, null);
});

test('build preserves report-local identity through a later empty extraction and original baseline stays separate', () => {
  const baseline = report({ periodEnd: '2026-03-31', publishedAt: '2026-04-21',
    sourceUrl: 'https://example.org/baseline.pdf', portfolioIdentityEvidence: null });
  const previous = snapshot([report(), baseline]);
  const result = snapshot([report({ portfolioIdentityEvidence: null }), baseline], { previous });
  const item = result.funds[0];
  assert.deepEqual(item.selectedReport.portfolioIdentityEvidence, fixture.proof);
  assert.equal(item.previousReport.portfolioIdentityEvidence, null);
  assert.equal(item.selectedReport.navYuan, 10000);
  assert.equal(item.summary.knownPositionYuan, 100);
  assert.equal(item.reports.find(row => row.periodEnd === '2026-06-30').portfolioIdentityEvidence.retrievedAt,
    fixture.proof.retrievedAt);
  const changed = snapshot([report({ sourceUrl: 'https://example.org/new.pdf', publishedAt: '2026-07-22',
    portfolioIdentityEvidence: null })], { previous });
  assert.equal(changed.funds[0].selectedReport.portfolioIdentityEvidence, null);
  assert.ok(changed.funds[0].reports.some(row => row.sourceUrl === fixture.report.sourceUrl && row.portfolioIdentityEvidence));
});

test('merge preserves identity proof and no double-counted portfolio value across empty attempts', () => {
  const previous = mergeSnapshots({ snapshots: [snapshot()], catalog, config, now });
  const next = mergeSnapshots({ snapshots: [snapshot([report({ portfolioIdentityEvidence: null })], { retrievedAt: '2026-10-26T11:00:00Z' })],
    previousManifest: previous.manifest, previousCompanies: previous.companySnapshots, catalog, config, now });
  const items = Object.values(next.companySnapshots).flatMap(part => part.funds);
  assert.equal(items.length, 1);
  assert.deepEqual(items[0].selectedReport.portfolioIdentityEvidence, fixture.proof);
  assert.equal(items[0].selectedReport.navYuan, 10000);
  assert.equal(items[0].summary.knownPositionYuan, 100);
});

test('merge keeps different-source identity only in its matching report history', () => {
  const previous = mergeSnapshots({ snapshots: [snapshot()], catalog, config, now });
  const newerReport = report({ sourceUrl: 'https://example.org/interim.pdf', publishedAt: '2026-08-31',
    kind: 'semiannual', coverage: 'full', portfolioIdentityEvidence: null });
  const next = mergeSnapshots({ snapshots: [snapshot([newerReport], { retrievedAt: '2026-10-26T11:00:00Z' })],
    previousManifest: previous.manifest, previousCompanies: previous.companySnapshots, catalog, config, now });
  const item = Object.values(next.companySnapshots).flatMap(part => part.funds)[0];
  assert.equal(item.selectedReport.sourceUrl, newerReport.sourceUrl);
  assert.equal(item.selectedReport.portfolioIdentityEvidence, null);
  const oldReport = item.reports.find(value => value.sourceUrl === fixture.report.sourceUrl);
  assert.deepEqual(oldReport.portfolioIdentityEvidence, fixture.proof);
});
