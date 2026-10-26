'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSnapshot } = require('../scripts/build-data');
const { analyzeQuarter } = require('../scripts/analyze-data');
const { validateDataset } = require('../web/app');
const evidence = require('./fixtures/zero-stock.json');

test('zero-stock report retains previous stock disclosures without inventing zero shares or a sale', () => {
  const now = '2026-10-26T12:00:00Z', prior = { fundCode: '000001', periodEnd: '2026-03-31',
    publishedAt: '2026-04-20', sourceUrl: 'https://example.org/prior.pdf', kind: 'quarterly', coverage: 'top10', navYuan: 10000,
    holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 }] };
  const snapshot = buildSnapshot({ asOf: '2026-10-26', retrievedAt: now, errors: [], barsByStock: {},
    funds: [{ code: '000001', name: '合成基金', company: '合成公司', companyId: 'co1', managers: [],
      reports: [prior], noEquitiesEvidence: evidence }] },
    { now, config: { funds: [{ code: '000001' }], targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' } });
  const fund = snapshot.funds[0];
  assert.equal(fund.status, 'no-equities');
  assert.equal(fund.rows.length, 1);
  assert.equal(fund.rows[0].previous.shares, 100);
  assert.equal(fund.rows[0].current, null);
  assert.equal(fund.rows[0].change.sharesDelta, null);
  assert.equal(fund.summary.leftDisclosureCount, 1);
  assert.doesNotThrow(() => validateDataset(snapshot));
  const parts = [];
  const manifest = { ...snapshot, targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31',
    companyFiles: [{ path: './data/companies/co1.json' }] };
  const analysis = analyzeQuarter(manifest, () => manifest, { now, writePart: (_path, part) => parts.push(part) });
  assert.equal(analysis.stocks[0].currentPortfolioCount, 0);
  assert.equal(analysis.stocks[0].previousPortfolioCount, 1);
  assert.equal(analysis.stocks[0].changes.leftDisclosure, 1);
  const contribution = parts.find(part => part.kind === 'stock-contributions').stocks[0].contributions[0];
  assert.equal(contribution.provesTrade, false);
  fund.rows[0].current = fund.rows[0].previous;
  assert.throws(() => validateDataset(snapshot));
});
