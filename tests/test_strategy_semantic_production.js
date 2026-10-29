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
function ledger(value, key = '2026Q2:000001') { return new lane.Ledger(value.root).get(key); }
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
  assert.equal(ledger(value).attempts[0].status, 'reserved');
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
  const entry = ledger(value);
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
  assert.equal(ledger(value).usageHistory[0].outcome, 'validated');
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
  assert.equal(ledger(value).attempts[0].status, 'reserved');
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

function batchFixture(t) {
  const value = fixture(t), company = clone(value.company);
  const report = python('import json,sys;sys.path[:0]=["scripts","tests"];from test_produce_strategy_semantics import baseline_for;print(json.dumps(baseline_for("000003"),ensure_ascii=False))');
  report.parseMethod = 'pypdf';
  company.funds.push({ ...clone(value.fund), code: '000003', aliases: ['000004'], selectedReport: report, reports: [clone(report)] });
  // A reported share class is not a third independent model request.
  company.funds.push({ ...clone(value.fund), code: '000002', aliases: [], selectedReport: clone(value.report) });
  save(path.join(value.folder, 'companies/co1.json'), company);
  return value;
}
function batchPrepare(value, extra = {}) {
  return lane.prepareBatch(value.root, { quarter: '2026Q2', runId: '123', runAttempt: '1',
    batchSize: 10, maxAttempts: 1, now: '2026-10-26T20:00:00.123456+00:00', ...extra });
}
function batchProduce(value, modes = {}) {
  for (const item of lane.batchFor(value.root).tasks) {
    if (modes[item.code] === 'missing') continue;
    const result = python(`import json,sys
sys.path[:0]=['scripts','tests']
from test_produce_strategy_semantics import run_synthetic
from produce_strategy_semantics import receipt_for
import copy
task=json.load(open(sys.argv[1]))
transport=(lambda *_: {'errorCode':'PROVIDER_TIMEOUT'}) if sys.argv[2]=='unknown' else None
checkpoints=[]
output,receipt=run_synthetic(task,transport=transport,checkpoint=lambda row: checkpoints.append(copy.deepcopy(row)))
if sys.argv[2]=='started':
    output=next(row for row in checkpoints if (row.get('adapter') or {}).get('modelStatus')=='started')
    receipt=receipt_for(task,output)
print(json.dumps({'output':output,'receipt':receipt},ensure_ascii=False))`,
      path.join(value.root, lane.CACHE, item.path, 'task.json'), modes[item.code] || 'valid');
    assert.equal(result.output.adapter.modelRequests, 1);
    save(path.join(value.root, lane.CACHE, item.path, 'result.json'), result.output);
    save(path.join(value.root, lane.CACHE, item.path, 'receipt.json'), result.receipt);
  }
}

test('automatic Q2 batch uses the quarterly strategy source despite unrelated interim parser metadata', t => {
  for (const keepQuarterReport of [true, false]) {
    const value = fixture(t, report => {
      report.kind = 'semiannual'; report.sourceUrl = 'https://example.org/interim.pdf';
      report.reportPdfSourceUrl = report.sourceUrl; report.parseMethod = 'mineru';
      report.pageStats = { totalPages: 500, processedPages: 200, truncated: true, pageNumbersVerified: false };
    });
    const quarter = { ...clone(initialReport), parseMethod: 'pypdf' };
    if (keepQuarterReport) value.company.funds[0].reports.push(quarter);
    save(path.join(value.folder, 'companies/co1.json'), value.company);
    const selected = batchPrepare(value);
    assert.equal(selected.selectedProducts, 1);
    const task = read(path.join(value.root, lane.CACHE, lane.batchFor(value.root).tasks[0].path, 'task.json'));
    assert.equal(task.report.sourceUrl, 'https://example.org/interim.pdf');
    assert.equal(task.report.strategyEvidencePool.source.sourceUrl, quarter.sourceUrl);
    batchProduce(value);
    const summary = lane.finishBatch(value.root, { now: NOW });
    assert.equal(summary.admittedProducts, 1);
    const report = lane.locate(value.root, '2026Q2', '000001').fund.selectedReport;
    assert.equal(report.sourceUrl, 'https://example.org/interim.pdf');
    assert.equal(report.parseMethod, 'mineru'); assert.equal(report.pageStats.totalPages, 500);
    assert.equal(report.strategySemantics.candidate.sidecar.source.sourceUrl, quarter.sourceUrl);
    assert.equal(ledger(value).usageHistory.length, 1);
  }
});

