'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { strategySectionCoverageFor, strategyAnalysisFor } = require('../lib/strategy-sections');
const samples = require('./fixtures/strategy-sections.json');
const copy = value => JSON.parse(JSON.stringify(value));
const { spawnSync } = require('node:child_process');
const path = require('node:path');

test('Python section proof and selected evidence retain their separate completeness contracts', () => {
  for (const name of ['complete', 'child', 'selected-evidence', 'next-major-section']) {
    const sample = samples[name], proof = strategySectionCoverageFor(sample.strategySectionCoverage, '2026-06-30');
    assert.equal(proof.complete, true, name);
    const analysis = strategyAnalysisFor(sample.strategyAnalysis, proof);
    assert.equal(analysis.status, 'available');
    assert.equal(analysis.sourceSectionComplete, true);
    assert.equal(analysis.semanticAnalysisComplete, false);
    assert.equal(analysis.analysisCoverage, 'selected-evidence');
    assert.equal(analysis.explainsIndividualStockTrades, false);
  }
  assert.ok(strategyAnalysisFor(samples['selected-evidence'].strategyAnalysis, samples['selected-evidence'].strategySectionCoverage).omittedSentenceCount > 0);
});

test('Python opening-period anchoring stays equivalent in JS with disclosure history and wrong covers', () => {
  const report = { sourceUrl: 'https://example.org/interim.pdf', kind: 'semiannual', periodEnd: '2026-06-30', publishedAt: '2026-08-28' };
  const section = '4.4 报告期内基金的投资策略和运作分析\n本基金增加债券配置，因为市场利率下行。\n4.5 报告期内基金的业绩表现';
  const history = '14.5 信息披露\n合成基金2025年第4季度报告\n合成基金2025年年度报告\n合成基金2026年第1季度报告';
  const code = "import sys,json;sys.path.insert(0,'scripts');from strategy_sections import parse_strategy_sections;x=json.load(sys.stdin);print(json.dumps(parse_strategy_sections(x['pages'],x['report']),ensure_ascii=False))";
  for (const [cover, complete] of [['合成基金2026年中期报告', true], ['合成基金2025年年度报告', false]]) {
    const proc = spawnSync('python3', ['-c', code], { cwd: path.resolve(__dirname, '..'), input: JSON.stringify({ pages: [cover, section, history], report }), encoding: 'utf8' });
    assert.equal(proc.status, 0);
    const sample = JSON.parse(proc.stdout), proof = strategySectionCoverageFor(sample.strategySectionCoverage);
    assert.equal(proof.complete, complete);
    assert.deepEqual(proof.documentPeriodEvidence, sample.strategySectionCoverage.documentPeriodEvidence);
    assert.equal(strategyAnalysisFor(sample.strategyAnalysis, proof).status, complete ? 'available' : 'wrong-period');
  }
});

test('unclosed capped unknown-page and partial mappings cannot be promoted by a complete flag', () => {
  for (const name of ['unknown-page', 'noncontiguous', 'missing-end', 'page-cap', 'text-cap', 'wrong-kind', 'missing-title']) {
    const sample = copy(samples[name]); sample.strategySectionCoverage.complete = true;
    const proof = strategySectionCoverageFor(sample.strategySectionCoverage);
    assert.equal(proof.complete, false, name);
    const analysis = strategyAnalysisFor(sample.strategyAnalysis, proof);
    assert.ok(analysis, name);
    assert.equal(analysis.sourceSectionComplete, false, name);
    assert.equal(analysis.semanticAnalysisComplete, false, name);
    assert.equal(analysis.status, name === 'wrong-kind' ? 'wrong-period' : 'partial-source', name);
  }
});

test('section proof rejects altered identity dates hashes page boundaries and counts', () => {
  for (const patch of [{ sourceUrl: 'https://127.0.0.1/a.pdf' }, { periodStart: '2026-01-01' }, { periodEnd: '2026-09-30' },
    { sourceTextHash: 'short' }, { documentPeriodEvidence: [] }, { publishedAt: null }, { contentChars: 1 }]) {
    const proof = strategySectionCoverageFor({ ...copy(samples.complete.strategySectionCoverage), ...patch, complete: true }, '2026-06-30');
    assert.equal(proof?.complete || false, false);
  }
  for (const patch of [{ endSection: '4.3' }, { pages: [7, 9] }, { endHeading: '4.3 运作情况' }, { textLimitReached: true }]) {
    const value = copy(samples.complete.strategySectionCoverage); Object.assign(value.sections[0], patch);
    assert.equal(strategySectionCoverageFor(value).complete, false);
  }
});

test('evidence binding refuses copied sources periods outside pages and excessive quotes', () => {
  const sample = samples.complete;
  for (const patch of [{ sourceUrl: 'https://example.org/other.pdf' }, { periodEnd: '2026-09-30' },
    { pages: [99], page: 99 }, { text: '长'.repeat(161) }, { sentenceIndex: 999 }]) {
    const raw = copy(sample.strategyAnalysis); Object.assign(raw.evidence[0], patch);
    assert.equal(strategyAnalysisFor(raw, sample.strategySectionCoverage), null);
  }
  const forged = copy(sample.strategyAnalysis); forged.semanticAnalysisComplete = true; forged.explainsIndividualStockTrades = true;
  const result = strategyAnalysisFor(forged, sample.strategySectionCoverage);
  assert.equal(result.semanticAnalysisComplete, false); assert.equal(result.explainsIndividualStockTrades, false);
});

test('security references require the actual full name or code in the selected quote and never prove trades', () => {
  const sample = copy(samples.complete), operation = sample.strategyAnalysis.evidence.find(item => item.securityMentions.length);
  operation.securityMentions[0].provesTrade = true;
  operation.securityMentions.push({ market: 'CN', stockCode: '600001', stockName: '平安', matchType: 'exact-name', provesTrade: true });
  const analysis = strategyAnalysisFor(sample.strategyAnalysis, sample.strategySectionCoverage);
  const mentions = analysis.evidence.flatMap(item => item.securityMentions);
  assert.deepEqual(mentions.map(item => item.stockCode), ['601318']);
  assert.equal(mentions[0].provesTrade, false); assert.equal(mentions[0].provesTradeReason, false);
});
