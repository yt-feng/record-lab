'use strict';

// Deliberately synthetic fixtures; these are not public fund records.
const test = require('node:test');
const assert = require('node:assert/strict');
const { analyzeFund, estimatePosition, priceRange, selectReports, validDate } = require('../lib/fund-analysis');

const holding = (changes = {}) => ({ stockCode: '600001', stockName: '测试证券', market: 'CN', currency: 'CNY',
  shares: 100, weightPct: 5, marketValueYuan: 500, ...changes });
const report = (changes = {}) => ({ periodEnd: '2026-06-30', publishedAt: '2026-07-20', kind: 'quarterly',
  coverage: 'top10', navYuan: 10000, sourceUrl: 'https://example.org/report.pdf', title: '合成测试报告',
  holdings: [holding()], ...changes });
const prior = (changes = {}) => report({ periodEnd: '2026-03-31', publishedAt: '2026-04-20', ...changes });
const fund = (reports, changes = {}) => ({ code: 'TEST001', name: '合成测试基金', company: '测试公司', managers: [], reports, ...changes });
const options = (changes = {}) => ({ asOf: '2026-09-29', barsByStock: {}, ...changes });
const series = (rows, changes = {}) => ({ currency: 'CNY', priceBasis: 'unadjusted',
  sourceUrl: 'https://example.org/prices', rows, ...changes });

test('strict calendar validation rejects impossible, non-padded, and non-date inputs', () => {
  assert.equal(validDate('2024-02-29'), true);
  for (const date of ['2026-02-29', '2026-04-31', '2026-6-30', '2026-06-30T00:00:00Z', '', null]) {
    assert.equal(validDate(date), false);
  }
  assert.throws(() => analyzeFund(fund([]), { asOf: '2026-02-30' }), /INVALID_AS_OF/);
});

test('latest report obeys as-of publication and period dates, including selected historic date', () => {
  const futurePublication = report({ periodEnd: '2026-09-30', publishedAt: '2026-10-20' });
  const embargoed = report({ periodEnd: '2026-06-30', publishedAt: '2026-09-30', coverage: 'full' });
  const invalid = report({ periodEnd: '2026-06-31' });
  const result = analyzeFund(fund([futurePublication, embargoed, invalid, report(), prior()]), options());
  assert.equal(result.selectedReport.publishedAt, '2026-07-20');
  assert.equal(result.previousReport.periodEnd, '2026-03-31');
  assert.equal(analyzeFund(fund([report(), prior()]), options({ periodEnd: '2026-03-31' })).selectedReport.periodEnd, '2026-03-31');
  assert.equal(analyzeFund(fund([futurePublication]), options()).status, 'unavailable');
});

test('full report wins on same date; same-date version never becomes prior quarter', () => {
  const full = report({ coverage: 'full', kind: 'semiannual', publishedAt: '2026-08-28', holdings: [holding({ shares: 101 })] });
  const result = selectReports([report(), full, prior()], options());
  assert.equal(result.selectedReport.coverage, 'full');
  assert.equal(result.selectedReport.holdings[0].shares, 101);
  assert.equal(result.previousReport.periodEnd, '2026-03-31');
});

test('top-ten entries and exits remain disclosure changes, never zero or proved trades', () => {
  const result = analyzeFund(fund([report({ holdings: [holding({ stockCode: '600002' })] }), prior()]), options());
  assert.equal(result.rows.length, 2);
  const entered = result.rows.find(row => row.stockCode === '600002');
  const exited = result.rows.find(row => row.stockCode === '600001');
  assert.equal(entered.change.kind, 'entered-disclosure');
  assert.equal(entered.previous, null);
  assert.equal(entered.change.sharesDelta, null);
  assert.equal(exited.change.kind, 'left-disclosure');
  assert.equal(exited.current, null);
  assert.equal(exited.position.valueYuan, null);
  assert.equal(exited.change.sharesDelta, null);
  assert.equal(exited.change.provesTrade, false);
});

