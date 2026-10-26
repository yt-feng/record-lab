'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../web/app.js');
const { analyzeFund } = require('../lib/fund-analysis.js');

function fixtureFund(overrides = {}) {
  const holdings = [{ stockCode: '600001', stockName: '甲证券', market: 'CN', currency: 'CNY', shares: 1000, weightPct: 5, marketValueYuan: 50000 }];
  const fund = analyzeFund({ code: '000001', name: '甲基金', company: '甲公司', managers: [{ name: '甲经理' }], shareClassCodes: ['000001', '000002'],
    reports: [{ periodEnd: '2026-06-30', publishedAt: '2026-07-20', kind: 'quarterly', coverage: 'top10', navYuan: 1000000,
      sourceUrl: 'https://example.org/report', holdings, narrative: { text: null } },
    { periodEnd: '2026-03-31', publishedAt: '2026-04-20', kind: 'quarterly', coverage: 'top10', navYuan: 900000,
      holdings: [{ ...holdings[0], shares: 900, weightPct: 4.5 }, { stockCode: '000003', stockName: '乙证券', market: 'CN', shares: 200, weightPct: 3 }] }]
  }, { asOf: '2026-09-29' });
  return { ...fund, ...overrides };
}

function fixtureData(funds = [fixtureFund()]) {
  return { schemaVersion: 1, asOf: '2026-09-29', retrievedAt: '2026-09-29T02:30:00.000Z', sourceMode: 'actions',
    coverage: { fundCount: funds.length, companyCount: 1, scope: '用于验证的有限集合', limitations: [] }, funds, errors: [] };
}

test('accepts analyzer output including nullable narrative and missing holding facts', () => {
  const data = fixtureData();
  data.funds[0].name = null;
  data.funds[0].company = null;
  data.funds[0].rows[0].stockName = null;
  assert.equal(ui.validateDataset(data), data);
  assert.equal(ui.validateDataset(fixtureData([])).funds.length, 0);
});

test('rejects wrong schema, invalid observation dates and invalid rows', () => {
  for (const alter of [data => { data.schemaVersion = 2; }, data => { data.asOf = '2026-02-30'; },
    data => { data.retrievedAt = '2026-02-30T01:00:00Z'; }, data => { data.retrievedAt = '2026-09-29T01:00:00'; },
    data => { data.sourceMode = 'realtime'; }, data => { data.funds = {}; },
    data => { data.funds[0].shareClassCodes = {}; }, data => { data.funds[0].code = 123456; },
    data => { data.funds[0].rows[0].current.shares = '1000'; }, data => { data.funds[0].rows[0].position = null; },
    data => { data.funds[0].rows[0].priceRange.low = NaN; }, data => { data.coverage.limitations = ['valid', {}]; }]) {
    const data = fixtureData(); alter(data); assert.throws(() => ui.validateDataset(data), /INVALID_DATASET/);
  }
});

test('permits source links only over HTTP(S), with no embedded credentials or control characters', () => {
  assert.equal(ui.safeHttpUrl('https://example.org/report?a=1&b=2'), 'https://example.org/report?a=1&b=2');
  assert.equal(ui.safeHttpUrl('http://example.org/report'), 'http://example.org/report');
  assert.equal(ui.safeHttpUrl('https://example.org/report.pdf#page=5'), 'https://example.org/report.pdf#page=5');
  const credentialUrl = 'https://' + ['name', 'password'].join(':') + String.fromCharCode(64) + 'example.org';
  for (const value of ['javascript:alert(1)', 'data:text/html,x', '//example.org/path', 'file:///tmp/report',
    credentialUrl, 'java\nscript:alert(1)', 'https://exam\tple.org/report', null]) assert.equal(ui.safeHttpUrl(value), null);
  for (const key of ['token', 'access_token', 'API_KEY', 'authorization', 'X-Amz-Signature']) {
    assert.equal(ui.safeHttpUrl('https://example.org/report?' + key + '=' + 'test-value'), null);
  }
});

