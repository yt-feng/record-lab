'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { chinaDate, chinaDayEnd } = require('../web/source-dates');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { analyzeQuarter, strategyFor } = require('../scripts/analyze-data');
const { normalizePortfolioIdentity } = require('../lib/portfolio-identity');
const { normalizeProfileInception, normalizeInception } = require('../lib/historical-inception');
const app = require('../web/app'), ui = require('../web/analysis');
const NOW = '2026-10-26T16:15:00Z';
const OBSERVATIONS = ['2026-10-26T16:10:00Z', '2026-10-27T00:10:00+08:00', '2026-10-26T12:10:00-04:00'];
const config = { funds: [{ code: '000001' }], targetPeriodEnd: '2026-09-30', baselinePeriodEnd: '2026-06-30' };
const catalog = { schemaVersion: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 1, directoryStatus: 'complete' }],
  funds: [{ code: '000001', name: '合成基金', companyCode: 'co1' }] };
const clone = value => JSON.parse(JSON.stringify(value));
function raw(observed = OBSERVATIONS[0]) {
  return { asOf: '2026-10-27', retrievedAt: observed, funds: [{ code: '000001', name: '合成基金', companyId: 'co1', reports: [{
    fundCode: '000001', kind: 'quarterly', coverage: 'top10', navYuan: 10000, periodEnd: config.targetPeriodEnd,
    publishedAt: '2026-10-27', sourceUrl: 'https://example.org/q3.pdf', holdings: [{ stockCode: '600001', stockName: '合成证券',
      market: 'CN', currency: 'CNY', shares: 100, weightPct: 1, marketValueYuan: 100 }] }] }], barsByStock: {}, errors: [] };
}
function analyze(merged, now = NOW) {
  return analyzeQuarter(merged.manifest, file => merged.companySnapshots[path.basename(file, '.json')], { now, writePart: () => {} });
}

test('China date helpers use instants and reject ambiguous dates, while day-end is UTC+8', () => {
  for (const observed of OBSERVATIONS) assert.equal(chinaDate(observed), '2026-10-27');
  assert.equal(chinaDate('2026-10-26T15:59:59Z'), '2026-10-26');
  assert.equal(chinaDate('2026-10-26T16:00:00Z'), '2026-10-27');
  for (const value of [null, '', '2026-10-27', '2026-10-27T00:10:00', '2026-02-31T00:00:00Z', new Date(NaN)]) assert.equal(chinaDate(value), null);
  assert.equal(new Date(chinaDayEnd('2026-10-27')).toISOString(), '2026-10-27T15:59:59.999Z');
  assert.ok(Number.isNaN(chinaDayEnd('2026-02-31')));
});

test('midnight build, merge, analysis, Python quality and receipt preserve one observed report', () => {
  for (const observed of OBSERVATIONS) {
    const part = buildSnapshot(raw(observed), { config, now: NOW });
    const merged = mergeSnapshots({ snapshots: [part], catalog, config, now: NOW });
    assert.equal(merged.manifest.coverage.fundCount, 1);
    assert.equal(merged.manifest.asOf, '2026-10-27');
    const fund = merged.companySnapshots.co1.funds[0];
    assert.equal(fund.retrievedAt, observed);
    for (const now of [NOW, '2026-10-27T02:00:00Z']) {
      const analysis = analyze(merged, now);
      assert.equal(analysis.coverage.analyzedRecords, 1);
      assert.equal(analysis.retrievedAt, observed);
      assert.equal(ui.validate(analysis, '2026Q3'), analysis);
      const python = spawnSync('python3', ['-c', `import json,sys
sys.path.insert(0,'scripts')
from traversal import report_quality,report_evidence,valid_event_evidence,instant
a=json.load(sys.stdin);now=instant(a['now']);p=a['config'];f=a['fund']
status,e=report_evidence(f,p,now)
print(json.dumps({'quality':report_quality(f,p,now)is not None,'valid':valid_event_evidence(e,'complete',p,now),'observedAt':e['observedAt']}))`],
        { cwd: path.resolve(__dirname, '..'), input: JSON.stringify({ fund, config, now }), encoding: 'utf8' });
      assert.equal(python.status, 0, python.stderr);
      assert.deepEqual(JSON.parse(python.stdout), { quality: true, valid: true, observedAt: observed });
    }
  }
});

