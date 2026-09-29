'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { buildSnapshot, isValidNoEq, publicUrl, safeError, SafeBuildError } = require('../scripts/build-data');

// Synthetic records, reserved example URLs, and temporary test directories only.
const now = '2026-09-29T12:00:00Z';
const holding = (code = '600001') => ({ stockCode: code, stockName: '测试证券', market: 'CN', currency: 'CNY',
  shares: 100, weightPct: 5, marketValueYuan: 500 });
const report = (changes = {}) => ({ fundCode: '000001', periodEnd: '2026-06-30', publishedAt: '2026-07-20',
  kind: 'quarterly', coverage: 'top10', navYuan: 10000, sourceUrl: 'https://example.org/report.pdf',
  holdings: [holding()], narrative: null, ...changes });
const fund = (code = '000001', changes = {}) => ({ code, name: '合成测试基金', company: '测试公司',
  managers: [], shareClassCodes: [code], reports: [report({ fundCode: code })], ...changes });
const raw = (funds = [fund()], changes = {}) => ({ asOf: '2026-09-29', retrievedAt: '2026-09-29T10:00:00Z',
  funds, barsByStock: {}, errors: [], coverage: {}, ...changes });
const config = { funds: [{ code: '000001' }, { code: '000002' }], scope: '合成样本' };
const build = (value, changes = {}) => buildSnapshot(value, { now, ...changes });
const validBars = { 'CN:600001': { market: 'CN', stockCode: '600001', currency: 'CNY', priceBasis: 'unadjusted',
  sourceUrl: 'https://example.org/prices', rows: [{ date: '2026-04-10', low: 8, high: 12, close: 10 }] } };
const noEquitiesEvidence = (changes = {}) => ({ confirmed: true, sourceUrl: 'https://example.org/no-equities.pdf',
  periodEnd: '2026-06-30', publishedAt: '2026-07-20', page: 8,
  text: '本基金本报告期末未持有股票。', kind: 'quarterly', ...changes });

test('publishes the v1 envelope with measured coverage and separate A/C identities', () => {
  const result = build(raw([fund(), fund('000002')]), { config });
  assert.equal(result.schemaVersion, 1);
  assert.equal(result.sourceMode, 'actions');
  assert.equal(result.coverage.fundCount, 2);
  assert.equal(result.coverage.companyCount, 1);
  assert.equal(result.coverage.holdingCount, 2);
  assert.equal(result.coverage.requestedFundCount, 2);
  assert.equal(result.coverage.failedFundCount, 0);
  assert.deepEqual(result.funds.map(item => item.code), ['000001', '000002']);
  assert.equal(result.funds[0].dataStatus, 'fresh');
  assert.equal(result.funds[0].reports[0].fundCode, '000001');
});

test('rejects future as-of, malformed fund codes, missing report sources, and identity mixing', () => {
  assert.throws(() => build(raw(undefined, { asOf: '2026-09-30' })), { code: 'FUTURE_AS_OF' });
  assert.throws(() => build(raw([fund('bad')])), { code: 'INVALID_CONFIG' });
  for (const changes of [{ sourceUrl: null }, { sourceUrl: 'file:report.pdf' }, { fundCode: '000002' },
    { sourceFundCode: '000002' }, { officialSourceUrl: 'javascript:void(0)' }]) {
    assert.throws(() => build(raw([fund('000001', { reports: [report(changes)] })])), { code: 'NO_VALID_FRESH_FUNDS' });
  }
  assert.equal(publicUrl('https://example.org/report.pdf'), true);
  assert.equal(publicUrl('http://127.0.0.1/report.pdf'), false);
  assert.equal(publicUrl('https://example.org/report.pdf#unverified'), false);
  assert.equal(publicUrl('https://example.org/?' + 'to' + 'ken=example'), false);
});

test('partial failure keeps the original observation dates and reports', () => {
  const previous = build(raw([fund(), fund('000002')], { retrievedAt: '2026-09-28T10:00:00Z', asOf: '2026-09-28' }), { config });
  const result = build(raw([fund('000001', { reports: [] }), fund('000002')]), { previous, config });
  const cached = result.funds.find(item => item.code === '000001');
  assert.equal(cached.dataStatus, 'cached');
  assert.equal(cached.cacheReason, 'NO_VALID_REPORTS');
  assert.equal(cached.retrievedAt, '2026-09-28T10:00:00Z');
  assert.equal(cached.asOf, '2026-09-28');
  assert.equal(cached.selectedReport.periodEnd, '2026-06-30');
  assert.equal(result.coverage.failedFundCount, 1);
  assert.equal(result.coverage.fundCount, 2);
});