test('stock search matches only the corresponding rows, including a departed disclosure', () => {
  const fund = fixtureFund();
  assert.equal(ui.filterFunds([fund], { query: '甲经理 600001' })[0].visibleRows.length, 1);
  assert.equal(ui.filterFunds([fund], { query: '乙证券' })[0].visibleRows[0].change.kind, 'left-disclosure');
  assert.equal(ui.filterFunds([fund], { query: '不存在的证券' }).length, 0);
  assert.equal(ui.filterFunds([fund], { query: '000002' })[0].visibleRows.length, 2);
});

test('company, manager, date and change filters intersect without broadening an empty selection', () => {
  const fund = fixtureFund();
  const filters = { company: '甲公司', manager: '甲经理', period: '2026-06-30', change: 'increased' };
  assert.equal(ui.filterFunds([fund], filters)[0].visibleRows[0].stockCode, '600001');
  assert.equal(ui.filterFunds([fund], { ...filters, manager: '乙经理' }).length, 0);
  assert.equal(ui.filterFunds([fund], { ...filters, company: '乙公司' }).length, 0);
  assert.equal(ui.filterFunds([fund], { ...filters, period: '2026-03-31' }).length, 0);
  assert.equal(ui.filterFunds([fund], { ...filters, change: 'entered' }).length, 0);
});

test('historical manager attribution uses disclosed or tenure-supported names and labels current profiles separately', () => {
  const current = fixtureFund({ managerAttribution: 'current-profile', reportManagers: [] });
  assert.deepEqual(ui.managerNames(current), ['甲经理']);
  assert.equal(ui.managerAttributionLabel(current), '当前经理概况');
  const report = fixtureFund({ managerAttribution: 'report', reportManagers: [{ name: '乙经理' }] });
  assert.deepEqual(ui.managerNames(report), ['乙经理']);
  assert.equal(ui.managerAttributionLabel(report), '报告披露经理');
  assert.equal(ui.filterFunds([report], { manager: '甲经理' }).length, 0);
  assert.equal(ui.filterFunds([report], { manager: '乙经理' }).length, 1);
  assert.equal(ui.managerAttributionLabel({ ...report, managerAttribution: 'tenure-overlap' }), '任期覆盖报告期');
});

test('latest selection is computed per fund, while historical periods remain accessible', () => {
  const latest = fixtureFund();
  const old = fixtureFund({ selectedReport: { ...latest.previousReport } });
  const other = fixtureFund({ code: '000003', selectedReport: { ...latest.previousReport } });
  assert.deepEqual(ui.latestFunds([old, latest, other]), [latest, other]);
  assert.equal(ui.filterFunds([old, latest, other], { period: 'latest' }).length, 2);
  assert.equal(ui.filterFunds([old, latest, other], { period: '2026-03-31' }).length, 2);
});

test('unavailable funds remain visible without inventing holding records', () => {
  const fund = fixtureFund({ rows: [], status: 'unavailable', selectedReport: null });
  assert.equal(ui.validateDataset(fixtureData([fund])).funds[0].rows.length, 0);
  assert.equal(ui.filterFunds([fund], { query: '甲基金' }).length, 1);
  assert.equal(ui.filterFunds([fund], { change: 'unknown' }).length, 0);
  assert.equal(ui.filterFunds([fund], { query: '600001' }).length, 0);
});

test('missing or undisclosed values never become zeros', () => {
  assert.equal(ui.formatNumber(null), '未提供');
  assert.equal(ui.formatMoney(undefined), '未提供');
  assert.equal(ui.formatMoney(0), '0 元');
  assert.equal(ui.holdingValue(null, 'shares'), '未在披露名单');
  assert.equal(ui.holdingValue({ shares: null }, 'shares'), '未提供');
  assert.equal(ui.holdingValue({ shares: 0 }, 'shares'), '0');
  assert.equal(ui.signed(null), '不可比较');
  assert.equal(ui.signed(-0.5, ' pp'), '-0.5 pp');
});

test('price availability requires observed status and both finite prices', () => {
  assert.equal(ui.priceDescription({ status: 'missing', low: 0, high: 0 }).observed, false);
  assert.equal(ui.priceDescription({ status: 'observed', low: null, high: 1 }).observed, false);
  const output = ui.priceDescription({ status: 'observed', low: 12, high: 20.5, currency: 'HKD', observedStart: '2026-04-01', observedEnd: '2026-06-30', observedCount: 61 });
  assert.equal(output.value, '12 – 20.5 HKD');
  assert.match(output.note, /2026-04-01.*2026-06-30.*61/);
});

