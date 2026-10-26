'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const patches = require('../lib/strategy-claim-patches');
const { strategyClaimsFor } = require('../lib/strategy-claims');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots, mergeFiles } = require('../scripts/merge-parts');
const NOW = '2026-10-27T03:00:00Z', OLD = '2026-10-26T19:00:00.123456+00:00';
const PLANNED = '2026-10-26T19:10:00.123456Z', ATTEMPTED = '2026-10-26T19:20:00.123456Z';
const FETCHED = '2026-10-26T20:00:00.123456Z', RECEIVED = '2026-10-26T20:30:00.123456Z';
const clone = value => JSON.parse(JSON.stringify(value));
const repo = path.join(__dirname, '..');
function python(script, input) {
  return JSON.parse(execFileSync('python3', ['-c', script], { cwd: repo, env: { ...process.env, PYTHONPATH: 'tests:scripts' },
    input: JSON.stringify(input), encoding: 'utf8', maxBuffer: 1024 * 1024 }));
}
const sources = python('import json,sys;from test_strategy_evidence_pool import extracted,REPORT;v=json.loads(sys.stdin.read());print(json.dumps([{**REPORT,**extracted(report={**REPORT,"retrievedAt":at})} for at in v],ensure_ascii=False))', [OLD, FETCHED]);
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001' }] };
const catalog = { fundCount: 2, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 2 }],
  funds: ['000001', '000002'].map(code => ({ code, companyCode: 'co1', companyName: '合成公司' })) };
function oldSnapshot({ interim = false, source = sources[0] } = {}) {
  const report = { ...clone(source), fundCode: '000001', navYuan: 1000, coverage: 'top10',
    holdings: [{ stockCode: '600001', stockName: '合成证券', market: 'CN', currency: 'CNY', shares: 100, weightPct: 10, marketValueYuan: 100 }],
    evidenceRetrievedAt: OLD, managerEvidenceRetrievedAt: OLD, navRetrievedAt: OLD };
  for (const key of ['strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget']) delete report[key];
  const reports = [report];
  if (interim) reports.unshift({ ...report, sourceUrl: 'https://example.org/interim.pdf', kind: 'semiannual', publishedAt: '2026-08-28',
    coverage: 'full', portfolioIdentityEvidence: null, strategySectionCoverage: undefined, strategyAnalysis: undefined,
    narrative: null, strategyExcerpts: undefined });
  return buildSnapshot({ asOf: '2026-10-27', retrievedAt: OLD, funds: [{ code: '000001', name: '合成基金', companyId: 'co1', company: '合成公司', reports }],
    errors: [], barsByStock: {} }, { config, now: NOW });
}
function setup({ outcome = 'parsed', errorCode = null, code = '000001', interim = false, oldSource = sources[0], newSource = sources[1] } = {}) {
  const previous = oldSnapshot({ interim, source: oldSource }), task = patches.taskFor(previous.funds[0], config, NOW, { code });
  assert.ok(task);
  const receipt = { schemaVersion: 1, code, targetPeriodEnd: config.targetPeriodEnd, taskDigest: task.taskDigest,
    analysisDigest: task.analysisDigest, sourceUrl: task.source.sourceUrl, attemptedAt: ATTEMPTED, outcome, errorCode };
  const report = Object.fromEntries(patches.REPORT_FIELDS.filter(key => Object.hasOwn(newSource, key)).map(key => [key, clone(newSource[key])]));
  Object.assign(report, { fundCode: code, sourceUrl: task.source.sourceUrl, periodEnd: task.targetPeriodEnd, publishedAt: task.source.publishedAt, kind: 'quarterly' });
  const patch = { schemaVersion: 1, code, binding: task, receipt, report };
  const batch = { ...config, funds: [{ code, strategyClaimsOnly: task }], traversal: { plannedAt: PLANNED } };
  const raw = { asOf: '2026-10-27', retrievedAt: RECEIVED, funds: [{ code, reports: [] }], errors: [], barsByStock: {},
    strategyClaimsReceipts: [receipt], strategyClaimsPatches: outcome === 'parsed' ? [patch] : [] };
  return { previous, task, receipt, patch, batch, raw };
}
const buildPart = value => buildSnapshot(value.raw, { config: value.batch, now: NOW });

