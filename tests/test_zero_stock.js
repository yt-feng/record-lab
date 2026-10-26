'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const evidence = require('./fixtures/zero-stock.json');
const { validZeroStock } = require('../web/zero-stock');
const { buildSnapshot, isValidNoEq } = require('../scripts/build-data');
const { validateDataset } = require('../web/app');

test('dated zero-stock balance sheet survives normalization and browser validation', () => {
  assert.equal(validZeroStock(evidence), true);
  assert.equal(isValidNoEq(evidence, { asOf: '2026-10-26', code: '000001' }), true);
  const snapshot = buildSnapshot({ asOf: '2026-10-26', retrievedAt: '2026-10-26T00:00:00Z',
    funds: [{ code: '000001', name: '合成基金', company: '合成公司', managers: [], reports: [], noEquitiesEvidence: evidence }],
    barsByStock: {}, errors: [] }, { now: '2026-10-26T01:00:00Z',
    config: { funds: [{ code: '000001' }], targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' } });
  assert.equal(snapshot.funds[0].status, 'no-equities');
  assert.equal(snapshot.funds[0].selectedReport.noEquitiesEvidence.evidenceType, 'zero-stock-balance-sheet');
  assert.equal(snapshot.funds[0].rows.length, 0);
  assert.doesNotThrow(() => validateDataset(snapshot));
  snapshot.funds[0].selectedReport.noEquitiesEvidence.text = evidence.text.replace('- -', '12.00 -');
  snapshot.funds[0].noEquitiesEvidence = snapshot.funds[0].selectedReport.noEquitiesEvidence;
  assert.throws(() => validateDataset(snapshot));
});

test('zero-stock validator rejects mixed dates, non-stock rows and historical zero with current value', () => {
  for (const text of [evidence.text.replace('- -', '1.00 -'), evidence.text.replace('- -', '- 1.00'),
    evidence.text.replace('股票投资', '股票投资收益'), evidence.text.replaceAll('2026年6月30日', '2025年6月30日'),
    evidence.text.replace('单位：人民币元', '单位：港元'), evidence.text + '\n附加文字']) {
    assert.equal(validZeroStock({ ...evidence, text }), false);
  }
  assert.equal(validZeroStock({ ...evidence, kind: 'quarterly' }), false);
});
