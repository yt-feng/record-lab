'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor } = require('../lib/strategy-evidence-pool');
const NOW = '2026-10-27T02:00:00Z';
const python = spawnSync('python3', ['-c', [
  'import json,sys', 'sys.path[:0]=["tests","scripts"]',
  'from test_strategy_evidence_pool import fixture,extracted,START,END',
  'print(json.dumps({"basic":fixture(),"extracted":extracted(),"emoji":fixture(pages=[START+"本基金增配科技行业😀，","并保持审慎。"+END],page_numbers=[6,7]),"boundary":fixture("本基金增配科技行业"+"😀"*150+"。")},ensure_ascii=False))',
].join('\n')], { cwd: path.join(__dirname, '..'), encoding: 'utf8', maxBuffer: 200000 });
assert.equal(python.status, 0, python.stderr);
const fixtures = JSON.parse(python.stdout);
const clone = value => structuredClone(value);

test('Python extraction pool roundtrips through strict JS admission with all public projections', () => {
  for (const report of Object.values(fixtures)) {
    assert.deepEqual(normalizeStrategyEvidencePool(report.strategyEvidencePool, report.strategySectionCoverage, {}, NOW), report.strategyEvidencePool);
    assert.deepEqual(strategyEvidenceBudgetFor(report, NOW), report.strategyEvidenceBudget);
    assert.equal(strategyEvidenceBudgetFor(report, NOW).status, 'verified');
    assert.equal(report.strategyClaims.semanticAnalysisComplete, false);
  }
  assert.equal(fixtures.extracted.strategyClaimsIdentityEvidence.retrievedAt, fixtures.extracted.strategyEvidencePool.source.retrievedAt);
});

test('Unicode budgets count codepoints and never cut surrogate pairs', () => {
  const report = fixtures.boundary, quote = report.strategyEvidencePool.quotes[0];
  assert.equal(Array.from(quote.text).length, 160); assert.ok(quote.text.length > 160);
  assert.equal(strategyEvidenceBudgetFor(report, NOW).status, 'verified');
  assert.equal(fixtures.emoji.strategyEvidencePool.quotes[0].pages.join(','), '6,7');
  const bad = clone(report); bad.strategyEvidencePool.quotes[0].text += '😀';
  assert.equal(normalizeStrategyEvidencePool(bad.strategyEvidencePool, bad.strategySectionCoverage, {}, NOW), null);
});

test('body, hash, page, extra field and independent text projection mutations are not admitted', () => {
  const mutations = [
    report => report.strategyEvidencePool.quotes[0].text += '伪造原因',
    report => report.strategyEvidencePool.quotes[0].textHash = '0'.repeat(64),
    report => report.strategyEvidencePool.quotes[0].pages = [9000],
    report => report.strategyEvidencePool.fullText = 'must not pass',
    report => report.narrative.text = '独立未计费正文',
    report => report.strategyAnalysis.evidence[0].evidencePoolId = 'q' + '0'.repeat(64),
    report => report.strategyClaims.evidence[0].sourceUrl = 'https://example.org/other.pdf',
    report => report.strategyExcerpts.push(clone(report.strategyExcerpts[0]), ...Array(8).fill(null)),
  ];
  for (const mutate of mutations) { const value = clone(fixtures.basic); mutate(value); assert.equal(strategyEvidenceBudgetFor(value, NOW).status, 'partial'); }
});

test('fresh and explicitly derived observations preserve separate clocks without refreshing the digest', () => {
  const fresh = clone(fixtures.basic), old = fresh.strategyEvidenceRetrievedAt, digest = fresh.strategyEvidencePool.digest;
  fresh.strategyEvidencePool.source.retrievedAt = NOW; fresh.strategyClaims.source.retrievedAt = NOW;
  assert.equal(strategyEvidenceBudgetFor(fresh, NOW).status, 'partial');
  fresh.strategyClaimsDerivedFromObservedAt = old;
  assert.equal(strategyEvidenceBudgetFor(fresh, NOW).status, 'verified');
  assert.equal(fresh.strategyEvidenceRetrievedAt, old); assert.equal(fresh.strategyEvidencePool.digest, digest);
  fresh.strategyClaimsDerivedFromObservedAt = NOW;
  assert.equal(strategyEvidenceBudgetFor(fresh, NOW).status, 'partial');
  assert.equal(normalizeStrategyEvidencePool(fixtures.basic.strategyEvidencePool, fixtures.basic.strategySectionCoverage,
    { sourceUrl: 'https://example.org/interim.pdf' }, NOW), null);
});

test('future/invalid observation and wrong quarter stay partial, including microsecond bound', () => {
  for (const stamp of ['2026-10-27T02:05:00.000001Z', '2026-10-26T24:00:00Z', '2026-10-26T19:00:00+00:60']) {
    const value = clone(fixtures.basic);
    value.strategyEvidencePool.source.retrievedAt = value.strategyEvidenceRetrievedAt = stamp;
    value.strategyClaims.source.retrievedAt = stamp;
    assert.equal(strategyEvidenceBudgetFor(value, NOW).status, 'partial');
  }
  const bad = clone(fixtures.basic); bad.strategySectionCoverage.periodEnd = '2026-09-30';
  assert.equal(strategyEvidenceBudgetFor(bad, NOW).status, 'partial');
});

test('legacy observations are diagnosed without deleting text, proof or refreshing time', () => {
  const value = clone(fixtures.basic); delete value.strategyEvidencePool;
  value.narrative.text = '原始旧摘录'.repeat(200); const before = clone(value);
  assert.equal(strategyEvidenceBudgetFor(value, NOW).status, 'legacy-unbudgeted');
  assert.deepEqual(value, before);
});

test('rejected projections remain partial after cleanup, including a canonical null pool', () => {
  const value = clone(fixtures.basic); value.narrative.text = '未计费正文';
  value.strategyEvidenceBudget = strategyEvidenceBudgetFor(value, NOW);
  Object.assign(value, { narrative: null, strategyExcerpts: [], strategyAnalysis: null, strategyClaims: null });
  assert.deepEqual(strategyEvidenceBudgetFor(value, NOW).reasonCodes, ['PRIOR_QUOTE_BUDGET_REJECTED']);
  value.strategyEvidencePool = null;
  assert.equal(strategyEvidenceBudgetFor(value, NOW).status, 'partial');
  assert.equal(strategyEvidenceBudgetFor(fixtures.basic, NOW).status, 'verified');
});
