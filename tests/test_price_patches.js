'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { analyzeQuarter } = require('../scripts/analyze-data');
const { bindingFor, cleanBinding, key } = require('../lib/price-patches');
const { expectedSessions } = require('../lib/price-coverage');
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001', companyCode: 'co1' }] };
const now = '2026-09-29T12:00:00Z', oldAt = '2026-09-29T10:00:00Z', freshAt = '2026-09-29T11:30:00Z';
const catalog = { companies: [{ code: 'co1', name: '合成机构', fundCount: 1 }], funds: [{ code: '000001', companyCode: 'co1', companyName: '合成机构' }] };
function initial() {
  const holdings = [{ market: 'CN', stockCode: '600001', currency: 'CNY', shares: 10, weightPct: 1 }];
  const reports = [['2026-06-30', '2026-07-20'], ['2026-03-31', '2026-04-20']].map(([periodEnd, publishedAt]) => ({
    periodEnd, publishedAt, kind: 'quarterly', sourceUrl: `https://example.org/${periodEnd}.pdf`, coverage: 'top10', navYuan: 10000, holdings }));
  const snapshot = buildSnapshot({ asOf: '2026-09-29', retrievedAt: oldAt, errors: [], barsByStock: {},
    funds: [{ code: '000001', name: '合成基金', company: '合成机构', companyId: 'co1', managers: [], reports }] }, { config, catalog, now });
  const first = mergeSnapshots({ snapshots: [snapshot], config, catalog, now });
  const fund = first.companySnapshots.co1.funds[0];
  return { first, fund, binding: bindingFor(fund, config) };
}
function patch(state, values = expectedSessions('CN', '2026-04-01', '2026-06-30'), mutation = {}, companyCode = 'co1') {
  const item = { ...config.funds[0], companyCode, priceOnly: state.binding, priceTargets: [{ market: 'CN', stockCode: '600001' }] };
  const batch = { ...config, funds: [item], traversal: { plannedAt: '2026-09-29T11:00:00Z' } };
  const raw = { asOf: '2026-09-29', retrievedAt: freshAt, funds: [], errors: [], pricePatches: [{ code: '000001', binding: state.binding,
    attemptedStockKeys: ['CN:600001'], attemptedAt: '2026-09-29T11:01:00Z', ...mutation }],
    barsByStock: { 'CN:600001': { stockCode: '600001', market: 'CN', currency: 'CNY', priceBasis: 'unadjusted',
      requestedStart: '2026-04-01', requestedEnd: '2026-06-30', sourceUrl: 'https://example.org/quotes',
      sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 }, rows: values.map(date => ({ date, low: 10, high: 20, close: 15 })) } } };
  return { result: buildSnapshot(raw, { config: batch, now }), batch };
}
function merge(state, snapshot) {
  return mergeSnapshots({ snapshots: [snapshot], previousManifest: state.first.manifest, previousCompanies: state.first.companySnapshots, config, catalog, now });
}
test('price-only publication carries only normalized ranges and updates no report observation', () => {
  const state = initial(), before = structuredClone(state.fund), result = patch(state).result;
  assert.equal(result.funds.length, 0); assert.equal(result.batchResults.length, 0);
  assert.equal(result.pricePatches[0].prices[0].priceRange.completeness, 'complete');
  assert.equal(JSON.stringify(result).includes('"rows"'), false);
  const merged = merge(state, result), fund = merged.companySnapshots.co1.funds[0];
  assert.equal(merged.counts.acceptedFreshFundCount, 0); assert.equal(merged.counts.acceptedPricePatchCount, 1);
  assert.equal(fund.retrievedAt, before.retrievedAt); assert.equal(fund.asOf, before.asOf);
  assert.deepEqual(fund.selectedReport, before.selectedReport); assert.deepEqual(fund.previousReport, before.previousReport);
  assert.deepEqual(fund.reportManagers, before.reportManagers);
  assert.equal(fund.priceUpdatedAt, freshAt); assert.equal(merged.manifest.retrievedAt, oldAt);
  assert.equal(merged.receipt.funds[0].priceCoverage.priceComplete, 1);
  assert.equal(merged.receipt.pricePatches[0].accepted, true);
  const analysis = analyzeQuarter(merged.manifest, () => merged.companySnapshots.co1, { now });
  assert.equal(analysis.priceUpdatedAt, freshAt);
  assert.equal(analysis.retrievedAt, oldAt);
  assert.equal(analysis.stocks[0].priceRange.completeness, 'complete');
  const replay = mergeSnapshots({ previousCompanies: merged.companySnapshots, previousManifest: merged.manifest, config, catalog, now,
    receiptCodes: ['000001'] });
  assert.equal(replay.receipt.funds[0].priceCoverage.priceComplete, 1);
});
test('wrong quarter, changed report URL and changed holdings universe cannot apply stale patches', () => {
  for (const mutate of [s => { s.fund.selectedReport.sourceUrl = 'https://example.org/revised.pdf'; },
    s => { s.fund.selectedReport.holdings.push({ market: 'CN', stockCode: '600002', currency: 'CNY', shares: 1, weightPct: 1 }); }]) {
    const state = initial(), result = patch(state).result;
    mutate(state);
    state.fund.reports = [state.fund.selectedReport, state.fund.previousReport];
    const merged = merge(state, result);
    assert.equal(merged.receipt.pricePatches[0].accepted, false);
    assert.equal(merged.receipt.pricePatches[0].errorCode, 'PRICE_BINDING_INVALID');
  }
  const state = initial(), result = patch(state).result;
  result.targetPeriodEnd = '2026-09-30';
  assert.equal(merge(state, result).receipt.pricePatches.length, 0);
});
test('new partial prices cannot replace a complete saved price-only update', () => {
  const state = initial(), first = merge(state, patch(state).result);
  const saved = first.companySnapshots.co1.funds[0].rows[0].priceRange;
  const partial = patch(state, ['2026-04-01']).result;
  const next = mergeSnapshots({ snapshots: [partial], previousManifest: first.manifest, previousCompanies: first.companySnapshots, config, catalog, now });
  assert.equal(next.changed, false); assert.equal(next.receipt.funds[0].priceCoverage.priceComplete, 1);
  assert.equal(saved.completeness, 'complete');
});
test('unstarted requests do not manufacture fetched rows, even when raw bars exist', () => {
  const state = initial(), result = patch(state, undefined, { attemptedStockKeys: [], attemptedAt: null }).result;
  assert.deepEqual(result.pricePatches[0].prices, []);
  assert.equal(result.pricePatches[0].errorCode, 'PRICE_BUDGET_REACHED');
  const merged = merge(state, result);
  assert.equal(merged.changed, false); assert.equal(merged.receipt.pricePatches[0].accepted, false);
});
test('unsupported extra patch metadata is discarded before public artifacts', () => {
  const state = initial(), result = patch(state, undefined, { rawPdfText: 'unpublished full text' }).result;
  assert.equal(JSON.stringify(result).includes('unpublished full text'), false);
  assert.equal(JSON.stringify(result).includes('rawPdfText'), false);
});
test('bindings reject missing selected identity, private source URLs and wrong symbol widths', () => {
  const state = initial();
  assert.equal(cleanBinding({ ...state.binding, selectedReport: null }, config, '000001'), null);
  assert.equal(key({ market: 'CN', stockCode: '00700' }), null);
  assert.equal(key({ market: 'HK', stockCode: '600001' }), null);
  for (const sourceUrl of ['https://localhost/report', 'https://127.0.0.1/report', 'https://10.0.0.1/report',
    'https://example.local/report', 'https://[::1]/report', 'https://example.org/report#secret',
    'https://example.org/report?token=', 'https://example.org/report?API_KEY=private']) {
    const binding = { ...state.binding, selectedReport: { ...state.binding.selectedReport, sourceUrl } };
    assert.equal(cleanBinding(binding, config, '000001'), null);
  }
});
test('wrong quote currency or truncated window cannot complete prices', () => {
  const state = initial();
  for (const change of [{ currency: 'USD' }, { rangeStart: '2026-04-02' }]) {
    const result = patch(state).result;
    Object.assign(result.pricePatches[0].prices[0].priceRange, change);
    const merged = merge(state, result);
    assert.equal(merged.receipt.funds[0].priceCoverage.priceComplete, 0);
  }
});
test('later strategy repair preserves independently newer complete quote observations', () => {
  const state = initial();
  Object.assign(state.fund.selectedReport, { kind: 'semiannual', coverage: 'full', publishedAt: '2026-08-28' });
  state.fund.reports = [state.fund.selectedReport, state.fund.previousReport];
  state.binding = bindingFor(state.fund, config);
  const quoted = merge(state, patch(state).result);
  const saved = quoted.companySnapshots.co1.funds[0];
  const sample = require('./fixtures/strategy-sections.json').complete;
  const at = '2026-09-29T11:40:00Z';
  const report = { ...saved.selectedReport, ...sample, kind: 'quarterly', coverage: 'top10',
    publishedAt: '2026-07-20', sourceUrl: 'https://example.org/strategy.pdf', strategyEvidenceRetrievedAt: at,
    narrative: { text: '我们增加中国平安的配置，因为估值较为合理。', sourceUrl: 'https://example.org/strategy.pdf',
      page: 7, periodStart: '2026-04-01', periodEnd: '2026-06-30' } };
  const incoming = buildSnapshot({ asOf: '2026-09-29', retrievedAt: at, errors: [], barsByStock: {},
    funds: [{ code: '000001', companyId: 'co1', reports: [report, saved.previousReport] }] }, { config, catalog, now });
  const merged = mergeSnapshots({ snapshots: [incoming], previousManifest: quoted.manifest,
    previousCompanies: quoted.companySnapshots, config, catalog, now });
  const fund = merged.companySnapshots.co1.funds[0];
  assert.equal(fund.selectedReport.coverage, 'full');
  assert.equal(fund.selectedReport.strategyEvidenceRetrievedAt, at);
  assert.equal(fund.retrievedAt, oldAt);
  assert.equal(fund.priceUpdatedAt, freshAt);
  assert.equal(fund.rows[0].priceRange.completeness, 'complete');
  assert.equal(fund.rows[0].priceRange.fetchedAt, freshAt);
});
test('unmapped directory company keeps one null identity across planning, publication and merge', () => {
  const unknownCatalog = { companies: [], funds: [{ code: '000001', companyCode: null }] };
  const seed = initial().fund;
  const snapshot = buildSnapshot({ asOf: '2026-09-29', retrievedAt: oldAt, errors: [], barsByStock: {},
    funds: [{ code: '000001', reports: [seed.selectedReport, seed.previousReport] }] }, { config, catalog: unknownCatalog, now });
  const first = mergeSnapshots({ snapshots: [snapshot], config, catalog: unknownCatalog, now });
  const fund = first.companySnapshots.unknown.funds[0], binding = bindingFor(fund, config);
  assert.equal(fund.companyId, 'unknown'); assert.equal(binding.companyCode, null);
  const incoming = patch({ binding }, undefined, {}, null).result;
  assert.equal(incoming.pricePatches[0].binding.companyCode, null);
  const merged = mergeSnapshots({ snapshots: [incoming], previousManifest: first.manifest,
    previousCompanies: first.companySnapshots, config, catalog: unknownCatalog, now });
  assert.equal(merged.receipt.pricePatches[0].accepted, true);
  assert.equal(merged.companySnapshots.unknown.funds[0].rows[0].priceRange.completeness, 'complete');
});