test('task and receipt canonical digests match Python and never depend on observation clock', () => {
  const value = setup();
  const expected = python('import json,sys;from strategy_claim_patches import binding_for,normalize_receipt;v=json.loads(sys.stdin.read());t=binding_for(v["fund"],v["phase"],now=v["now"]);print(json.dumps({"task":t,"receipt":normalize_receipt(v["receipt"],t,v["planned"],v["received"],v["now"])},ensure_ascii=False))',
    { fund: value.previous.funds[0], phase: config, receipt: value.receipt, planned: PLANNED, received: RECEIVED, now: NOW });
  assert.deepEqual(expected.task, value.task);
  assert.deepEqual(expected.receipt, patches.normalizeReceipt(value.receipt, value.task, config, RECEIVED, NOW, PLANNED));
  const changed = clone(value.task); changed.retainedStrategyObservedAt = '2026-10-26T20:00:00Z';
  assert.equal(patches.normalizeTask(changed, config, '000001', NOW).taskDigest, value.task.taskDigest);
  assert.ok(patches.normalizePatch(value.patch, value.task, config, RECEIVED, NOW, PLANNED));
});

test('task binding rejects wrong method, source, quarter, pages, hash, clocks and unsafe URLs', () => {
  const { task } = setup();
  const changes = [x => { x.method = 'explicit-strategy-claims-v1'; }, x => { x.source.periodStart = '2026-01-01'; },
    x => { x.source.sourceTextHash = '0'.repeat(64); }, x => { x.source.pages = [4, 6]; },
    x => { x.analysisDigest = '0'.repeat(64); }, x => { x.taskDigest = '0'.repeat(64); },
    x => { x.retainedStrategyObservedAt = '2026-10-27T04:00:00Z'; }, x => { x.code = '000000'; },
    x => { x.extra = 'not admitted'; }];
  for (const change of changes) { const bad = clone(task); change(bad); assert.equal(patches.normalizeTask(bad, config, '000001', NOW), null); }
  for (const sourceUrl of ['http://example.org/report.pdf', 'https://127.0.0.1/report.pdf', 'https://example.org/report.pdf#',
    'https://user@example.test/report.pdf', 'https://example.org/report.pdf?token=x', 'https://%65xample.org/report.pdf',
    'https://example.local/report.pdf', 'https://example.org:8443/report.pdf', 'https://example.org/\ud800.pdf']) {
    const bad = clone(task); bad.source.sourceUrl = sourceUrl;
    const digest = value => crypto.createHash('sha256').update(patches.canonical(value)).digest('hex');
    bad.analysisDigest = digest({ ...bad.source, method: bad.method });
    bad.taskDigest = digest({ code: bad.code, targetPeriodEnd: bad.targetPeriodEnd, analysisDigest: bad.analysisDigest });
    assert.equal(patches.normalizeTask(bad, config, '000001', NOW), null);
  }
});

test('only a real request receipt survives; request-free failures and microsecond-preplan times are rejected', () => {
  const value = setup();
  for (const [outcome, errorCode] of [['started', null], ['parsed', null], ['failed', 'TIMEOUT'], ['unknown', 'STRATEGY_CLAIMS_INTERRUPTED']]) {
    assert.ok(patches.normalizeReceipt({ ...value.receipt, outcome, errorCode }, value.task, config, RECEIVED, NOW, PLANNED));
  }
  for (const change of [{ outcome: 'started', errorCode: 'TIMEOUT' }, { outcome: 'failed', errorCode: 'STRATEGY_CLAIMS_REQUEST_NOT_STARTED' },
    { outcome: 'failed', errorCode: 'STRATEGY_CLAIMS_BUDGET_REACHED' }, { attemptedAt: '2026-10-26T19:10:00.123455Z' },
    { outcome: 'unknown', errorCode: 'arbitrary source body' }, { taskDigest: '0'.repeat(64) }, { code: '000002' }]) {
    assert.equal(patches.normalizeReceipt({ ...value.receipt, ...change }, value.task, config, RECEIVED, NOW, PLANNED), null);
  }
});