test('known shares and weights produce distinct share delta and percentage-point delta', () => {
  const result = analyzeFund(fund([report({ holdings: [holding({ shares: 120, weightPct: 4 })] }), prior()]), options());
  assert.equal(result.rows[0].change.kind, 'increase');
  assert.equal(result.rows[0].change.label, '持股增加');
  assert.equal(result.rows[0].change.sharesDelta, 20);
  assert.equal(result.rows[0].change.sharesPctChange, 20);
  assert.equal(result.rows[0].change.weightDeltaPp, -1);
  assert.equal(result.rows[0].change.provesTrade, false);
  assert.match(result.rows[0].change.reason, /送转股/);
});

test('invalid or unknown shares never become zero, but known weight difference remains', () => {
  for (const invalid of [null, undefined, NaN, Infinity, -1, '100']) {
    const result = analyzeFund(fund([report({ holdings: [holding({ shares: invalid, weightPct: 4 })] }), prior()]), options());
    assert.equal(result.rows[0].change.sharesDelta, null);
    assert.equal(result.rows[0].change.kind, 'not-comparable');
    assert.equal(result.rows[0].change.weightDeltaPp, -1);
  }
});

test('actual disclosed zero is valid and zero baseline has no percentage growth', () => {
  const result = analyzeFund(fund([report(), prior({ holdings: [holding({ shares: 0 })] })]), options());
  assert.equal(result.rows[0].change.sharesDelta, 100);
  assert.equal(result.rows[0].change.sharesPctChange, null);
  assert.equal(estimatePosition(holding({ marketValueYuan: 0 }), report()).valueYuan, 0);
});

test('missing baseline is explicit and uses current quarter only as price reference', () => {
  const result = analyzeFund(fund([report()]), options());
  assert.equal(result.rows[0].change.kind, 'no-baseline');
  assert.equal(result.rows[0].change.label, '基期缺失');
  assert.equal(result.interval.start, '2026-04-01');
  assert.equal(result.interval.adjacent, null);
  assert.equal(result.rows[0].priceRange.actualCostKnown, false);
});

test('nonadjacent baseline creates an explicit multi-quarter interval', () => {
  const old = report({ periodEnd: '2025-12-31', publishedAt: '2026-03-20', kind: 'annual', coverage: 'full' });
  const result = analyzeFund(fund([report(), old]), options());
  assert.deepEqual(result.interval, { start: '2026-01-01', end: '2026-06-30', adjacent: false,
    quarterSpan: 2, label: '跨 2 个季度的披露间隔（非单季）' });
  assert.equal(result.rows[0].priceRange.actualCostKnown, false);
});

test('position uses same-report NAV and disclosed value takes precedence with discrepancy', () => {
  const result = analyzeFund(fund([report({ navYuan: 20000 }), prior({ navYuan: 900000 })]), options());
  assert.equal(result.rows[0].position.estimatedYuan, 1000);
  assert.equal(result.rows[0].position.disclosedYuan, 500);
  assert.equal(result.rows[0].position.valueYuan, 500);
  assert.equal(result.rows[0].position.discrepancyYuan, 500);
  assert.equal(result.rows[0].position.discrepancyPct, 100);
  const estimated = estimatePosition(holding({ marketValueYuan: null }), report());
  assert.equal(estimated.valueYuan, 500);
  assert.equal(estimated.method, 'nav-weight');
  for (const navYuan of [null, 0, -1, NaN, Infinity, '10000']) {
    assert.equal(estimatePosition(holding({ marketValueYuan: null }), report({ navYuan })).valueYuan, null);
  }
  assert.equal(estimatePosition(holding({ marketValueYuan: null, weightPct: null }), report()).valueYuan, null);
  const rounded = estimatePosition(holding({ marketValueYuan: 50, weightPct: 0 }), report());
  assert.equal(rounded.estimatedYuan, 0);
  assert.equal(rounded.valueYuan, 50);
  assert.match(rounded.estimateNote, /四舍五入/);
});

