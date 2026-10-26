'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { analyzeQuarter } = require('../scripts/analyze-data');

const now = '2026-10-26T11:00:00Z';
const config = { funds: [{ code: '000001' }], targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
function fixture() {
  const sourceUrl = 'https://example.org/interim.pdf';
  const holdings = ['GOOGL', 'GOOG', 'BRK.B'].map((code, index) => ({ stockCode: code, stockName: '合成证券',
    market: 'US', currency: 'USD', shares: 100, weightPct: 5, marketValueYuan: 500, marketValueCurrency: 'CNY',
    sourceRank: index < 2 ? 1 : 2, sourceRankCell: index < 2 ? [2, 0, 49, 550, 690] : [2, 0, 49, 480, 549],
    sourcePage: 2, sourceEndPage: 2, sourceStockCode: code === 'BRK.B' ? 'BRK/B' : code,
    sourceExchange: '美国纽约证券交易所', sourceCountry: '美国' }));
  const proof = { sourceUrl, periodEnd: '2026-06-30', kind: 'semiannual', publishedAt: '2026-08-31',
    sourceTextHash: 'a'.repeat(64), parseMethod: 'pypdf', coverage: 'full', complete: true,
    sectionNumber: '7.4.1', nextSectionNumber: '7.5', sectionBounded: true, contiguousRanks: true, unitsVerified: true,
    rowsSeen: 3, parsedRows: 3, excludedRows: 0, startPage: 2, endPage: 3, totalPages: 10, processedPages: 10,
    documentTruncated: false, pageNumbersVerified: true, rankMode: 'pdf-cells', rankGroups: 2,
    mergedSecurityRows: 1, continuedRows: 0, marketValueCurrency: 'CNY' };
  const report = { fundCode: '000001', periodEnd: '2026-06-30', publishedAt: '2026-08-31', kind: 'semiannual',
    coverage: 'full', sourceUrl, holdingsSourceUrl: sourceUrl, holdingsRetrieval: 'pdf-stock-table-cells',
    holdingsPossiblyTruncated: false, holdingsPdfEvidence: proof, holdings, navYuan: 10000 };
  const barsByStock = Object.fromEntries(holdings.map(holding => [`US:${holding.stockCode}`, {
    stockCode: holding.stockCode, market: 'US', currency: 'USD', priceBasis: 'unadjusted',
    sourceUrl: 'https://example.org/prices', rows: [{ date: '2026-04-01', low: 10, high: 20, close: 15 }] }]));
  return { asOf: '2026-10-26', retrievedAt: '2026-10-26T10:00:00Z', errors: [], barsByStock,
    funds: [{ code: '000001', name: '合成基金', company: '合成公司', companyId: 'co1', managers: [],
      sourcePortfolioCode: '000001', shareClassCodes: ['000001'], portfolioSourceUrl: sourceUrl, reports: [report] }] };
}

test('US holdings proof, quoted USD and CNY positions survive build, merge and aggregate analysis', () => {
  const snapshot = buildSnapshot(fixture(), { now, config });
  const selected = snapshot.funds[0].selectedReport;
  assert.equal(selected.holdingsPdfEvidence.complete, true);
  assert.equal(selected.holdingsPdfEvidence.rankGroups, 2);
  assert.equal(selected.holdings[2].sourceStockCode, 'BRK/B');
  for (const row of snapshot.funds[0].rows) {
    assert.equal(row.priceRange.status, 'observed');
    assert.equal(row.priceRange.currency, 'USD');
    assert.equal(row.priceRange.isCost, false);
    assert.equal(row.position.currency, 'CNY');
    assert.equal(row.position.valueYuan, 500);
  }
  const merged = mergeSnapshots({ snapshots: [snapshot], config, now,
    catalog: { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
      funds: [{ code: '000001', name: '合成基金', companyCode: 'co1', companyName: '合成公司' }] } });
  const retained = merged.companySnapshots.co1;
  assert.deepEqual(retained.funds[0].selectedReport.holdingsPdfEvidence, selected.holdingsPdfEvidence);
  const manifest = { schemaVersion: 1, sourceMode: 'actions', ...config, asOf: snapshot.asOf, retrievedAt: snapshot.retrievedAt,
    coverage: { fundCount: 1, catalogFundCount: 1 }, companyFiles: [{ path: './data/companies/co1.json' }] };
  const aggregate = analyzeQuarter(manifest, () => retained, { now });
  assert.equal(aggregate.stocks.length, 3);
  const stock = aggregate.stocks.find(stock => stock.key === 'US:BRK.B');
  assert.equal(stock.currency, 'USD');
  assert.equal(stock.disclosedValueYuan, 500);
  assert.equal(stock.valueCoverage.unit, 'CNY');
});

test('altered rank geometry, market, source or row counts cannot prove a complete foreign table', () => {
  const mutations = [
    report => { report.holdings[1].sourceRankCell = [2, 0, 49, 400, 450]; },
    report => { report.holdings[0].sourceEndPage = 12; },
    report => { report.holdings[0].sourceCountry = '中国香港'; },
    report => { report.holdings[0].sourceStockCode = 'OTHER'; },
    report => { report.holdings[0].marketValueCurrency = 'USD'; },
    report => { delete report.holdings[0].sourceRank; },
    report => { report.holdingsPdfEvidence.mergedSecurityRows = 0; },
    report => { report.holdingsPdfEvidence.rankGroups = 3; },
    report => { report.holdingsPdfEvidence.continuedRows = 1; },
    report => { report.holdingsPdfEvidence.marketValueCurrency = 'USD'; },
    report => { report.holdingsRetrieval = 'pdf-stock-table-text'; },
  ];
  for (const mutate of mutations) {
    const input = fixture(); mutate(input.funds[0].reports[0]);
    const selected = buildSnapshot(input, { now, config }).funds[0].selectedReport;
    assert.equal(selected.holdingsPdfEvidence.complete, false);
    assert.equal(selected.holdingsPossiblyTruncated, true);
  }
});

test('foreign quote currency mismatch never becomes an observed range', () => {
  const input = fixture(); input.barsByStock['US:BRK.B'].currency = 'CNY';
  const fund = buildSnapshot(input, { now, config }).funds[0];
  assert.equal(fund.rows.find(row => row.stockCode === 'BRK.B').priceRange.status, 'unsupported-currency');
});
