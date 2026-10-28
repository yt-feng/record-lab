'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const view = require('../web/price-representations');
const record = () => ({ kind: 'price-representation-classification',
  key: 'CN|000636|CNY|unadjusted|2026-07-01|2026-09-30|exchange-sessions-2026-q2-q3-v1',
  eligible: true, classification: 'stored-fact-representation-compatible', scope: 'stored-facts-only',
  method: 'exact-or-yahoo-equals-binary32-eastmoney', classificationDigest: 'a'.repeat(64),
  rawRegistryDigest: 'b'.repeat(64), proof: { evidenceSha256: 'c'.repeat(64) }, sourceDataCommit: 'd'.repeat(40),
  observationCount: 24, mathematicallyClassifiedObservationCount: 24, directlyBoundRecheckObservationCount: 2,
  uncoveredObservationCount: 22, factCount: 2, automaticResolution: false, quotesModified: false,
  rawConflictModified: false, ledgerModified: false, marketDataRequests: 0 });
const document = row => ({ schemaVersion: 1, kind: 'price-representations', quarter: '2026Q3',
  targetPeriodEnd: '2026-09-30', records: [row] });
test('view distinguishes 24 fact-compatible observations from two bound rechecks', () => {
  const row = record(), original = structuredClone(row);
  assert.equal(view.entries(document(row), '2026Q3').length, 1);
  assert.match(view.summary(row), /24\/24/); assert.match(view.summary(row), /2\/24/);
  assert.match(view.summary(row), /未逐次复查原响应/); assert.deepEqual(row, original);
});
test('wrong quarter, unknown key, fake response coverage and resolution do not enter view', () => {
  assert.deepEqual(view.entries(document(record()), '2026Q2'), []);
  for (const change of [{ key: record().key.replace('000636', '000637') }, { directlyBoundRecheckObservationCount: 24 },
    { uncoveredObservationCount: 0 }, { eligible: false }, { quotesModified: true }, { automaticResolution: true }]) {
    assert.deepEqual(view.entries(document({ ...record(), ...change }), '2026Q3'), []);
  }
});
