'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeReport } = require('../lib/fund-analysis');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');

// Synthetic documents only. Holdings and strategy deliberately cite different PDFs.
const now = '2026-10-26T11:00:00Z';
const config = { funds: [{ code: '000001' }], targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const proof = (changes = {}) => ({ sourceUrl: 'https://example.org/interim.pdf', periodEnd: '2026-06-30',
  kind: 'semiannual', publishedAt: '2026-08-31', sourceTextHash: 'a'.repeat(64), parseMethod: 'pypdf',
  coverage: 'full', complete: true, sectionNumber: '7.3', nextSectionNumber: '7.4', sectionBounded: true,
  contiguousRanks: true, unitsVerified: true, rowsSeen: 1, parsedRows: 1, excludedRows: 0,
  startPage: 2, endPage: 3, totalPages: 10, processedPages: 10, documentTruncated: false,
  pageNumbersVerified: true, ...changes });
const report = (changes = {}) => ({ fundCode: '000001', periodEnd: '2026-06-30', publishedAt: '2026-08-31',
  kind: 'semiannual', coverage: 'full', sourceUrl: 'https://example.org/interim.pdf',
  holdingsSourceUrl: 'https://example.org/interim.pdf', holdingsRetrieval: 'pdf-stock-table-text',
  holdingsPossiblyTruncated: false, holdingsPdfEvidence: proof(), navYuan: 10000,
  holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY', shares: 100,
    marketValueYuan: 500, weightPct: 5 }],
  reportPdfSourceUrl: 'https://example.org/quarter.pdf', reportPdfPeriodEnd: '2026-06-30', reportPdfKind: 'quarterly',
  parseMethod: 'pypdf', sourceTextHash: 'b'.repeat(64),
  pageStats: { pageCount: 8, totalPages: 8, processedPages: 8, truncated: false, nonEmptyPages: 8,
    charCount: 1000, textStatus: 'usable', pageNumbersVerified: true },
  narrative: { text: '本基金本季度调整行业配置，并保持适度流动性。', sourceUrl: 'https://example.org/quarter.pdf',
    page: 4, periodStart: '2026-04-01', periodEnd: '2026-06-30' }, ...changes });
const build = document => buildSnapshot({ asOf: '2026-10-26', retrievedAt: '2026-10-26T10:00:00Z',
  funds: [{ code: '000001', name: '合成基金', company: '合成公司', managers: [], reports: [document] }],
  errors: [], barsByStock: {} }, { now, config });

test('holdings PDF completeness and its own source survive build and company merge', () => {
  const snapshot = build(report());
  const selected = snapshot.funds[0].selectedReport;
  assert.equal(selected.holdingsPdfEvidence.complete, true);
  assert.equal(selected.holdingsPdfEvidence.sourceTextHash, 'a'.repeat(64));
  assert.equal(selected.sourceTextHash, 'b'.repeat(64));
  assert.equal(selected.reportPdfSourceUrl, 'https://example.org/quarter.pdf');
  assert.equal(selected.holdingsPossiblyTruncated, false);
  const merged = mergeSnapshots({ snapshots: [snapshot], config, now,
    catalog: { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
      funds: [{ code: '000001', name: '合成基金', companyCode: 'co1', companyName: '合成公司' }] } });
  const retained = merged.companySnapshots.co1.funds[0].selectedReport;
  assert.deepEqual(retained.holdingsPdfEvidence, selected.holdingsPdfEvidence);
  assert.equal(retained.coverage, 'full');
  assert.equal(retained.holdingsPossiblyTruncated, false);
});

test('missing or inconsistent PDF proof cannot clear an incomplete-stock-table flag', () => {
  const changes = [{ complete: false }, { sourceUrl: 'https://example.org/other.pdf' }, { kind: 'quarterly' },
    { periodEnd: '2026-09-30' }, { publishedAt: '2026-08-30' }, { sourceTextHash: 'bad' },
    { startPage: 11 }, { endPage: 1 }, { excludedRows: 1 }, { parsedRows: 2 }, { rowsSeen: 2 },
    { contiguousRanks: false }, { unitsVerified: false }, { documentTruncated: true },
    { pageNumbersVerified: false }, { totalPages: 11 }, { nextSectionNumber: '7.2' },
    { coverage: 'unknown' }, { parseMethod: 'mineru' }];
  for (const change of changes) {
    const selected = build(report({ holdingsPdfEvidence: proof(change) })).funds[0].selectedReport;
    assert.equal(selected.holdingsPdfEvidence.complete, false, JSON.stringify(change));
    assert.equal(selected.holdingsPossiblyTruncated, true, JSON.stringify(change));
    assert.equal(selected.coverage, 'unknown', JSON.stringify(change));
  }
  const missing = build(report({ holdingsPdfEvidence: undefined })).funds[0].selectedReport;
  assert.equal(missing.holdingsPossiblyTruncated, true);
  const invalid = normalizeReport(report({ holdings: [{ stockCode: 'AAPL', market: 'US', currency: 'USD',
    shares: 100, weightPct: 5, marketValueYuan: 500 }] }));
  assert.equal(invalid.holdingsPdfEvidence.complete, false);
  assert.throws(() => build(report({ holdingsPdfEvidence: proof({ sourceUrl: 'file:private.pdf' }) })),
    { code: 'NO_VALID_FRESH_FUNDS' });
});
