'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { validate, MAX_LINE_BYTES } = require('../scripts/strategy-claims-state');
const script = path.join(__dirname, '../scripts/strategy-claims-state.js');
const generated = spawnSync('python3', ['-c', 'import json,sys;sys.path[:0]=["scripts","tests"];from test_strategy_evidence_pool import extracted,REPORT;print(json.dumps([{**REPORT,**extracted()},{**REPORT,**extracted(body="市场表现尚待观察。")}],ensure_ascii=False))'],
  { cwd: path.join(__dirname, '..'), encoding: 'utf8', maxBuffer: 200000 });
assert.equal(generated.status, 0, generated.stderr);
const reports = JSON.parse(generated.stdout);
const value = (index = 0) => ({ code: '000001', now: '2026-10-27T02:00:00Z', asOf: '2026-10-27', report: structuredClone(reports[index]) });

test('strict hydration admits all-unknown scans but rejects wrong source identity and fabricated flags', () => {
  for (const index of [0, 1]) assert.deepEqual(validate(value(index)), {
    code: '000001', analysisDigest: reports[index].strategyClaims.source.digest, scanned: true });
  for (const mutate of [v => v.code = '000099', v => v.report.strategyClaims.coverage.scannedClauseCount++,
    v => v.report.strategyEvidencePool.digest = '0'.repeat(64), v => v.report.strategyClaims.semanticAnalysisComplete = true,
    v => v.report.strategyEvidenceRetrievedAt = '2026-10-27T03:00:00Z']) {
    const item = value(); mutate(item); assert.equal(validate(item).scanned, false);
  }
});

test('one NDJSON process returns bounded metadata only for successive observations', () => {
  const inputs = Array.from({ length: 12 }, (_, i) => value(i % 2));
  const result = spawnSync(process.execPath, [script], { input: inputs.map(v => JSON.stringify(v)).join('\n') + '\n', encoding: 'utf8', maxBuffer: 20000 });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.stdout.trim().split('\n').map(JSON.parse), inputs.map(validate));
  assert.ok(!result.stdout.includes('sourceUrl'));
  assert.ok(!result.stdout.includes('text'));
});

test('oversized or invalid input exits with a fixed classification and no raw material', () => {
  for (const input of ['{"privateMarker":', JSON.stringify({ privateMarker: 'x'.repeat(MAX_LINE_BYTES + 1) }) + '\n']) {
    const result = spawnSync(process.execPath, [script], { input, encoding: 'utf8', maxBuffer: 1000 });
    assert.equal(result.status, 1);
    assert.equal(result.stderr, 'STRATEGY_STATE_INPUT_INVALID\n');
    assert.equal(result.stdout, '');
  }
});
