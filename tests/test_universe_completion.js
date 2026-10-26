'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const app = require('../web/app');
const { analyzeQuarter } = require('../scripts/analyze-data');

test('the UI separates a fully processed known directory from unverified historical scope', () => {
  const progress = { completionStatus: 'incomplete-exhausted', quarterUniverse: { schemaVersion: 1,
    required: true, targetPeriodEnd: '2026-06-30', historicalUniverseVerified: false,
    historicalAddedFundCount: 2, historicalParseStatus: 'complete' },
    completionAudit: { knownDirectoryComplete: true } };
  assert.match(app.retryState(progress).label, /历史范围待核验/);
  assert.match(app.universeDescription(progress, null, '2026-06-30'), /历史来源补入 2 个/);
  assert.match(app.universeDescription(progress, null, '2026-06-30'), /产品及份额范围仍需逐项核对/);
  assert.doesNotMatch(app.universeDescription(progress, null, '2026-09-30'), /补入 2/);
  assert.match(app.universeDescription(null, null, '2026-06-30'), /尚未完整核验/);
});

test('zero records or a legacy complete flag cannot claim complete industry analysis', () => {
  const manifest = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: '2026-06-30',
    baselinePeriodEnd: '2026-03-31', asOf: '2026-10-26', retrievedAt: '2026-10-26T10:00:00Z',
    coverage: { fundCount: 0, catalogFundCount: 0 }, companyFiles: [] };
  const result = analyzeQuarter(manifest, () => { throw new Error('no partitions expected'); }, {
    now: '2026-10-26T11:00:00Z', progress: { completionStatus: 'complete',
      phase: { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' },
      research: { completeFunds: 0, applicableFunds: 0 } } });
  assert.equal(result.analysisVersion, 7);
  assert.equal(result.coverage.complete, false);
  assert.equal(result.coverage.historicalUniverseVerified, false);
  assert.equal(result.coverage.historicalUniverseStatus, 'unchecked');
});