test('all collection failures throw even when an old complete snapshot exists', () => {
  const previous = build(raw());
  const oldBytes = JSON.stringify(previous);
  assert.throws(() => build(raw([fund('000001', { reports: [] })]), { previous }), { code: 'NO_VALID_FRESH_FUNDS' });
  assert.equal(JSON.stringify(previous), oldBytes);
});

test('same-date shrinking holdings or weaker report coverage keeps the complete prior fund', () => {
  const oldFund = fund('000001', { reports: [report({ coverage: 'full', holdings: [holding(), holding('600002')] })] });
  const previous = build(raw([oldFund, fund('000002')]), { config });
  const result = build(raw([fund(), fund('000002')]), { previous, config });
  assert.equal(result.funds[0].dataStatus, 'cached');
  assert.equal(result.funds[0].cacheReason, 'HOLDING_COVERAGE_REGRESSION');
  assert.equal(result.funds[0].rows.length, 2);
  const weaker = fund('000001', { reports: [report({ holdings: [holding(), holding('600002')] })] });
  assert.equal(build(raw([weaker, fund('000002')]), { previous, config }).funds[0].cacheReason, 'REPORT_COVERAGE_REGRESSION');
});

test('duplicate fund codes collapse without doubling holdings or NAV', () => {
  const result = build(raw([fund(), fund()]));
  assert.equal(result.funds.length, 1);
  assert.equal(result.coverage.requestedFundCount, 1);
  assert.equal(result.coverage.holdingCount, 1);
  assert.equal(result.funds[0].summary.knownPositionYuan, 500);
  assert.ok(result.errors.some(issue => issue.errorCode === 'DUPLICATE_FUND_CODE'));
});

test('retains source reports and reconstructs the prior quarter from saved history', () => {
  const older = report({ periodEnd: '2026-03-31', publishedAt: '2026-04-20', sourceUrl: 'https://example.org/prior.pdf' });
  const previous = build(raw([fund('000001', { reports: [older] })]));
  const result = build(raw(), { previous });
  assert.equal(result.funds[0].reports.length, 2);
  assert.equal(result.funds[0].previousReport.periodEnd, '2026-03-31');
  assert.equal(result.funds[0].previousReport.sourceUrl, 'https://example.org/prior.pdf');
  assert.equal(result.funds[0].selectedReport.sourceUrl, 'https://example.org/report.pdf');
});

test('same-window missing prices preserve their original fetch time', () => {
  const previous = build(raw(undefined, { retrievedAt: '2026-09-28T10:00:00Z', barsByStock: validBars }));
  const result = build(raw(), { previous });
  const price = result.funds[0].rows[0].priceRange;
  assert.equal(price.status, 'observed');
  assert.equal(price.low, 8);
  assert.equal(price.dataStatus, 'cached');
  assert.equal(price.fetchedAt, '2026-09-28T10:00:00Z');
  assert.equal(result.coverage.priceAvailable, 1);
  assert.equal(previous.funds[0].rows[0].priceRange.dataStatus, 'fresh');
});

test('different price window, report period, currency, or adjustment cannot reuse cached prices', () => {
  const previous = build(raw(undefined, { barsByStock: validBars }));
  const changedWindow = JSON.parse(JSON.stringify(previous));
  changedWindow.funds[0].rows[0].priceRange.rangeStart = '2026-01-01';
  assert.equal(build(raw(), { previous: changedWindow }).funds[0].rows[0].priceRange.status, 'missing');
  const changedCurrency = JSON.parse(JSON.stringify(previous));
  changedCurrency.funds[0].rows[0].priceRange.currency = 'HKD';
  assert.equal(build(raw(), { previous: changedCurrency }).funds[0].rows[0].priceRange.status, 'missing');
  const adjusted = JSON.parse(JSON.stringify(previous));
  adjusted.funds[0].rows[0].priceRange.priceBasis = 'forward-adjusted';
  assert.equal(build(raw(), { previous: adjusted }).funds[0].rows[0].priceRange.status, 'missing');
  const nextReport = report({ periodEnd: '2026-09-30', publishedAt: '2026-10-20' });
  const next = buildSnapshot(raw([fund('000001', { reports: [nextReport] })], {
    asOf: '2026-10-21', retrievedAt: '2026-10-21T10:00:00Z' }), { previous, now: '2026-10-21T12:00:00Z' });
  assert.equal(next.funds[0].rows[0].priceRange.status, 'missing');
});

