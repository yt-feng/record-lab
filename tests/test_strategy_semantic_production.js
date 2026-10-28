'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const lane = require('../scripts/strategy-semantic-production');
const { normalizeReport } = require('../lib/fund-analysis');
const { retainedStrategySemanticsFor, retainStrategySemantics } = require('../lib/strategy-semantics');
const { strategyFor, analyzeQuarter } = require('../scripts/analyze-data');
const ui = require('../web/app');
const ROOT = path.resolve(__dirname, '..'), NOW = '2026-10-26T21:00:00.123456+00:00';
const clone = value => JSON.parse(JSON.stringify(value));
function python(source, ...args) {
  const result = spawnSync('python3', ['-c', source, ...args], { cwd: ROOT, encoding: 'utf8', timeout: 20000, maxBuffer: 2000000 });
  assert.equal(result.status, 0, 'synthetic Python producer failed');
  return JSON.parse(result.stdout);
}
const initialReport = python('import json,sys;sys.path[:0]=["scripts","tests"];from test_strategy_semantic_adapter import baseline;print(json.dumps(baseline(),ensure_ascii=False))');
function save(file, value) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(value)); }
function read(file) { return JSON.parse(fs.readFileSync(file, 'utf8')); }
function fixture(t, change = null) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'semantic-production-')); t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const report = { ...clone(initialReport), coverage: 'unknown', holdings: [], reportOnly: true, parseMethod: 'pypdf' };
  if (change) change(report);
  const fund = { code: '000001', name: '合成基金', company: '合成机构', companyId: '80000001',
    aliases: ['000002'], selectedReport: report, reports: [clone(report)], rows: [], managers: [],
    status: 'ok', dataStatus: 'cached', asOf: '2026-10-26', retrievedAt: initialReport.retrievedAt };
  const company = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31',
    asOf: fund.asOf, retrievedAt: fund.retrievedAt, funds: [fund] };
  const manifest = { ...company, funds: [], coverage: { fundCount: 1, companyCount: 1 },
    companyFiles: [{ companyCode: '80000001', path: './data/companies/co1.json' }] };
  const folder = path.join(root, 'web/data/periods/2026Q2');
  save(path.join(folder, 'latest.json'), manifest); save(path.join(folder, 'companies/co1.json'), company);
  return { root, folder, report, fund, company, manifest };
}
function prepare(fixture, extra = {}) {
  return lane.prepare(fixture.root, { quarter: '2026Q2', requestedCode: '000001', runId: '123', runAttempt: '1',
    now: '2026-10-26T20:00:00.123456+00:00', ...extra });
}
function produce(fixture, mode = 'valid') {
  const result = python(`import json,sys
sys.path[:0]=['scripts','tests']
from test_produce_strategy_semantics import run_synthetic
task=json.load(open(sys.argv[1]))
mode=sys.argv[2]
transport=(lambda *_: {'errorCode':'PROVIDER_TIMEOUT'}) if mode=='unknown' else None
output,receipt=run_synthetic(task,transport=transport)
print(json.dumps({'output':output,'receipt':receipt},ensure_ascii=False))`, path.join(fixture.root, lane.CACHE, 'task.json'), mode);
  save(path.join(fixture.root, lane.CACHE, 'result.json'), result.output);
  save(path.join(fixture.root, lane.CACHE, 'receipt.json'), result.receipt);
  return result;
}