test('future business dates and genuinely future observation instants remain rejected', () => {
  assert.throws(() => buildSnapshot({ ...raw(), asOf: '2026-10-28' }, { config, now: NOW }), /FUTURE_AS_OF/);
  assert.throws(() => buildSnapshot(raw('2026-10-26T16:21:00Z'), { config, now: NOW }), /INVALID_RETRIEVED_AT/);
  const futureReport = raw(); futureReport.funds[0].reports[0].publishedAt = '2026-10-28';
  assert.throws(() => buildSnapshot(futureReport, { config, now: NOW }), /NO_VALID_FRESH_FUNDS/);
  const part = buildSnapshot(raw(), { config, now: NOW });
  const dropped = mergeSnapshots({ snapshots: [{ ...part, asOf: '2026-10-28' }], catalog, config, now: NOW });
  assert.equal(dropped.manifest.coverage.fundCount, 0);
  const merged = mergeSnapshots({ snapshots: [part], catalog, config, now: NOW });
  assert.throws(() => analyze({ ...merged, manifest: { ...merged.manifest, asOf: '2026-10-28' } }), /ANALYSIS_FUTURE_OBSERVATION/);
  merged.companySnapshots.co1.funds[0].selectedReport.publishedAt = '2026-10-28';
  assert.throws(() => analyze(merged), /ANALYSIS_FUTURE_REPORT/);
});

test('strategy analyzer and both UI views agree across timestamp offsets', () => {
  const sample = clone(require('./fixtures/strategy-sections.json').complete);
  sample.strategySectionCoverage.publishedAt = '2026-10-27';
  for (const observed of OBSERVATIONS) {
    const report = { ...sample, periodEnd: '2026-06-30', strategyEvidenceRetrievedAt: observed };
    const strategy = strategyFor({ selectedReport: report, retrievedAt: observed }, { start: '2026-04-01', target: '2026-06-30', generation: NOW });
    assert.equal(strategy.sectionStatus, 'complete');
    assert.equal(strategy.evidenceRetrievedAt, observed);
    assert.equal(app.strategyEvidence(report, '2026-10-27', observed).complete, true);
    assert.equal(ui.strategyEvidence(strategy, '2026-06-30', '2026-10-27').complete, true);
    assert.equal(ui.strategyEvidence(strategy, '2026-06-30', '2026-10-26').complete, false);
  }
  const tooLate = { ...sample, periodEnd: '2026-06-30', strategyEvidenceRetrievedAt: '2026-10-27T16:10:00Z' };
  assert.equal(app.strategyEvidence(tooLate, '2026-10-27').complete, false);
});

test('portfolio identity and founding-date evidence preserve timestamps with China date validation', () => {
  const fixture = clone(require('./fixtures/portfolio-identity.json'));
  fixture.report.publishedAt = fixture.proof.publishedAt = '2026-10-27';
  for (const observed of OBSERVATIONS) {
    fixture.proof.retrievedAt = observed;
    assert.equal(normalizePortfolioIdentity(fixture.proof, fixture.report, new Date(NOW)).retrievedAt, observed);
    const inception = { schemaVersion: 1, fundCode: '000001', date: '2026-10-27',
      sourceUrl: 'https://fundf10.eastmoney.com/jbgk_000001.html', retrievedAt: observed };
    assert.deepEqual(normalizeProfileInception(inception, '000001', new Date(NOW)), inception);
    assert.equal(normalizeProfileInception({ ...inception, date: '2026-10-28' }, '000001', new Date(NOW)), null);
  }
  fixture.report.publishedAt = fixture.proof.publishedAt = '2026-10-28';
  assert.equal(normalizePortfolioIdentity(fixture.proof, fixture.report, new Date(NOW)), null);
  const history = { schemaVersion: 1, fundCode: '000001', periodEnd: '2026-06-30', date: '2026-04-01',
    sourceId: 'csrc-fund-products-20260630', sourceUrl: 'https://www.csrc.gov.cn/csrc/c101900/c1029655/1029655/files/公募基金产品索引（截至20260630）.xlsx',
    contentHash: '0f6ac234b792bb6334c0c94ec37fa6c0548363c80017c946d11a5c7e95d3a9e0', sheet: 'Sheet1', row: 3,
    retrievedAt: '2026-06-29T16:10:00Z' };
  const product = { code: '000001', inceptionDate: history.date, inceptionSourceUrl: history.sourceUrl };
  assert.deepEqual(normalizeInception(history, product, new Date('2026-06-29T16:15:00Z')), history);
  assert.equal(normalizeInception({ ...history, retrievedAt: '2026-06-29T15:59:59Z' }, product, new Date('2026-06-29T16:15:00Z')), null);
});