test('unknown error messages and raw metadata never enter published errors', () => {
  const result = build(raw(undefined, { errors: [{ code: '000001', stage: 'report', errorCode: 'UNRECOGNIZED',
    message: 'private diagnostic', response: 'raw body' }] }));
  assert.deepEqual(result.errors[0], { code: '000001', stage: 'report', errorCode: 'BUILD_FAILED' });
  assert.equal(JSON.stringify(result).includes('private diagnostic'), false);
  assert.equal(safeError(new Error('private diagnostic')).errorCode, 'BUILD_FAILED');
  assert.equal(new SafeBuildError('unknown').message, 'BUILD_FAILED');
});

test('builder retains report and current-manager attribution sources', () => {
  const item = fund('000001', { metadataSourceUrl: 'https://example.org/profile', managersAsOf: '2026-09-29',
    managerScope: 'current-profile', managers: [{ name: '测试经理' }], reports: [report({
      holdingsSourceUrl: 'https://example.org/holdings', holdingsRetrieval: 'structured-table',
      navSourceUrl: 'https://example.org/nav.pdf', narrative: { text: '报告短摘录', excerptSelection: 'operation-sentences' },
    })] });
  const result = build(raw([item])).funds[0];
  assert.equal(result.metadataSourceUrl, 'https://example.org/profile');
  assert.equal(result.managerSourceUrl, 'https://example.org/profile');
  assert.equal(result.managerAttribution, 'current-profile');
  assert.equal(result.managersAsOf, '2026-09-29');
  assert.equal(result.selectedReport.holdingsSourceUrl, 'https://example.org/holdings');
  assert.equal(result.selectedReport.navSourceUrl, 'https://example.org/nav.pdf');
  assert.equal(result.selectedReport.holdingsRetrieval, 'structured-table');
  assert.equal(result.narrative.excerptSelection, 'operation-sentences');
});

test('incremental merge preserves other batches and distinguishes catalog totals from available data', () => {
  const previous = build(raw([fund(), fund('000002')]), { config });
  const catalog = { fundCount: 4, companyCount: 2, companies: [{ id: 'co1', name: '测试公司', fundCount: 3 },
    { id: 'co2', name: '另一公司', fundCount: 1 }], funds: [
    { code: '000001', companyId: 'co1' }, { code: '000002', companyId: 'co1' },
    { code: '000003', companyId: 'co1' }, { code: '000004', companyId: 'co2' },
  ] };
  const result = build(raw([fund('000003')]), { previous, preserveOthers: true, catalog,
    config: { funds: [{ code: '000003' }] } });
  assert.equal(result.coverage.requestedFundCount, 4);
  assert.equal(result.coverage.batchRequestedFundCount, 1);
  assert.equal(result.coverage.fundCount, 3);
  assert.equal(result.coverage.retainedFundCount, 2);
  assert.equal(result.coverage.catalogAvailableFundCount, 3);
  assert.equal(result.coverage.failedFundCount, 0);
  assert.equal(result.funds.find(item => item.code === '000001').cacheReason, 'NOT_IN_CURRENT_BATCH');
  assert.equal(result.funds.find(item => item.code === '000001').retrievedAt, previous.funds[0].retrievedAt);
  assert.equal(result.companyCoverage[0].availableFundCount, 3);
  assert.equal(result.companyCoverage[0].status, 'available');
  assert.equal(result.companyCoverage[1].status, 'unavailable');
  assert.throws(() => build(raw([fund('000003', { reports: [] })]), { previous, preserveOthers: true,
    config: { funds: [{ code: '000003' }] }, catalog }), { code: 'NO_VALID_FRESH_FUNDS' });
});

test('merge mode never describes batch size as the unknown whole catalog', () => {
  const result = build(raw(), { preserveOthers: true });
  assert.equal(result.coverage.requestedFundCount, null);
  assert.equal(result.coverage.catalogFundCount, null);
  assert.equal(result.coverage.batchRequestedFundCount, 1);
  assert.equal(result.coverage.catalogStatus, 'unavailable');
});

test('confirmed portfolio evidence deduplicates share classes using canonical holdings', () => {
  const proof = { sourcePortfolioCode: '000001', shareClassCodes: ['000001', '000002'],
    portfolioSourceUrl: 'https://example.org/share-classes.pdf' };
  const canonical = fund('000001', { ...proof, name: '合成基金A' });
  const alias = fund('000002', { ...proof, name: '合成基金C', reports: [report({ fundCode: '000002',
    navYuan: 900000, holdings: [{ ...holding(), marketValueYuan: 45000 }] })] });
  const result = build(raw([alias, canonical]));
  assert.equal(result.funds.length, 1);
  assert.equal(result.funds[0].code, '000001');
  assert.equal(result.funds[0].sourcePortfolioCode, '000001');
  assert.equal(result.funds[0].summary.knownPositionYuan, 500);
  assert.equal(result.funds[0].selectedReport.navYuan, 10000);
  assert.equal(result.funds[0].summary.aggregatesShareClasses, false);
  assert.equal(result.coverage.shareClassDeduplicatedCount, 1);
  assert.deepEqual(result.funds[0].aliases.map(item => item.code), ['000001', '000002']);
});