test('unadjusted prices are bounded inclusively and missing days stay unverified', () => {
  const rows = [
    { date: '2026-03-31', low: 1, high: 999 },
    { date: '2026-04-01', low: 8, high: 10 },
    { date: '2026-06-30', low: 7, high: 12 },
    { date: '2026-07-01', low: 1, high: 999 },
    { date: '2026-05-01', low: null, high: 20 },
    { date: '2026-05-02', low: 20, high: 19 },
    { date: '2026-06-30', low: 2, high: 40 },
  ];
  const result = analyzeFund(fund([report(), prior()]), options({ barsByStock: { '600001': series(rows) } })).rows[0].priceRange;
  assert.equal(result.low, 7);
  assert.equal(result.high, 12);
  assert.equal(result.observedCount, 2);
  assert.equal(result.rejectedRowCount, 3);
  assert.equal(result.observedStart, '2026-04-01');
  assert.equal(result.observedEnd, '2026-06-30');
  assert.equal(result.completeness, 'not-verified');
  assert.equal(result.isCost, false);
});

test('currency, market, and adjustment mismatches do not manufacture price ranges', () => {
  const interval = { start: '2026-04-01', end: '2026-06-30' };
  const bars = [{ date: '2026-04-01', low: 8, high: 10 }];
  assert.equal(priceRange(holding(), series(bars, { priceBasis: 'forward-adjusted' }), interval).status, 'invalid-price-basis');
  const mismatch = priceRange(holding(), series(bars, { currency: 'HKD' }), interval);
  assert.equal(mismatch.status, 'unsupported-currency');
  assert.equal(mismatch.low, null);
  assert.equal(priceRange(holding(), null, interval).status, 'missing');
  assert.equal(priceRange(holding(), series([]), interval).low, null);
  assert.equal(priceRange(holding(), series(bars), null).status, 'unavailable-interval');
});

test('market-qualified codes preserve separate securities and quote currencies', () => {
  const result = analyzeFund(fund([report({ holdings: [holding({ stockCode: '00001' }),
    holding({ stockCode: '00001', market: 'HK', currency: 'HKD' })] })]), options({ barsByStock: {
    'CN:00001': series([{ date: '2026-04-01', low: 1, high: 2 }]),
    'HK:00001': series([{ date: '2026-04-01', low: 10, high: 20 }], { currency: 'HKD' }),
  } }));
  assert.equal(result.rows.length, 2);
  assert.equal(result.rows[0].priceRange.low, 1);
  assert.equal(result.rows[1].priceRange.low, 10);
  assert.equal(result.rows[1].priceRange.currency, 'HKD');
  assert.equal(result.rows[1].position.currency, 'CNY');
});

test('report narrative remains attributed, bounded, and separate from numeric inference', () => {
  const source = fund([report({ narrative: { text: '报告摘录'.repeat(200), page: 8,
    sourceUrl: 'https://example.org/primary.pdf', periodStart: '2026-01-01', periodEnd: '2026-06-30' } })]);
  const snapshot = JSON.stringify(source);
  const result = analyzeFund(source, options());
  assert.equal(result.narrative.text.length, 600);
  assert.equal(result.narrative.truncated, true);
  assert.equal(result.narrative.attribution, 'report');
  assert.equal(result.narrative.isInference, false);
  assert.equal(result.narrative.page, 8);
  assert.equal(result.narrative.periodStart, '2026-01-01');
  assert.equal(JSON.stringify(source), snapshot);
});

test('duplicate lines and A/C share classes never multiply positions', () => {
  const result = analyzeFund(fund([report({ holdings: [holding(), holding()] })], { shareClassCodes: ['TEST001', 'TEST002'] }), options());
  assert.equal(result.rows.length, 1);
  assert.equal(result.summary.knownPositionYuan, 500);
  assert.equal(result.summary.aggregatesShareClasses, false);
  assert.deepEqual(result.shareClassCodes, ['TEST001', 'TEST002']);
  assert.equal(result.selectedReport.duplicateHoldingCount, 1);
});