test('CSV neutralizes spreadsheet expressions, including leading whitespace', () => {
  for (const value of ['=HYPERLINK("https://example.org")', '+SUM(1,1)', '-2+3', '@SUM(1,1)', ' \t=1+1', '\ufeff=1+1', '\nhello', '\t000001']) {
    assert.ok(ui.csvCell(value).startsWith('"\''), value);
  }
  assert.equal(ui.csvCell(-10), '"-10"');
  assert.equal(ui.csvCell('含"引号,逗号'), '"含""引号,逗号"');
  assert.equal(ui.csvCell(null), '""');
});

test('export includes only filtered stock rows, original dates, safe links and no invented costs', () => {
  const fund = fixtureFund({ name: '=1+1' });
  fund.rows[0].priceRange.sourceUrl = 'javascript:alert(1)';
  const csv = ui.exportCsv(ui.filterFunds([fund], { query: '600001' }));
  assert.ok(csv.startsWith('\ufeff'));
  assert.match(csv, /600001/);
  assert.doesNotMatch(csv, /乙证券|javascript:/);
  assert.match(csv, /"'=1\+1"/);
  assert.match(csv, /2026-06-30/);
  assert.match(csv, /未知；区间价格不代表持有成本/);
  assert.equal(csv.split('\r\n').length, 2);
});

test('failed reload preserves the snapshot and imported provenance labels', () => {
  const data = fixtureData();
  const snapshot = JSON.stringify(data);
  const state = ui.observationState(data, 'remote', true);
  assert.match(state.badge, /载入未成功/);
  assert.match(state.dates, /2026-09-29.*02:30:00 UTC/);
  assert.equal(JSON.stringify(data), snapshot);
  assert.match(ui.observationState(data, 'imported', true).badge, /本地导入/);
  assert.match(ui.observationState({ ...data, sourceMode: 'cached' }, 'remote', false).badge, /保留的有效数据/);
  assert.equal(ui.observationState(null, 'remote', true).badge, '尚无可用数据');
});

function fixtureCatalog(complete = true) {
  return { schemaVersion: 1, asOf: '2026-09-29', retrievedAt: '2026-09-29T01:00:00Z',
    companies: [{ code: 'c1', name: '甲公司全称', fundCount: 2 }, { code: 'c2', name: '乙公司', fundCount: null }],
    funds: [{ code: '000001', name: '甲基金', companyCode: 'c1', companyName: '甲公司全称', type: '混合' },
      { code: '000002', name: '甲基金另一份额', companyCode: 'c1', companyName: '甲公司全称', type: '混合' }],
    coverage: { companyCount: 2, fundCount: 2, complete, status: complete ? 'complete' : 'bootstrap' } };
}

test('catalog preserves unprocessed companies and unknown directory totals', () => {
  const catalog = ui.validateCatalog(fixtureCatalog(false));
  const companies = ui.companyCoverage(catalog, null);
  assert.equal(companies.length, 2);
  assert.equal(companies[1].totalFunds, null);
  assert.equal(companies[0].processedFunds, null);
  assert.equal(ui.companyCoverage(catalog, null, '乙公司')[0].code, 'c2');
  const overview = ui.coverageOverview(catalog, null, fixtureData());
  assert.equal(overview.totalFunds, null);
  assert.equal(overview.totalCompanies, null);
  assert.equal(overview.percent, null);
  const partial = fixtureCatalog(); partial.coverage = { ...partial.coverage, status: 'partial-directory', directoryComplete: false };
  partial.funds.push({ code: '999999', name: '归属待核验基金', companyCode: null, companyName: null });
  assert.equal(ui.validateCatalog(partial), partial);
  assert.equal(ui.catalogComplete(partial), false);
});

test('company-code mapping supports differing company labels without falling back to another company', () => {
  assert.equal(ui.filterFunds([fixtureFund()], { company: 'code:c1' }, fixtureCatalog()).length, 1);
  assert.equal(ui.filterFunds([fixtureFund()], { company: 'code:c2' }, fixtureCatalog()).length, 0);
  assert.equal(ui.filterFunds([fixtureFund()], { query: '甲公司全称' }, fixtureCatalog()).length, 1);
});