test('new PDF patches require matching full/section source, identity, finite scan, budget and actual fetch clock', () => {
  const value = setup();
  const changes = [x => { x.report.holdings = []; }, x => { x.report.strategyClaimsDerivedFromObservedAt = OLD; },
    x => { x.report.strategyClaimsIdentityEvidence = null; }, x => { x.report.strategyClaims.coverage.scannedClauseCount++; },
    x => { x.report.strategySectionCoverage.sections[0].contentHash = '0'.repeat(64); },
    x => { x.report.narrative.text = 'unpooled extra quote'; }, x => { x.binding.retainedStrategyObservedAt = FETCHED; },
    x => { x.receipt.outcome = 'failed'; x.receipt.errorCode = 'TIMEOUT'; }];
  for (const mutate of changes) { const bad = clone(value.patch); mutate(bad); assert.equal(patches.normalizePatch(bad, value.task, config, RECEIVED, NOW, PLANNED), null); }
  const bad = clone(value.patch), at = '2026-10-26T19:20:00.123455Z';
  bad.report.strategyEvidenceRetrievedAt = at; bad.report.strategyClaims.source.retrievedAt = at;
  bad.report.strategyEvidencePool.source.retrievedAt = at; bad.report.strategyClaimsIdentityEvidence.retrievedAt = at;
  assert.equal(patches.normalizePatch(bad, value.task, config, RECEIVED, NOW, PLANNED), null);
});

test('builder emits only task-bound metadata for successful, failed and interrupted analysis attempts', () => {
  for (const [outcome, errorCode] of [['parsed', null], ['failed', 'TIMEOUT'], ['unknown', 'STRATEGY_CLAIMS_INTERRUPTED'], ['started', null]]) {
    const value = setup({ outcome, errorCode });
    value.raw.strategyClaimsTasks = [{ private: 'ignored raw task' }]; value.raw.strategyClaimsPlannedAt = '1900-01-01T00:00:00Z';
    const part = buildPart(value);
    assert.deepEqual(part.funds, []); assert.equal(part.coverage.batchFreshFundCount, 0);
    assert.deepEqual(part.strategyClaimsReceipts, [value.receipt]);
    assert.deepEqual(part.strategyClaimsTasks, [value.task]); assert.equal(part.strategyClaimsPlannedAt, PLANNED);
    assert.equal(part.strategyClaimsPatches.length, outcome === 'parsed' ? 1 : 0);
    assert.ok(!JSON.stringify(part).includes('ignored raw task'));
  }
});

test('invalid patches preserve real request receipts while unstarted work never manufactures a request', () => {
  const value = setup(); value.raw.strategyClaimsPatches[0].report.strategyClaimsIdentityEvidence = null;
  const part = buildPart(value);
  assert.equal(part.strategyClaimsPatches.length, 0); assert.equal(part.strategyClaimsReceipts.length, 1);
  const deferred = setup(); deferred.raw.strategyClaimsReceipts = []; deferred.raw.strategyClaimsPatches = [];
  deferred.raw.funds[0].collectionAttempt = { status: 'deferred-budget', attempted: false, errorCode: 'BATCH_DEADLINE_REACHED' };
  const empty = buildPart(deferred);
  assert.equal(empty.strategyClaimsReceipts.length, 0); assert.equal(empty.coverage.deferredFundCount, 1);
  assert.equal(empty.batchResults[0].attempted, false);
  delete deferred.raw.funds[0].collectionAttempt;
  assert.throws(() => buildPart(deferred), { code: 'NO_VALID_FRESH_FUNDS' });
});

test('independent tasks cannot claim a second lane or normalize out-of-plan requests', () => {
  for (const field of ['qualityCodes', 'managerCodes', 'strategyCodes', 'enrichmentCodes', 'reportLocatorCodes']) {
    const value = setup(); value.batch.traversal[field] = ['000001'];
    assert.throws(() => buildPart(value), { code: 'NO_VALID_FRESH_FUNDS' });
  }
  for (const field of ['reportLocatorOnly', 'reportLocator', 'priceOnly']) {
    const value = setup(); value.batch.funds[0][field] = true;
    if (field === 'priceOnly') {
      const part = buildPart(value);
      assert.equal(part.strategyClaimsReceipts.length, 0); assert.equal(part.strategyClaimsPatches.length, 0);
    } else assert.throws(() => buildPart(value), { code: 'NO_VALID_FRESH_FUNDS' });
  }
  const value = setup(); value.raw.strategyClaimsReceipts[0].code = '000002';
  assert.throws(() => buildPart(value), { code: 'NO_VALID_FRESH_FUNDS' });
});