test('one real adapter candidate reaches production partition, normalization, analysis and rendered evidence', t => {
  const value = fixture(t), before = clone(value.company), oldManifest = clone(value.manifest);
  assert.equal(prepare(value).hasWork, true);
  const reserved = read(path.join(value.root, lane.LEDGER));
  assert.equal(reserved.products['2026Q2:000001'].attempts[0].status, 'reserved');
  const { output } = produce(value);
  assert.equal(output.status, 'candidate-ready');
  const summary = lane.finish(value.root, { now: NOW });
  assert.equal(summary.status, 'admitted'); assert.equal(summary.selectedClaims, 1);
  const located = lane.locate(value.root, '2026Q2', '000001'), report = located.fund.selectedReport;
  assert.notEqual(located.relative, 'companies/co1.json');
  assert.equal(fs.existsSync(path.join(value.folder, 'companies/co1.json')), false);
  assert.equal(located.manifest.retrievedAt, oldManifest.retrievedAt);
  assert.equal(located.company.retrievedAt, before.retrievedAt);
  assert.equal(located.fund.retrievedAt, before.funds[0].retrievedAt);
  assert.deepEqual(located.fund.rows, before.funds[0].rows);
  assert.deepEqual(report.strategyClaims, value.report.strategyClaims);
  assert.deepEqual(report.holdings, value.report.holdings);
  assert.equal(report.strategyEvidenceRetrievedAt, value.report.strategyEvidenceRetrievedAt);
  assert.deepEqual(located.fund.reports[0], report);
  assert.ok(retainedStrategySemanticsFor(report, { code: '000001', now: NOW }));
  const normalized = normalizeReport(report, { code: '000001', now: NOW });
  assert.ok(retainedStrategySemanticsFor(normalized, { code: '000001', now: NOW }));
  assert.deepEqual(normalized.strategySemantics, report.strategySemantics);
  const strategy = strategyFor(located.fund, { target: '2026-06-30', start: '2026-04-01', generation: NOW, asOf: '2026-10-26' });
  assert.equal(strategy.semantic.claims.length, 1); assert.equal(strategy.semanticAnalysisComplete, false);
  const analysis = analyzeQuarter(located.manifest, () => located.company, { now: NOW, writePart: () => {} });
  assert.equal(analysis.coverage.strategyModelInterpretedRecords, 1);
  assert.equal(analysis.coverage.strategyModelSelectedClaimCount, 1);
  assert.equal(analysis.coverage.strategySemanticAnalysisCompleteRecords, 0);
  const rendered = ui.modelStrategyEvidence(report);
  assert.equal(rendered.claims[0].text, '本基金看好科技行业。');
  assert.equal(rendered.claims[0].attribution, '报告主体');
  assert.equal(rendered.coverage.unknown, 1);
  assert.equal(prepare(value, { runId: '124' }).hasWork, false);
});

test('quarterly strategy on interim holdings preserves both independent report sources', t => {
  const value = fixture(t, report => { report.kind = 'semiannual'; report.sourceUrl = 'https://example.org/interim.pdf'; });
  prepare(value); produce(value);
  assert.equal(lane.finish(value.root, { now: NOW }).status, 'admitted');
  const report = lane.locate(value.root, '2026Q2', '000001').fund.selectedReport;
  assert.equal(report.kind, 'semiannual'); assert.equal(report.sourceUrl, 'https://example.org/interim.pdf');
  assert.equal(report.strategySemantics.candidate.sidecar.source.kind, 'quarterly');
  assert.equal(report.strategySemantics.candidate.sidecar.source.sourceUrl, initialReport.sourceUrl);
});

test('a persisted reservation and unknown result block reruns without making another request', t => {
  const value = fixture(t); prepare(value);
  assert.throws(() => prepare(value, { runId: '124', maxAttempts: 2 }), { code: 'SEMANTIC_PRIOR_ATTEMPT_UNRESOLVED' });
  const original = fs.readFileSync(path.join(value.folder, 'companies/co1.json'));
  produce(value, 'unknown');
  assert.equal(lane.finish(value.root, { now: NOW }).status, 'unknown');
  assert.deepEqual(fs.readFileSync(path.join(value.folder, 'companies/co1.json')), original);
  const entry = read(path.join(value.root, lane.LEDGER)).products['2026Q2:000001'];
  assert.equal(entry.usageHistory.length, 1); assert.equal(entry.usageHistory[0].outcome, 'unknown');
  assert.throws(() => prepare(value, { runId: '124', maxAttempts: 2 }), { code: 'SEMANTIC_PRIOR_ATTEMPT_UNRESOLVED' });
});

test('a changed production report is never overwritten while the real model attempt remains accounted', t => {
  const value = fixture(t); prepare(value); produce(value);
  const company = read(path.join(value.folder, 'companies/co1.json'));
  company.funds[0].selectedReport.navYuan = 42; save(path.join(value.folder, 'companies/co1.json'), company);
  const summary = lane.finish(value.root, { now: NOW });
  assert.equal(summary.status, 'rejected'); assert.equal(summary.errorCode, 'SEMANTIC_CONCURRENT_REPORT_CHANGED');
  assert.equal(lane.locate(value.root, '2026Q2', '000001').fund.selectedReport.navYuan, 42);
  assert.equal(read(path.join(value.root, lane.LEDGER)).products['2026Q2:000001'].usageHistory[0].outcome, 'validated');
});

test('independent producer receipt rejects modified candidate before any corpus write', t => {
  const value = fixture(t); prepare(value); const result = produce(value);
  result.output.adapter.candidate.claims[0].polarity = 'negative';
  save(path.join(value.root, lane.CACHE, 'result.json'), result.output);
  assert.throws(() => lane.finish(value.root, { now: NOW }), { code: 'SEMANTIC_PRODUCER_RECEIPT_INVALID' });
  assert.equal(lane.locate(value.root, '2026Q2', '000001').fund.selectedReport.strategySemantics, undefined);
});

