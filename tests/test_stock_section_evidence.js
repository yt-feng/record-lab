'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { validExplicitNoStock } = require('../web/zero-stock');
const text = '7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n本基金不投资于股票。\n7.4 报告期内股票投资组合的重大变动';
const proof = { evidenceType: 'explicit-no-stock-detail', text, periodEnd: '2026-06-30', kind: 'semiannual' };

test('browser and publisher recognize only the complete explicit empty-stock subsection', () => {
  assert.equal(validExplicitNoStock(proof), true);
  const variants = [text.replace('所有股票', '前十名股票'), text.replace('所有股票', '所有受限股票'),
    text.replace('所有股票投资明细', '所有股票期权投资明细'), text.replace('7.4 ', '7.5 '),
    text.replace('本基金不投资于股票。', '本基金不投资于股票。另持有股票。'),
    text.replace('本基金不投资于股票。', '本基金并非不投资于股票。'), text.split('\n7.4')[0]];
  for (const value of variants) assert.equal(validExplicitNoStock({ ...proof, text: value }), false, value);
  assert.equal(validExplicitNoStock({ ...proof, kind: 'annual' }), false);
  assert.equal(validExplicitNoStock({ ...proof, periodEnd: '2026-09-30' }), false);
});