test('independent patch enriches retained Q2 interim analysis without refreshing any existing observation', () => {
  const value = setup({ interim: true });
  const old = mergeSnapshots({ snapshots: [value.previous], catalog, config, now: NOW });
  const before = old.companySnapshots.co1.funds[0], part = buildPart(value);
  const result = mergeSnapshots({ snapshots: [part], previousManifest: old.manifest, previousCompanies: old.companySnapshots, catalog, config, now: NOW });
  const fund = result.companySnapshots.co1.funds[0];
  assert.equal(result.counts.acceptedFreshFundCount, 0); assert.equal(result.counts.acceptedStrategyClaimsPatchCount, 1);
  for (const key of ['retrievedAt', 'asOf', 'priceUpdatedAt', 'rows', 'reportManagers', 'managerEvidenceRetrievedAt']) assert.deepEqual(fund[key], before[key]);
  for (const key of ['sourceUrl', 'publishedAt', 'kind', 'holdings', 'navYuan', 'evidenceRetrievedAt', 'navRetrievedAt', 'managerEvidenceRetrievedAt', 'strategyEvidenceRetrievedAt']) {
    assert.deepEqual(fund.selectedReport[key], before.selectedReport[key], key);
  }
  assert.equal(fund.selectedReport.strategyClaims.source.retrievedAt, FETCHED);
  assert.equal(fund.selectedReport.strategyClaimsDerivedFromObservedAt, OLD);
  assert.ok(strategyClaimsFor(fund.selectedReport, { code: fund.code, now: NOW, asOf: fund.asOf }));
  assert.equal(result.receipt.strategyClaims[0].accepted, true); assert.equal(result.receipt.strategyClaims[0].scanned, true);
  const replay = mergeSnapshots({ snapshots: [part, clone(part)], previousManifest: result.manifest, previousCompanies: result.companySnapshots, catalog, config, now: NOW });
  assert.equal(replay.changed, false); assert.equal(replay.counts.acceptedStrategyClaimsPatchCount, 0);
  assert.equal(replay.receipt.strategyClaims.length, 1); assert.equal(replay.receipt.strategyClaims[0].accepted, true);
});

test('changed retained binding blocks adoption but preserves the true attempt receipt', () => {
  const value = setup(), old = mergeSnapshots({ snapshots: [value.previous], catalog, config, now: NOW });
  old.companySnapshots.co1.funds[0].selectedReport.strategyEvidenceRetrievedAt = '2026-10-26T20:00:00Z';
  for (const report of old.companySnapshots.co1.funds[0].reports) report.strategyEvidenceRetrievedAt = '2026-10-26T20:00:00Z';
  const result = mergeSnapshots({ snapshots: [buildPart(value)], previousManifest: old.manifest, previousCompanies: old.companySnapshots, catalog, config, now: NOW });
  assert.equal(result.changed, false); assert.equal(result.receipt.strategyClaims[0].accepted, false);
  assert.equal(result.receipt.strategyClaims[0].mergeErrorCode, 'STRATEGY_CLAIMS_BINDING_CHANGED');
  assert.equal(result.receipt.strategyClaims[0].attemptedAt, ATTEMPTED); assert.equal(result.receipt.strategyClaims[0].outcome, 'parsed');
});

test('started, failed, unknown and orphan patches leave previous observations unchanged under replay', () => {
  for (const [outcome, errorCode] of [['started', null], ['failed', 'TIMEOUT'], ['unknown', 'STRATEGY_CLAIMS_INTERRUPTED'], ['parsed', null]]) {
    const value = setup({ outcome, errorCode }), old = mergeSnapshots({ snapshots: [value.previous], catalog, config, now: NOW });
    const part = buildPart(value), before = JSON.stringify(old);
    if (outcome === 'parsed') part.strategyClaimsReceipts = [];
    const result = mergeSnapshots({ snapshots: [part, clone(part)], previousManifest: old.manifest, previousCompanies: old.companySnapshots, catalog, config, now: NOW });
    assert.equal(result.changed, false); assert.equal(result.counts.acceptedStrategyClaimsPatchCount, 0);
    assert.equal(result.counts.acceptedFreshFundCount, 0); assert.deepEqual(result.manifest, old.manifest);
    assert.equal(JSON.stringify(old), before);
    assert.equal(result.receipt.strategyClaims.length, outcome === 'parsed' ? 0 : 1);
    if (outcome !== 'parsed') {
      assert.equal(result.receipt.strategyClaims[0].attemptedAt, ATTEMPTED);
      assert.equal(result.receipt.strategyClaims[0].outcome, outcome);
      assert.equal(result.receipt.strategyClaims[0].accepted, false);
    }
  }
});