test('ordinary merge retention reuses admitted sidecar without touching finite claims or source clocks', t => {
  const value = fixture(t); prepare(value); produce(value); lane.finish(value.root, { now: NOW });
  const source = lane.locate(value.root, '2026Q2', '000001').fund.selectedReport, target = clone(value.report);
  const finiteBefore = clone(target.strategyClaims), observed = target.strategyEvidenceRetrievedAt;
  assert.equal(retainStrategySemantics(target, source, { code: '000001', now: NOW }), true);
  assert.deepEqual(target.strategyClaims, finiteBefore); assert.equal(target.strategyEvidenceRetrievedAt, observed);
  assert.deepEqual(target.strategySemantics, source.strategySemantics);
  assert.equal(retainStrategySemantics(target, source, { code: '000001', now: NOW }), false);
  assert.ok(retainedStrategySemanticsFor(source, { code: '000002', now: NOW }));
  assert.equal(retainedStrategySemanticsFor(source, { code: '000003', now: NOW }), null);
  const refreshed = clone(value.report), later = '2026-10-26T20:45:00Z';
  refreshed.strategyEvidenceRetrievedAt = later;
  refreshed.strategyEvidencePool.source.retrievedAt = later;
  refreshed.strategyClaims.source.retrievedAt = later;
  refreshed.strategyClaimsIdentityEvidence.retrievedAt = later;
  refreshed.navYuan = 42;
  assert.equal(retainStrategySemantics(refreshed, source, { code: '000001', now: NOW }), true);
  assert.equal(refreshed.navYuan, 42);
  assert.equal(refreshed.strategyEvidenceRetrievedAt, source.strategyEvidenceRetrievedAt);
  assert.deepEqual(refreshed.strategyClaims, source.strategyClaims);
  assert.ok(retainedStrategySemanticsFor(refreshed, { code: '000001', now: NOW }));
});

test('blank input deterministically selects one eligible independent portfolio and skips any prior ledger entry', t => {
  const value = fixture(t), file = path.join(value.folder, 'companies/co1.json');
  const company = read(file);
  company.funds.unshift({ ...clone(company.funds[0]), code: '000003', selectedReport: {} });
  save(file, company);
  const selected = prepare(value, { requestedCode: '' });
  assert.equal(selected.code, '000001');
  assert.equal(lane.taskFor(value.root).fundIndex, 1);
  assert.throws(() => prepare(value, { requestedCode: '', runId: '124' }), { code: 'SEMANTIC_NO_ELIGIBLE_PRODUCT' });
});

test('automatic selection skips a source whose original parser or incomplete pages cannot be reproduced', t => {
  for (const alter of [report => { report.parseMethod = 'mineru'; }, report => { report.pageStats.truncated = true; },
    report => { report.pageStats.processedPages = 1; }]) {
    const value = fixture(t, alter);
    assert.throws(() => prepare(value, { requestedCode: '' }), { code: 'SEMANTIC_NO_ELIGIBLE_PRODUCT' });
    assert.equal(fs.existsSync(path.join(value.root, lane.LEDGER)), false);
  }
});

test('a staging I/O failure is fatal and cannot finalize the ledger or proceed to publication', t => {
  const value = fixture(t); prepare(value); produce(value);
  const original = fs.renameSync;
  fs.renameSync = (from, to) => {
    if (to === path.join(value.folder, 'latest.json')) { const error = new Error('synthetic disk full'); error.code = 'ENOSPC'; throw error; }
    return original(from, to);
  };
  try { assert.throws(() => lane.finish(value.root, { now: NOW }), { code: 'ENOSPC' }); }
  finally { fs.renameSync = original; }
  const ledger = read(path.join(value.root, lane.LEDGER));
  assert.equal(ledger.products['2026Q2:000001'].attempts[0].status, 'reserved');
  assert.equal(fs.existsSync(path.join(value.root, lane.CACHE, 'summary.json')), false);
  assert.equal(read(path.join(value.folder, 'latest.json')).companyFiles[0].path, './data/companies/co1.json');
});

test('known diagnostic portfolio cannot start a fresh zero-based production history', t => {
  const value = fixture(t), file = path.join(value.folder, 'companies/co1.json');
  const company = JSON.parse(JSON.stringify(read(file)).replaceAll('000001', '168401'));
  company.funds[0].aliases = [];
  save(file, company);
  assert.throws(() => prepare(value, { requestedCode: '' }), { code: 'SEMANTIC_NO_ELIGIBLE_PRODUCT' });
  assert.throws(() => prepare(value, { requestedCode: '168401' }), { code: 'SEMANTIC_PRIOR_HISTORY_IMPORT_REQUIRED' });
  assert.equal(fs.existsSync(path.join(value.root, lane.LEDGER)), false);
});