test('manifest accepts multiple pages per company and sorts pages without loading others', () => {
  const manifest = fixtureData([]);
  manifest.companyFiles = [
    { companyCode: 'c1', companyName: '甲公司', path: './data/companies/c1-part2.json', partIndex: 2, partCount: 2, fundCount: 5 },
    { companyCode: 'c2', companyName: '乙公司', path: './data/companies/c2.json', fundCount: 1 },
    { companyCode: 'c1', companyName: '甲公司', path: './data/companies/c1-part1.json', partIndex: 1, partCount: 2, fundCount: 100 }
  ];
  assert.equal(ui.validateDataset(manifest), manifest);
  assert.deepEqual(ui.companyPages(manifest, 'c1').map(page => page.partIndex), [1, 2]);
  assert.deepEqual(ui.companyPages(manifest, 'pending-company'), []);
  for (const path of ['https://example.org/data.json', '//example.org/data.json', './data/companies/../latest.json', './data/companies/c1.json?x=1', './data/companies/%2e%2e.json', './data/other.json']) assert.equal(ui.safeCompanyPath(path), null);
  assert.equal(ui.safeCompanyPath('./data/companies/c1-part2.json'), './data/companies/c1-part2.json');
  manifest.companyFiles[0].path = '../private.json';
  assert.throws(() => ui.validateDataset(manifest), /INVALID_DATASET/);
});

test('progress validates success counts while incomplete company directories remain unknown', () => {
  const progress = { schemaVersion: 1, updatedAt: '2026-09-29T01:00:00Z', totalCompanies: 2, totalFunds: 2,
    processedFunds: 1, completedCompanies: 0, pendingFunds: 1, funds: [{ code: '000001', companyCode: 'c1', status: 'complete' }, { code: '000002', companyCode: null, status: 'pending' }],
    companies: [{ code: 'c1', name: '甲公司', totalFunds: null, processedFunds: 1, withHoldings: 1, withoutEquities: 0, failed: 0, pending: 1 }] };
  assert.equal(ui.validateProgress(progress), progress);
  assert.equal(ui.coverageOverview(fixtureCatalog(), progress, fixtureData()).percent, 50);
  assert.equal(ui.companyCoverage(null, progress)[0].totalFunds, null);
  assert.throws(() => ui.validateProgress({ ...progress, processedFunds: 3 }), /INVALID_PROGRESS/);
});

test('no-equities records require report evidence and remain available without fabricated rows', () => {
  const fund = fixtureFund({ status: 'no-equities', rows: [], noEquitiesEvidence: { confirmed: true, sourceUrl: 'https://example.org/report.pdf', periodEnd: '2026-06-30', text: '本期末未持有股票。' } });
  assert.equal(ui.validateDataset(fixtureData([fund])).funds[0].status, 'no-equities');
  assert.equal(ui.filterFunds([fund], { query: '甲基金' }).length, 1);
  assert.equal(ui.coverageOverview(fixtureCatalog(), null, fixtureData([fund])).coveredFunds, 2);
  assert.throws(() => ui.validateDataset(fixtureData([{ ...fund, noEquitiesEvidence: { confirmed: false } }])), /INVALID_DATASET/);
});

function fixtureEmptyStockFund() {
  const fund = fixtureFund({ status: 'no-equities', rows: [] });
  fund.selectedReport = { ...fund.selectedReport, holdings: [], noEquitiesConfirmed: true };
  fund.noEquitiesEvidence = { confirmed: true, evidenceType: 'empty-stock-detail-section', kind: fund.selectedReport.kind,
    sourceUrl: fund.selectedReport.sourceUrl, periodEnd: fund.selectedReport.periodEnd, publishedAt: fund.selectedReport.publishedAt,
    page: 7, text: '5.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n无。',
    assetAllocationText: '1 权益投资 - -\n其中：股票 - -' };
  return fund;
}