test('a complete finite scan with no classified viewpoint still never claims semantic completion', () => {
  const unknown = python('import json,sys;from test_strategy_evidence_pool import extracted,REPORT;v=json.loads(sys.stdin.read());print(json.dumps([{**REPORT,**extracted(report={**REPORT,"retrievedAt":at},body="本季度市场保持平稳。宏观环境仍需观察。")} for at in v],ensure_ascii=False))', [OLD, FETCHED]);
  const value = setup({ oldSource: unknown[0], newSource: unknown[1] });
  const old = mergeSnapshots({ snapshots: [value.previous], catalog, config, now: NOW });
  const result = mergeSnapshots({ snapshots: [buildPart(value)], previousManifest: old.manifest, previousCompanies: old.companySnapshots, catalog, config, now: NOW });
  const claims = result.companySnapshots.co1.funds[0].selectedReport.strategyClaims;
  assert.equal(claims.coverage.classifiedClauseCount, 0);
  assert.ok(claims.coverage.unknownClauseCount > 0);
  assert.equal(claims.ruleScanComplete, true); assert.equal(claims.semanticAnalysisComplete, false);
  assert.equal(result.receipt.strategyClaims[0].scanned, true);
});

test('a verified share-class task updates its final representative once without duplicating the portfolio', () => {
  const value = setup({ code: '000002' }), old = mergeSnapshots({ snapshots: [value.previous], catalog, config, now: NOW });
  const result = mergeSnapshots({ snapshots: [buildPart(value)], previousManifest: old.manifest, previousCompanies: old.companySnapshots, catalog, config, now: NOW });
  assert.equal(result.counts.fundCount, 1); assert.equal(result.companySnapshots.co1.funds[0].code, '000001');
  assert.equal(result.receipt.strategyClaims[0].code, '000002'); assert.equal(result.receipt.strategyClaims[0].accepted, true);
  assert.ok(strategyClaimsFor(result.companySnapshots.co1.funds[0].selectedReport, { code: '000002', now: NOW }));
  const unsupported = clone(value.previous.funds[0]); unsupported.selectedReport.portfolioIdentityEvidence = null;
  assert.equal(patches.taskFor(unsupported, config, NOW, { code: '000002' }), null);
});

test('disk components retain strategy tasks, zero-fresh receipts and final accepted counts', () => {
  const value = setup(), old = mergeSnapshots({ snapshots: [value.previous], catalog, config, now: NOW });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'strategy-patch-test-'));
  try {
    const data = path.join(tmp, 'data'), parts = path.join(tmp, 'parts');
    fs.mkdirSync(path.join(data, 'companies'), { recursive: true }); fs.mkdirSync(parts);
    fs.writeFileSync(path.join(data, 'latest.json'), JSON.stringify(old.manifest));
    for (const [key, envelope] of Object.entries(old.companySnapshots)) fs.writeFileSync(path.join(data, 'companies', key + '.json'), JSON.stringify(envelope));
    fs.writeFileSync(path.join(parts, 'part.json'), JSON.stringify(buildPart(value)));
    const result = mergeFiles({ parts, data, catalog, config, previousManifest: old.manifest, now: NOW });
    assert.equal(result.counts.acceptedStrategyClaimsPatchCount, 1);
    assert.equal(result.counts.acceptedFreshFundCount, 0);
    assert.equal(result.receipt.strategyClaims.length, 1); assert.equal(result.receipt.strategyClaims[0].accepted, true);
    const next = JSON.parse(fs.readFileSync(path.join(data, 'latest.json')));
    const fund = JSON.parse(fs.readFileSync(path.join(data, 'companies', path.basename(next.companyFiles[0].path)))).funds[0];
    assert.equal(fund.retrievedAt, OLD); assert.equal(fund.selectedReport.strategyClaims.source.retrievedAt, FETCHED);
    assert.ok(!Object.hasOwn(fund, 'strategyClaimsTasks'));
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
});
