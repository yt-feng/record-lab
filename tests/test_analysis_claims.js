'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { strategyFor, analyzeQuarter } = require('../scripts/analyze-data');
const { strategyClaimsFor } = require('../lib/strategy-claims');
const ui = require('../web/analysis');
const now = '2026-10-27T02:00:00Z';
const phase = { start: '2026-04-01', target: '2026-06-30', generation: now };
const processResult = spawnSync('python3', ['-c', 'import json,sys;sys.path[:0]=["tests","scripts"];from test_strategy_evidence_pool import extracted,REPORT;print(json.dumps({**REPORT,**extracted()},ensure_ascii=False))'],
  { cwd: path.join(__dirname, '..'), encoding: 'utf8', maxBuffer: 100000 });
assert.equal(processResult.status, 0, processResult.stderr);
const selectedReport = JSON.parse(processResult.stdout);
const fund = () => ({ code: '000001', name: '合成基金', company: '合成机构', selectedReport: structuredClone(selectedReport), retrievedAt: selectedReport.retrievedAt,
  holdings: [], managers: [], holdingStatus: 'report-only' });

test('a pooled fallback quote remains literal across astral characters and ignores unpooled fund narrative', () => {
  const result = spawnSync('python3', ['-c', 'import json,sys;sys.path[:0]=["tests","scripts"];from test_strategy_evidence_pool import extracted,REPORT;print(json.dumps({**REPORT,**extracted(body="本基金增配科技行业"+"😀"*150+"。")},ensure_ascii=False))'],
    { cwd: path.join(__dirname, '..'), encoding: 'utf8', maxBuffer: 100000 });
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout); report.strategyAnalysis = null;
  const quote = report.strategyEvidencePool.quotes[0]; assert.equal(Array.from(quote.text).length, 160);
  const strategy = strategyFor({ ...fund(), selectedReport: report, narrative: { ...report.narrative, text: '额外旧文' } }, phase);
  assert.equal(strategy.excerpts.length, 1);
  assert.equal(strategy.excerpts[0].text, quote.text);
  assert.equal(strategy.excerpts[0].evidencePoolId, quote.id);
  assert.doesNotMatch(strategy.excerpts[0].text, /[\ud800-\udfff]/u);
});

test('aggregate counts only admitted finite scans and keeps every semantic claim pending', () => {
  const value = fund(), before = structuredClone(value);
  assert.ok(strategyClaimsFor(value.selectedReport, { code: value.code, now }));
  const strategy = strategyFor(value, phase);
  assert.equal(strategy.ruleScanComplete, true);
  assert.equal(strategy.ruleScan.source.retrievedAt, selectedReport.retrievedAt);
  assert.equal(strategy.ruleScan.source.sourceUrl, selectedReport.sourceUrl);
  assert.equal(strategy.ruleScan.coverage.classifiedClauseCount, selectedReport.strategyClaims.coverage.classifiedClauseCount);
  assert.equal(strategy.semanticAnalysisComplete, false);
  assert.equal(strategy.ruleScan.explainsIndividualStockTrades, false);
  assert.deepEqual(value, before);
  assert.equal(strategyFor(value, { ...phase, asOf: '2026-07-20' }).ruleScanComplete, false);
  for (const mutate of [v => { delete v.selectedReport.strategyEvidencePool; },
    v => { v.selectedReport.strategyClaims.source.digest = '0'.repeat(64); },
    v => { v.code = '000003'; }, v => { v.selectedReport.strategyClaims.semanticAnalysisComplete = true; }]) {
    const invalid = fund(); mutate(invalid);
    const result = strategyFor(invalid, phase);
    assert.equal(result.ruleScanComplete, false); assert.equal(result.ruleScan, null);
    assert.equal(result.sectionStatus, 'complete');
    assert.equal(result.semanticAnalysisComplete, false);
  }
});

test('analysis v8 separates section, rule and semantic record counts including legacy sections', () => {
  const good = fund(), legacy = fund(); legacy.code = '000003'; legacy.selectedReport.fundCode = '000003'; delete legacy.selectedReport.strategyClaims;
  const file = './data/companies/co1.json';
  const manifest = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: phase.target, baselinePeriodEnd: '2026-03-31',
    asOf: '2026-10-27', retrievedAt: now, coverage: { fundCount: 2, catalogFundCount: 2 }, companyFiles: [{ path: file }] };
  const document = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: phase.target, baselinePeriodEnd: '2026-03-31', funds: [good, legacy] };
  const output = analyzeQuarter(manifest, () => document, { now, writePart: () => {}, progress: { phase: { targetPeriodEnd: phase.target, baselinePeriodEnd: '2026-03-31' }, completionStatus: 'complete', research: { completeFunds: 2, applicableFunds: 2 } } });
  assert.equal(output.analysisVersion, 8);
  assert.equal(output.coverage.analyzedRecords, 2);
  assert.equal(output.coverage.strategySectionCompleteRecords, 2);
  assert.equal(output.coverage.strategyRuleScanCompleteRecords, 1);
  assert.equal(output.coverage.strategyRuleScanPendingRecords, 1);
  assert.equal(output.coverage.strategySemanticAnalysisCompleteRecords, 0);
  assert.equal(output.coverage.strategySemanticAnalysisPendingRecords, 2);
  assert.equal(output.coverage.strategySemanticAnalysisComplete, false);
  assert.equal(output.coverage.complete, false);
  assert.equal(output.coverage.collectionEvidenceComplete, false);
  assert.equal(ui.validate(output, '2026Q2'), output);
  for (const change of [c => { c.strategyRuleScanCompleteRecords = 2; }, c => { c.strategySemanticAnalysisComplete = true; },
    c => { c.strategySemanticAnalysisPendingRecords = 0; }, c => { c.complete = true; }, c => { c.collectionEvidenceComplete = 'true'; }]) {
    const invalid = structuredClone(output); change(invalid.coverage);
    assert.throws(() => ui.validate(invalid, '2026Q2'), /INVALID_ANALYSIS/);
  }
});