test('mixed-source fallback verifies its PDF hash before any model call and explicit code can reparse known metadata', t => {
  for (const changedSource of ['pdf', 'text']) {
  const value = fixture(t, report => {
    report.kind = 'semiannual'; report.sourceUrl = 'https://example.org/interim.pdf';
    report.reportPdfSourceUrl = report.sourceUrl;
  });
  value.company.funds[0].reports.push({ ...clone(initialReport), parseMethod: 'mineru' });
  save(path.join(value.folder, 'companies/co1.json'), value.company);
  assert.equal(batchPrepare(value).selectedProducts, 0);
  assert.equal(batchPrepare(value, { runId: '124', requestedCode: '000001', batchSize: 1 }).selectedProducts, 1);
  const item = lane.batchFor(value.root).tasks[0];
  const checked = python(`import json,sys
sys.path[:0]=['scripts','tests']
from test_produce_strategy_semantics import run_synthetic, pages_for
from unittest.mock import patch
task=json.load(open(sys.argv[1]))
calls=[]
if sys.argv[2]=='pdf':
 output,receipt=run_synthetic(task,fetch=lambda url: b'%PDF-changed-source',transport=lambda *args: calls.append(args))
else:
 with patch('test_produce_strategy_semantics.pages_for',return_value=[page.replace('科技','消费') for page in pages_for(task['code'])]):
  output,receipt=run_synthetic(task,transport=lambda *args: calls.append(args))
print(json.dumps({'output':output,'receipt':receipt,'modelCalls':len(calls)},ensure_ascii=False))`,
    path.join(value.root, lane.CACHE, item.path, 'task.json'), changedSource);
  assert.equal(checked.modelCalls, 0);
  assert.equal(checked.output.adapter.modelRequests, 0);
  assert.equal(checked.output.adapter.errorCode, changedSource === 'pdf' ? 'PDF_IDENTITY_CHANGED' : 'SEMANTIC_PACKET_NOT_READY');
  save(path.join(value.root, lane.CACHE, item.path, 'result.json'), checked.output);
  save(path.join(value.root, lane.CACHE, item.path, 'receipt.json'), checked.receipt);
  assert.equal(lane.finishBatch(value.root, { now: NOW }).admittedProducts, 0);
  assert.equal(ledger(value).usageHistory.length, 0);
  }
});

test('two genuine mapper candidates in one company page admit together with independent usage and one page write', t => {
  const value = batchFixture(t), planned = batchPrepare(value);
  assert.equal(planned.selectedProducts, 2); assert.equal(planned.counts.aliasRecords, 1);
  const tasks = lane.batchFor(value.root).tasks;
  assert.equal(new Set(tasks.map(item => read(path.join(value.root, lane.CACHE, item.path, 'task.json')).companyPath)).size, 1);
  batchProduce(value);
  let pageWrites = 0; const rename = fs.renameSync;
  fs.renameSync = (from, to) => { if (to.includes('/companies/')) pageWrites++; return rename(from, to); };
  let result; try { result = lane.finishBatch(value.root, { now: NOW }); } finally { fs.renameSync = rename; }
  assert.equal(result.admittedProducts, 2); assert.equal(result.knownModelRequests, 2); assert.equal(pageWrites, 1);
  assert.equal(result.continueRun, false);
  for (const code of ['000001', '000003']) {
    const found = lane.locate(value.root, '2026Q2', code).fund;
    assert.equal(found.selectedReport.strategySemantics.candidate.code, code);
    const history = ledger(value, '2026Q2:' + code).usageHistory;
    assert.equal(history.length, 1); assert.equal(history[0].usage.total_tokens, 300);
    assert.equal(history[0].attemptedAt, '2026-10-26T20:01:00.123456+00:00');
  }
  assert.equal(lane.readbackBatch(value.root).publication, 'persisted');
  assert.equal(batchPrepare(value, { runId: '124' }).hasWork, false);
});