test('empty stock detail evidence requires both exact original excerpts and matching report metadata', () => {
  for (const kind of ['quarterly', 'semiannual']) {
    const fund = fixtureEmptyStockFund(); fund.selectedReport.kind = kind; fund.noEquitiesEvidence.kind = kind;
    assert.equal(ui.validEmptyStockEvidence(fund.noEquitiesEvidence, fund.selectedReport, '2026-09-29'), true);
    assert.equal(ui.validateDataset(fixtureData([fund])).funds[0].status, 'no-equities');
    fund.noEquitiesEvidence.text = '5.3.1 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n注：无。';
    fund.noEquitiesEvidence.assetAllocationText = '权益投资 0.00 0\n其中: 股票 0 0.00';
    assert.equal(ui.validateDataset(fixtureData([fund])).funds[0], fund);
  }
});

test('empty stock detail evidence rejects nonzero amounts, incomplete tables and unrelated empty sections', () => {
  const invalidEvidence = [
    { assetAllocationText: '1 权益投资 10 0\n其中：股票 - -' },
    { assetAllocationText: '1 权益投资 - -\n其中：股票 0 0.01' },
    { assetAllocationText: '1 权益投资 0.001 0\n其中：股票 0 0' },
    { assetAllocationText: '1 权益投资 - -' },
    { assetAllocationText: '1 权益投资 - -\n其中：债券 - -' },
    { assetAllocationText: '1 权益投资 - -\n其中：股票 - -\n2 固定收益投资 - -' },
    { text: '5.4 期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细\n无。' },
    { text: '5.4 期末债券投资明细\n无。' },
    { text: '无。' },
    { text: '期末按公允价值占基金资产净值比例大小排序的所有股票投资明细无。' },
    { text: '5.3.1.1 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细无。' },
    { evidenceType: 'trust-this-empty-section' },
    { sourceUrl: 'https://example.org/other-report' },
    { sourceUrl: 'javascript:alert(1)' },
    { periodEnd: '2026-03-31' },
    { publishedAt: '2026-07-21' },
    { kind: 'annual' },
    { kind: 'semiannual' },
    { confirmed: false },
    { page: null }, { page: 0 }, { page: -1 }, { page: 1.5 },
    { text: ' '.repeat(601) }, { assetAllocationText: ' '.repeat(601) }
  ];
  for (const change of invalidEvidence) {
    const fund = fixtureEmptyStockFund(); Object.assign(fund.noEquitiesEvidence, change);
    assert.equal(ui.validEmptyStockEvidence(fund.noEquitiesEvidence, fund.selectedReport, '2026-09-29'), false, JSON.stringify(change));
    assert.throws(() => ui.validateDataset(fixtureData([fund])), /INVALID_DATASET/, JSON.stringify(change));
  }
  const future = fixtureEmptyStockFund();
  future.noEquitiesEvidence.publishedAt = future.selectedReport.publishedAt = '2026-09-30';
  assert.throws(() => ui.validateDataset(fixtureData([future])), /INVALID_DATASET/);
});