test('names and unproven share class labels never establish portfolio identity', () => {
  for (const evidence of [{}, { sourcePortfolioCode: '000001', shareClassCodes: ['000001', '000002'] },
    { sourcePortfolioCode: '000001', portfolioSourceUrl: 'https://example.org/proof.pdf', shareClassCodes: ['000001'] }]) {
    const result = build(raw([fund('000001', { name: '同名基金', ...evidence }),
      fund('000002', { name: '同名基金', ...evidence })]));
    assert.equal(result.funds.length, 2);
    assert.equal(result.coverage.shareClassDeduplicatedCount, 0);
  }
});

test('conflicting portfolio identities remain separate with a fixed diagnostic', () => {
  const result = build(raw([fund('000001', { sourcePortfolioCode: '000001', shareClassCodes: ['000001', '000002'],
    portfolioSourceUrl: 'https://example.org/first.pdf' }),
  fund('000002', { sourcePortfolioCode: '000002', shareClassCodes: ['000001', '000002'],
    portfolioSourceUrl: 'https://example.org/second.pdf' })]));
  assert.equal(result.funds.length, 2);
  assert.ok(result.errors.some(issue => issue.errorCode === 'PORTFOLIO_IDENTITY_CONFLICT'));
});

test('portfolio proof persists across batches without adding duplicate class positions', () => {
  const proof = { sourcePortfolioCode: '000001', shareClassCodes: ['000001', '000002'],
    portfolioSourceUrl: 'https://example.org/share-classes.pdf' };
  const previous = build(raw([fund('000001', proof)]));
  const result = build(raw([fund('000002', proof)]), { previous, preserveOthers: true,
    config: { funds: [{ code: '000002' }] } });
  assert.equal(result.funds.length, 1);
  assert.equal(result.funds[0].code, '000001');
  assert.equal(result.funds[0].summary.knownPositionYuan, 500);
  assert.deepEqual(result.funds[0].shareClassCodes, ['000001', '000002']);
});

test('no-equities validation requires explicit sourced stock absence, not fund type or restricted stock absence', () => {
  assert.equal(isValidNoEq(noEquitiesEvidence(), { asOf: '2026-09-29' }), true);
  assert.equal(isValidNoEq(noEquitiesEvidence({ text: '本报告期未投资于股票。' }), { asOf: '2026-09-29' }), true);
  for (const changes of [{ confirmed: false }, { sourceUrl: null }, { page: null },
    { periodEnd: '2026-06-31' }, { publishedAt: '2026-10-20' },
    { text: '本基金为纯债基金。' }, { text: '本基金本报告期末未持有流通受限股票。' },
    { text: '本基金本报告期末未持有股票型基金。' }, { text: '本基金本报告期末未持有股票期权。' },
    { text: '本基金并非未持有股票。' }, { text: '说明'.repeat(310) + '未持有股票。' }]) {
    assert.equal(isValidNoEq(noEquitiesEvidence(changes), { asOf: '2026-09-29' }), false);
  }
});

test('a whole batch of officially confirmed no-equity funds is a valid publication', () => {
  const first = fund('000001', { reports: [], noEquitiesEvidence: noEquitiesEvidence() });
  const second = fund('000002', { reports: [], noEquitiesEvidence: noEquitiesEvidence({ kind: undefined }) });
  const result = build(raw([first, second]), { config });
  assert.equal(result.coverage.fundCount, 2);
  assert.equal(result.coverage.noEquitiesFundCount, 2);
  assert.equal(result.coverage.batchNoEquitiesFundCount, 2);
  assert.equal(result.coverage.holdingCount, 0);
  assert.equal(result.coverage.failedFundCount, 0);
  assert.deepEqual(result.batchResults.map(item => item.status), ['no-equities', 'no-equities']);
  assert.equal(result.funds[0].status, 'no-equities');
  assert.deepEqual(result.funds[0].rows, []);
  assert.equal(result.funds[0].selectedReport.noEquitiesConfirmed, true);
  assert.equal(result.funds[0].noEquitiesEvidence.sourceUrl, 'https://example.org/no-equities.pdf');
  assert.equal(result.funds[1].selectedReport.kind, 'unknown');
});