test('an unknown or missing product receipt never resets its usage or blocks independent admission', t => {
  for (const mode of ['unknown', 'missing', 'started']) {
    const value = batchFixture(t); batchPrepare(value); batchProduce(value, { '000003': mode });
    const result = lane.finishBatch(value.root, { now: NOW });
    assert.equal(result.admittedProducts, 1); assert.equal(result.unknownProducts, 1);
    const entry = ledger(value, '2026Q2:000003');
    assert.equal(entry.attempts[0].status, 'unknown');
    if (mode !== 'missing') {
      assert.equal(entry.usageHistory[0].outcome, mode); assert.equal(entry.attempts[0].modelRequests, 1);
    } else {
      assert.deepEqual(entry.usageHistory, []); assert.equal(entry.attempts[0].modelRequests, null);
      assert.equal(result.unknownRequestAccountingProducts, 1);
    }
    assert.equal(batchPrepare(value, { runId: '124', maxAttempts: 2 }).hasWork, false);
  }
});

test('legacy ledger bytes and previous failed usage survive partitioning and a new admitted attempt', t => {
  const value = batchFixture(t);
  const old = { usageHistory: [{ requestDigest: 'a'.repeat(64), inputDigest: 'b'.repeat(64),
    attemptedAt: '2026-10-26T19:10:00Z', finishedAt: '2026-10-26T19:10:01Z', outcome: 'failed', responseDigest: null, usage: null }],
    attempts: [{ reservationId: '100:1', status: 'rejected', plannedAt: '2026-10-26T19:09:00Z', pdfRequests: 1, modelRequests: 1 }] };
  old.usageHistoryDigest = lane.digest(old.usageHistory);
  save(path.join(value.root, lane.LEDGER), { schemaVersion: 1, kind: 'strategy-semantic-ledger', products: { '2026Q2:000001': old } });
  const original = fs.readFileSync(path.join(value.root, lane.LEDGER));
  batchPrepare(value, { maxAttempts: 2 }); batchProduce(value);
  assert.equal(lane.finishBatch(value.root, { now: NOW }).admittedProducts, 2);
  assert.deepEqual(fs.readFileSync(path.join(value.root, lane.LEDGER)), original);
  const entry = ledger(value);
  assert.deepEqual(entry.usageHistory[0], old.usageHistory[0]); assert.deepEqual(entry.attempts[0], old.attempts[0]);
  assert.equal(entry.usageHistory.length, 2);
  const file = path.join(value.root, lane.ledgerFileFor('2026Q2:000001')), shard = read(file);
  shard.products['2026Q2:000001'].usageHistory.shift(); shard.products['2026Q2:000001'].usageHistoryDigest = lane.digest(shard.products['2026Q2:000001'].usageHistory);
  save(file, shard);
  assert.throws(() => ledger(value), { code: 'SEMANTIC_LEDGER_INVALID' });
});

test('one concurrent report change keeps its usage while another same-page candidate admits', t => {
  const value = batchFixture(t); batchPrepare(value); batchProduce(value);
  const file = path.join(value.folder, 'companies/co1.json'), company = read(file);
  company.funds[1].selectedReport.navYuan = 99; save(file, company);
  const result = lane.finishBatch(value.root, { now: NOW });
  assert.equal(result.admittedProducts, 1);
  assert.equal(result.products.find(row => row.code === '000003').errorCode, 'SEMANTIC_CONCURRENT_REPORT_CHANGED');
  assert.equal(lane.locate(value.root, '2026Q2', '000003').fund.selectedReport.navYuan, 99);
  assert.equal(ledger(value, '2026Q2:000003').usageHistory[0].usage.total_tokens, 300);
});

test('batch I/O failure stops before any ledger finalization and workflow contains no self dispatch', t => {
  const value = batchFixture(t); batchPrepare(value); batchProduce(value);
  const rename = fs.renameSync;
  fs.renameSync = (from, to) => { if (to.endsWith('/latest.json')) { const error = new Error('synthetic'); error.code = 'ENOSPC'; throw error; } return rename(from, to); };
  try { assert.throws(() => lane.finishBatch(value.root, { now: NOW }), { code: 'ENOSPC' }); } finally { fs.renameSync = rename; }
  assert.equal(ledger(value).attempts[0].status, 'reserved');
  assert.equal(ledger(value, '2026Q2:000003').attempts[0].status, 'reserved');
  const workflow = fs.readFileSync(path.join(ROOT, '.github/workflows/enrich-strategy-semantics.yml'), 'utf8');
  assert.equal(workflow.includes('gh workflow run'), false);
  assert.match(workflow, /group: published-records/); assert.match(workflow, /maxWorkers|two workers/);
});