function fixturePeriods() {
  return { schemaVersion: 1, defaultPeriod: '2026Q3', periods: [
    { id: '2026Q2', label: '2026Q2', path: './data/periods/2026Q2/', targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' },
    { id: '2026Q3', label: '2026Q3', path: './data/periods/2026Q3/', targetPeriodEnd: '2026-09-30', baselinePeriodEnd: '2026-06-30' }
  ] };
}

test('published quarter selection never substitutes Q2 for an unpublished Q3 request', () => {
  const manifest = ui.validatePeriods(fixturePeriods());
  assert.equal(ui.selectQuarter(manifest).id, '2026Q3');
  assert.equal(ui.selectQuarter(manifest, '2026Q2').targetPeriodEnd, '2026-06-30');
  assert.equal(ui.selectQuarter(null).id, '2026Q2');
  assert.equal(ui.selectQuarter(null, '2026Q3'), null);
  assert.equal(ui.selectQuarter(manifest, '2026Q4'), null);
  const wrong = fixturePeriods(); wrong.periods[1].targetPeriodEnd = '2026-06-30';
  assert.throws(() => ui.validatePeriods(wrong), /INVALID_PERIODS/);
  assert.throws(() => ui.validatePeriods({ ...manifest, defaultPeriod: '2026Q4' }), /INVALID_PERIODS/);
  const duplicate = fixturePeriods(); duplicate.periods.push(duplicate.periods[0]);
  assert.throws(() => ui.validatePeriods(duplicate), /INVALID_PERIODS/);
});

test('quarter paths are limited to the validated local period and company namespaces', () => {
  const period = fixturePeriods().periods[1];
  assert.equal(ui.periodDataPath(period, 'latest.json'), './data/periods/2026Q3/latest.json');
  assert.equal(ui.periodDataPath(period, './data/companies/c1.json'), './data/periods/2026Q3/companies/c1.json');
  assert.equal(ui.periodDataPath(period, './data/companies/c1.json.gz'), './data/periods/2026Q3/companies/c1.json.gz');
  assert.equal(ui.periodDataPath(ui.selectQuarter(null), 'latest.json'), './data/latest.json');
  for (const path of ['./data/periods/2026Q3/../', 'https://example.org/', './data/periods/2026Q2/']) {
    assert.throws(() => ui.periodDataPath({ ...period, path }, 'latest.json'), /INVALID_PERIOD_PATH/);
  }
  for (const path of ['../latest.json', './data/companies/a.json.gz?x=1', './data/companies/a.json.gz/extra']) {
    assert.throws(() => ui.periodDataPath(period, path), /INVALID_PERIOD_PATH/);
  }
});

test('period dataset validation refuses a Q2 payload under the Q3 selector', () => {
  const data = fixtureData(); data.targetPeriodEnd = '2026-06-30';
  assert.equal(ui.validatePeriodDataset(data, fixturePeriods().periods[0]), data);
  assert.throws(() => ui.validatePeriodDataset(data, fixturePeriods().periods[1]), /PERIOD_MISMATCH/);
  data.targetPeriodEnd = '2026-09-30';
  assert.throws(() => ui.validatePeriodDataset(data, fixturePeriods().periods[1]), /PERIOD_MISMATCH/);
  const empty = fixtureData([]); empty.targetPeriodEnd = '2026-09-30';
  assert.equal(ui.validatePeriodDataset(empty, fixturePeriods().periods[1]), empty);
  empty.coverage.targetPeriodEnd = '2026-06-30';
  assert.throws(() => ui.validatePeriodDataset(empty, fixturePeriods().periods[1]), /PERIOD_MISMATCH/);
  empty.coverage.targetPeriodEnd = '2026-09-30'; empty.baselinePeriodEnd = '2026-03-31';
  assert.throws(() => ui.validatePeriodDataset(empty, fixturePeriods().periods[1]), /PERIOD_MISMATCH/);
});

test('compressed company pages decode with browser streams and reject malformed gzip', async () => {
  const { gzipSync } = require('node:zlib');
  const text = JSON.stringify(fixtureData());
  assert.equal(await ui.responseText(new Response(gzipSync(text)), './data/companies/c1.json.gz'), text);
  assert.equal(await ui.responseText(new Response(text), './data/companies/c1.json'), text);
  await assert.rejects(() => ui.responseText(new Response('broken archive'), './data/companies/c1.json.gz'));
  await assert.rejects(() => ui.responseText(new Response(gzipSync(text)), './data/companies/c1.json.gz', {}), /GZIP_UNAVAILABLE/);
});

test('research completeness stays distinct from holdings completion and validates bounded counts', () => {
  const progress = { schemaVersion: 1, updatedAt: '2026-10-26T01:00:00Z', totalCompanies: 1, totalFunds: 2,
    processedFunds: 2, completedCompanies: 1, pendingFunds: 0, funds: [{ code: '000001', companyCode: 'c1', status: 'complete', quality: { status: 'exhausted', fields: { pdf: 'complete', strategy: 'missing' } } }],
    companies: [{ code: 'c1', name: '甲公司', totalFunds: 2, processedFunds: 2, withHoldings: 2, withoutEquities: 0, failed: 0, pending: 0 }],
    completionStatus: 'incomplete-exhausted', research: { enabled: true, completeFunds: 0, incompleteFunds: 2, pendingFunds: 0, exhaustedFunds: 2,
      fieldCoverage: { pdf: 2, strategy: 0, nav: 1, baseline: 2, prices: 2 } } };
  assert.equal(ui.validateProgress(progress), progress);
  assert.equal(ui.researchOverview(progress).completeFunds, 0);
  assert.equal(ui.researchOverview(progress).completionStatus, 'incomplete-exhausted');
  assert.equal(ui.researchOverview(null), null);
  assert.equal(ui.fundQuality(progress, fixtureFund()).fields.strategy, 'missing');
  assert.equal(ui.fundQuality(progress, fixtureFund({ code: '000002' })), null);
  progress.research.fieldCoverage.holdings = 1;
  progress.funds[0].quality.fields.holdings = 'truncated';
  assert.equal(ui.validateProgress(progress), progress);
  assert.equal(ui.researchOverview(progress).fieldCoverage.holdings, 1);
  assert.equal(ui.fundQuality(progress, fixtureFund()).fields.holdings, 'truncated');
  progress.research.fieldCoverage.holdings = 3;
  assert.throws(() => ui.validateProgress(progress), /INVALID_PROGRESS/);
  delete progress.research.fieldCoverage.holdings;
  progress.research.fieldCoverage.pdf = 3;
  assert.throws(() => ui.validateProgress(progress), /INVALID_PROGRESS/);
});

test('period-after-inception exemption requires source proof and stays outside research completion', () => {
  const evidence = { reason: 'ESTABLISHED_AFTER_PERIOD', date: '2026-07-20', sourceUrl: 'https://example.org/profile', periodEnd: '2026-06-30', fundCode: '000001' };
  const fund = fixtureFund({ status: 'unavailable', selectedReport: null, previousReport: null, rows: [], notApplicableEvidence: evidence });
  assert.equal(ui.validateDataset(fixtureData([fund])).funds[0], fund);
  assert.equal(ui.coverageOverview(fixtureCatalog(), null, fixtureData([fund])).coveredFunds, 0);
  assert.throws(() => ui.validateDataset(fixtureData([{ ...fund, notApplicableEvidence: { ...evidence, date: '2026-06-01' } }])), /INVALID_DATASET/);
  assert.throws(() => ui.validateDataset(fixtureData([{ ...fund, notApplicableEvidence: { ...evidence, fundCode: '000002' } }])), /INVALID_DATASET/);
  const data = fixtureData([fund]); data.targetPeriodEnd = '2026-09-30';
  assert.throws(() => ui.validatePeriodDataset(data, fixturePeriods().periods[1]), /PERIOD_MISMATCH/);
  const progress = { schemaVersion: 1, updatedAt: '2026-10-26T01:00:00Z', totalCompanies: 1, totalFunds: 2,
    processedFunds: 1, completedCompanies: 1, pendingFunds: 0, notApplicableFunds: 1,
    funds: [{ code: '000001', companyCode: 'c1', status: 'not-applicable' }], companies: [], completionStatus: 'complete',
    research: { enabled: true, completeFunds: 1, incompleteFunds: 0, pendingFunds: 0, exhaustedFunds: 0, fieldCoverage: { pdf: 1, strategy: 1, nav: 1, baseline: 1, prices: 1 } } };
  assert.equal(ui.validateProgress(progress), progress);
  assert.equal(ui.researchOverview(progress).applicableFunds, 1);
  assert.equal(ui.researchOverview(progress).completeFunds, 1);
  assert.equal(ui.researchOverview(progress).notApplicableFunds, 1);
});

test('empty awaiting quarter accepts no observation dates without inventing a timestamp', () => {
  const awaiting = { ...fixtureData([]), asOf: null, retrievedAt: null, targetPeriodEnd: '2026-09-30', companyFiles: [],
    coverage: { ...fixtureData([]).coverage, status: 'awaiting-data', fundCount: 0 } };
  assert.equal(ui.validateDataset(awaiting), awaiting);
  assert.equal(ui.validatePeriodDataset(awaiting, fixturePeriods().periods[1]), awaiting);
  const state = ui.observationState(awaiting, 'remote', false);
  assert.match(state.badge, /等待首份有效数据/);
  assert.match(state.dates, /尚无有效观察或采集时点/);
  assert.doesNotMatch(state.dates, /null|undefined|UTC/);
  for (const mutation of [d => { d.coverage.status = 'partial'; }, d => { d.funds = [fixtureFund()]; },
    d => { d.coverage.fundCount = 1; }, d => { d.companyFiles = [{ companyCode: 'c1', companyName: '甲公司', path: './data/companies/c1.json', fundCount: 0 }]; },
    d => { d.retrievedAt = '2026-10-26T01:00:00Z'; }, d => { d.targetPeriodEnd = null; }]) {
    const data = structuredClone(awaiting); mutation(data);
    assert.throws(() => ui.validateDataset(data), /INVALID_DATASET/);
  }
});

test('budget deferrals remain distinct from failed attempts and expose the next scheduled check', () => {
  const progress = { schemaVersion: 1, updatedAt: '2026-10-26T01:00:00Z', totalCompanies: 1, totalFunds: 2,
    processedFunds: 0, completedCompanies: 0, pendingFunds: 1, failedFunds: 1, deferredFunds: 1,
    nextRetryAt: '2026-10-27T00:30:00Z', directoryNextRetryAt: null, continueRun: false, completionStatus: 'incomplete-exhausted',
    companies: [{ code: 'c1', name: '甲公司', totalFunds: 2, processedFunds: 0, withHoldings: 0, withoutEquities: 0, failed: 1, pending: 1, deferredFunds: 1 }],
    funds: [{ code: '000001', companyCode: 'c1', status: 'pending', attempts: 0, deferredUntil: '2026-10-27T00:30:00Z', lastDeferredAt: '2026-10-26T01:00:00Z' },
      { code: '000002', companyCode: 'c1', status: 'failed', attempts: 2 }] };
  assert.equal(ui.validateProgress(progress), progress);
  assert.equal(progress.funds[0].attempts, 0);
  assert.equal(progress.failedFunds, 1);
  assert.equal(ui.companyCoverage(null, progress)[0].deferredFunds, 1);
  const retry = ui.retryState(progress);
  assert.equal(retry.label, '本轮结束，等待复查');
  assert.match(retry.detail, /不早于.*08:30.*北京时间/);
  assert.doesNotMatch(retry.label, /已齐|已完成/);
  assert.equal(ui.retryState({ ...progress, nextRetryAt: null }).label, '仍有缺项，需继续处理');
  assert.equal(ui.retryState({ ...progress, continueRun: true, completionStatus: 'incomplete-running' }).label, '仍在补采');
  for (const mutation of [d => { d.deferredFunds = 3; }, d => { d.nextRetryAt = '2026-02-30T00:00:00Z'; },
    d => { d.funds[0].deferredUntil = 'tomorrow'; }, d => { d.companies[0].deferredFunds = -1; },
    d => { d.funds[0].status = 'deferred-budget'; }]) {
    const invalid = structuredClone(progress); mutation(invalid);
    assert.throws(() => ui.validateProgress(invalid), /INVALID_PROGRESS/);
  }
});

test('retained company ownership keeps its original verification date during a fresh directory refresh', () => {
  const catalog = fixtureCatalog(); catalog.asOf = '2026-10-26'; catalog.retrievedAt = '2026-10-26T01:00:00Z';
  catalog.companies[0].directoryStatus = 'stale'; catalog.companies[0].directoryAsOf = '2026-09-29';
  catalog.coverage.staleCompanyCount = 1; catalog.coverage.retainedHistoricFundCount = 1;
  assert.equal(ui.validateCatalog(catalog), catalog);
  assert.equal(ui.catalogComplete(catalog), false);
  assert.deepEqual(ui.catalogFreshness(catalog), { staleCompanyCount: 1, retainedHistoricFundCount: 1 });
  const company = ui.companyCoverage(catalog, null)[0];
  const label = ui.companyDirectoryLabel(company, catalog.asOf);
  assert.match(label, /沿用缓存归属.*2026-09-29.*本轮刷新未成功/);
  assert.doesNotMatch(label, /2026-10-26/);
  for (const mutate of [d => { delete d.companies[0].directoryAsOf; },
    d => { d.companies[0].directoryAsOf = '2026-10-27'; }, d => { d.coverage.staleCompanyCount = 0; },
    d => { d.coverage.retainedHistoricFundCount = 3; }]) {
    const invalid = structuredClone(catalog); mutate(invalid);
    assert.throws(() => ui.validateCatalog(invalid), /INVALID_CATALOG/);
  }
});