test('all missing position values yield unknown total, not a fictitious zero', () => {
  const result = analyzeFund(fund([report({ navYuan: null, holdings: [holding({ marketValueYuan: null })] })]), options());
  assert.equal(result.summary.knownPositionYuan, null);
  assert.equal(result.summary.knownPositionCount, 0);
  const empty = analyzeFund(fund([]), options());
  assert.deepEqual(empty.rows, []);
  assert.equal(empty.selectedReport, null);
  assert.equal(empty.interval, null);
  assert.equal(empty.summary.knownPositionYuan, null);
});

test('current-profile managers are never silently attributed to historical reports', () => {
  const result = analyzeFund(fund([report()], { managers: [{ name: '测试经理' }],
    managersAsOf: '2026-09-29', metadataSourceUrl: 'https://example.org/profile' }), options());
  assert.equal(result.managers.length, 1);
  assert.deepEqual(result.reportManagers, []);
  assert.equal(result.managerAttribution, 'current-profile');
  assert.equal(result.managersAsOf, '2026-09-29');
  assert.equal(result.managerSourceUrl, 'https://example.org/profile');
  assert.ok(result.warnings.some(message => message.includes('任期未核验')));
});

test('report managers use explicit report names or date-overlapping documented tenures', () => {
  const managers = [{ name: '已离任', startDate: '2025-01-01', endDate: '2026-03-31' },
    { name: '当期任职', startDate: '2026-04-01', endDate: null },
    { name: '后来上任', startDate: '2026-07-01', endDate: null }, { name: '任期不明' }];
  const result = analyzeFund(fund([report()], { managers }), options());
  assert.deepEqual(result.reportManagers.map(manager => manager.name), ['当期任职']);
  assert.equal(result.managerAttribution, 'tenure-overlap');
  const explicit = analyzeFund(fund([report({ managers: [{ name: '报告披露' }] })], { managers }), options());
  assert.deepEqual(explicit.reportManagers.map(manager => manager.name), ['报告披露']);
  assert.equal(explicit.managerAttribution, 'report');
  assert.equal(explicit.managerSourceUrl, 'https://example.org/report.pdf');
});

test('holdings-table and NAV report provenance remain distinct', () => {
  const result = analyzeFund(fund([report({ holdingsSourceUrl: 'https://example.org/holdings',
    holdingsRetrieval: 'structured-table', navSourceUrl: 'https://example.org/nav.pdf',
    narrative: { text: '合成短摘录', excerptSelection: 'operation-sentences' } })]), options());
  assert.equal(result.selectedReport.holdingsSourceUrl, 'https://example.org/holdings');
  assert.equal(result.selectedReport.navSourceUrl, 'https://example.org/nav.pdf');
  assert.equal(result.selectedReport.holdingsRetrieval, 'structured-table');
  assert.equal(result.narrative.excerptSelection, 'operation-sentences');
});

test('report parser evidence survives strict normalization with bounded fields', () => {
  const result = analyzeFund(fund([report({
    parseMethod: 'mineru',
    sourceTextHash: 'A'.repeat(64),
    pageStats: { pageCount: 42, nonEmptyPages: 38, charCount: 10000, markdownBytes: 20000 },
    strategyExcerpts: [{ text: '本基金依据估值调整组合。'.repeat(30), sourceUrl: 'https://example.org/report.pdf',
      page: 17, periodStart: '2026-04-01', periodEnd: '2026-06-30' }],
    strategyThemes: ['调仓/交易', 'unknown'],
  })]), options());
  assert.equal(result.selectedReport.parseMethod, 'mineru');
  assert.equal(result.selectedReport.sourceTextHash, 'a'.repeat(64));
  assert.equal(result.selectedReport.pageStats.pageCount, 42);
  assert.equal(result.selectedReport.strategyExcerpts[0].page, 17);
  assert.equal(result.selectedReport.strategyExcerpts[0].text.length, 260);
  assert.deepEqual(result.selectedReport.strategyThemes, ['调仓/交易']);
});