test('a ten-product cap leaves the next independent product ready without resetting an existing reservation', t => {
  const value = fixture(t), file = path.join(value.folder, 'companies/co1.json'), company = read(file);
  const reports = python('import json,sys;sys.path[:0]=["scripts","tests"];from test_produce_strategy_semantics import baseline_for;print(json.dumps([baseline_for(f"{code:06d}") for code in range(3,22,2)],ensure_ascii=False))');
  for (const report of reports) {
    report.parseMethod = 'pypdf';
    company.funds.push({ ...clone(value.fund), code: report.fundCode, aliases: [], selectedReport: report, reports: [clone(report)] });
  }
  save(file, company);
  const first = batchPrepare(value);
  assert.equal(first.selectedProducts, 10); assert.equal(first.counts.eligibleProducts, 11);
  const second = batchPrepare(value, { runId: '124' });
  assert.equal(second.selectedProducts, 1); assert.equal(second.counts.priorHistoryBlockedProducts, 10);
  assert.equal(lane.batchFor(value.root).tasks[0].code, '000021');
  assert.equal(ledger(value).attempts.length, 1); assert.equal(ledger(value).attempts[0].reservationId, '123:1:000001');
});

test('a real deadline deferral spends no request and is selectable next batch with its original record retained', t => {
  const value = batchFixture(t); batchPrepare(value);
  const result = python(`import json,sys
sys.path[:0]=['scripts','tests']
from pathlib import Path
from unittest.mock import patch
import produce_strategy_semantics as p
from test_strategy_semantic_adapter import NOW
root=Path(sys.argv[1]); batch=p.read_public(root/p.CACHE/'batch.json'); clocks=iter([0,2000,2000])
with patch.object(p,'stamp',return_value=NOW):
    output=p.execute_batch(batch,root=root,credential='synthetic',clock=lambda:next(clocks),executor=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('no request')))
print(json.dumps(output))`, value.root);
  assert.ok(result.products.every(row => row.status === 'deferred'));
  const finished = lane.finishBatch(value.root, { now: NOW });
  assert.ok(finished.products.every(row => row.status === 'deferred' && row.modelRequests === 0 && row.pdfRequests === 0));
  const old = clone(ledger(value).attempts[0]);
  assert.equal(batchPrepare(value, { runId: '124' }).selectedProducts, 2);
  assert.deepEqual(ledger(value).attempts[0], old);
  assert.equal(ledger(value).attempts[1].status, 'reserved'); assert.deepEqual(ledger(value).usageHistory, []);
});

test('known zero-model PDF failures get one source retry; parser failures require changed evidence or explicit selection', t => {
  for (const mode of ['fetch', 'parser']) {
    const value = fixture(t); batchPrepare(value);
    const store = new lane.Ledger(value.root), entry = clone(store.get('2026Q2:000001'));
    Object.assign(entry.attempts[0], { status: 'rejected', pdfRequests: 1, modelRequests: 0,
      errorCode: mode === 'fetch' ? 'SEMANTIC_PDF_HTTP_REJECTED' : 'SEMANTIC_PACKET_NOT_READY' });
    store.set('2026Q2:000001', entry); store.save();
    if (mode === 'fetch') {
      assert.equal(batchPrepare(value, { runId: '124' }).selectedProducts, 1);
      const later = new lane.Ledger(value.root), current = clone(later.get('2026Q2:000001'));
      Object.assign(current.attempts.at(-1), { status: 'rejected', pdfRequests: 1, modelRequests: 0,
        errorCode: 'SEMANTIC_PDF_HTTP_REJECTED' });
      later.set('2026Q2:000001', current); later.save();
      assert.equal(batchPrepare(value, { runId: '125' }).hasWork, false);
      assert.equal(batchPrepare(value, { runId: '126', requestedCode: '000001', batchSize: 1 }).selectedProducts, 1);
    } else {
      assert.equal(batchPrepare(value, { runId: '124' }).hasWork, false);
      const file = path.join(value.folder, 'companies/co1.json'), company = read(file);
      company.funds[0].selectedReport.navYuan = 42; save(file, company);
      assert.equal(batchPrepare(value, { runId: '125' }).selectedProducts, 1);
    }
    assert.deepEqual(ledger(value).attempts[0], entry.attempts[0]);
    assert.deepEqual(ledger(value).usageHistory, []);
  }
});
