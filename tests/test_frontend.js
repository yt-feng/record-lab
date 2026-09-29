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