test('a newer official no-equity report replaces former holdings while a same-date contradiction preserves them', () => {
  const older = report({ periodEnd: '2026-03-31', publishedAt: '2026-04-20' });
  const previous = build(raw([fund('000001', { reports: [older] })]));
  const result = build(raw([fund('000001', { reports: [], noEquitiesEvidence: noEquitiesEvidence() })]), { previous });
  assert.equal(result.funds[0].status, 'no-equities');
  assert.equal(result.funds[0].selectedReport.periodEnd, '2026-06-30');
  assert.equal(result.funds[0].previousReport.periodEnd, '2026-03-31');
  assert.equal(result.funds[0].summary.leftDisclosureCount, 0);
  const samePeriodPrevious = build(raw([fund(), fund('000002')]), { config });
  const contradiction = build(raw([fund('000001', { reports: [], noEquitiesEvidence: noEquitiesEvidence() }),
    fund('000002')]), { previous: samePeriodPrevious, config });
  assert.equal(contradiction.funds[0].dataStatus, 'cached');
  assert.equal(contradiction.funds[0].cacheReason, 'CONTRADICTORY_NO_EQUITIES');
  assert.equal(contradiction.funds[0].rows.length, 1);
  assert.equal(contradiction.batchResults[0].status, 'failed');
});

test('no-equities evidence survives caching and contributes to company catalog coverage', () => {
  const first = fund('000001', { reports: [], companyCode: 'co1', noEquitiesEvidence: noEquitiesEvidence() });
  const previous = build(raw([first]));
  const catalog = { fundCount: 2, companyCount: 1, companies: [{ companyCode: 'co1', name: '测试公司', fundCount: 2 }],
    funds: [{ code: '000001', companyCode: 'co1' }, { code: '000002', companyCode: 'co1' }] };
  const result = build(raw([fund('000002')]), { previous, catalog, preserveOthers: true,
    config: { funds: [{ code: '000002' }] } });
  assert.equal(result.coverage.noEquitiesFundCount, 1);
  assert.equal(result.funds.find(item => item.code === '000001').status, 'no-equities');
  assert.equal(result.companyCoverage[0].availableFundCount, 2);
  assert.equal(result.companyCoverage[0].status, 'available');
});

test('targeted Q2 phase only compares an exact Q1 baseline and cannot substitute annual data', () => {
  const phase = { funds: [{ code: '000001' }], targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
  const older = report({ periodEnd: '2025-12-31', publishedAt: '2026-03-20', kind: 'annual' });
  const result = build(raw([fund('000001', { reports: [report(), older] })]), { config: phase });
  assert.equal(result.funds[0].selectedReport.periodEnd, '2026-06-30');
  assert.equal(result.funds[0].previousReport, null);
  assert.equal(result.funds[0].rows[0].change.kind, 'no-baseline');
  assert.equal(result.targetPeriodEnd, '2026-06-30');
  assert.equal(result.coverage.baselinePeriodEnd, '2026-03-31');
  assert.throws(() => build(raw([fund('000001', { reports: [older] })]), { config: phase }), { code: 'NO_VALID_FRESH_FUNDS' });
});

test('CLI atomic write succeeds, while a failed later build preserves exact prior bytes', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'record-lab-test-'));
  try {
    const input = path.join(temporary, 'input.json');
    const output = path.join(temporary, 'latest.json');
    const settings = path.join(temporary, 'config.json');
    const today = new Date().toISOString();
    const value = raw(undefined, { asOf: today.slice(0, 10), retrievedAt: today });
    fs.writeFileSync(input, JSON.stringify(value));
    fs.writeFileSync(settings, JSON.stringify({ funds: [{ code: '000001' }] }));
    const script = path.join(__dirname, '..', 'scripts', 'build-data.js');
    const args = [script, '--input', input, '--output', output, '--config', settings];
    const good = spawnSync(process.execPath, args, { encoding: 'utf8' });
    assert.equal(good.status, 0);
    const bytes = fs.readFileSync(output, 'utf8');
    fs.writeFileSync(input, JSON.stringify({ ...value, funds: [] }));
    const bad = spawnSync(process.execPath, args, { encoding: 'utf8' });
    assert.equal(bad.status, 1);
    assert.equal(fs.readFileSync(output, 'utf8'), bytes);
    assert.deepEqual(JSON.parse(bad.stdout), { status: 'failed', errorCode: 'NO_VALID_FRESH_FUNDS' });
    assert.equal(bad.stderr, '');
    assert.deepEqual(fs.readdirSync(temporary).sort(), ['config.json', 'input.json', 'latest.json']);
  } finally { fs.rmSync(temporary, { recursive: true, force: true }); }
});
