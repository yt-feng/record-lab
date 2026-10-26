'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { validNonstockAssets, normalizeNonstockAssets } = require('../web/nonstock-assets');
const root = path.resolve(__dirname, '..');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/nonstock-assets.json')));
function parsePython(input = fixture) {
  const script = "import sys,json;sys.path.insert(0,'scripts');from nonstock_assets import parse_nonstock_assets;x=json.load(sys.stdin);print(json.dumps(parse_nonstock_assets(x['pages'],x['report'],page_numbers=x['pageNumbers']),ensure_ascii=False))";
  const result = spawnSync('python3', ['-c', script], { cwd: root, input: JSON.stringify(input), encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 0, 'synthetic Python parser must run');
  return JSON.parse(result.stdout);
}
const proof = parsePython();

test('browser validator accepts the exact Python-produced source-bounded schema', () => {
  assert.equal(validNonstockAssets(proof), true);
  assert.deepEqual(normalizeNonstockAssets(proof), proof);
  assert.equal(proof.stockBookValue, '0.00');
  assert.deepEqual(proof.pages, [8, 12, 13]);
  assert.equal(proof.assetTable.rows.at(-1).amount, '0.10');
  assert.equal(proof.assetTable.rows.at(-1).weightPct, '0.00');
});

test('zero percentage cannot erase positive unexplained other assets', () => {
  for (const mutate of [item => { item.otherAssetsTable = null; },
    item => { item.otherAssetsTable.text = item.otherAssetsTable.text.replace('7 其他 -', '7 其他 0.01'); },
    item => { item.otherAssetsTable.total.amount = '0.00'; }]) {
    const modified = structuredClone(proof); mutate(modified); assert.equal(validNonstockAssets(modified), false);
  }
});

test('each cent and each displayed percentage must agree with the source tables', () => {
  for (const [before, after] of [['2,999.90', '2,999.91'], ['3,000.00 30.00', '3,000.00 29.99'],
    ['100.00', '99.99'], ['0.10 0.00', '0.10 -'], ['固定收益投资', '权益投资']]) {
    const modified = structuredClone(proof); modified.assetTable.text = modified.assetTable.text.replace(before, after);
    assert.equal(validNonstockAssets(modified), false);
  }
  const modified = structuredClone(proof); modified.assetTable.rows[0].amount = '9999.99';
  assert.equal(validNonstockAssets(modified), false);
});

test('only rounding explained by every individual row can produce a 99.99 percent sum', () => {
  const changed = structuredClone(fixture);
  changed.pages[0] = changed.pages[0].replaceAll('4,000.00 40.00', '3,333.33 33.33')
    .replace('3,000.00 30.00', '3,333.33 33.33').replace('2,999.90 30.00', '3,333.24 33.33');
  const rounded = parsePython(changed);
  assert.equal(validNonstockAssets(rounded), true);
  rounded.assetTable.text = rounded.assetTable.text.replace('3,333.24 33.33', '3,333.24 33.34');
  assert.equal(validNonstockAssets(rounded), false);
});

test('missing section bounds, skipped pages, wrong periods and wrong units cannot prove zero', () => {
  const edits = [item => { item.assetTable.sectionBounded = false; },
    item => { item.otherAssetsTable.pageNumbers = [12, 14]; }, item => { item.otherAssetsTable.pageNumbersVerified = false; },
    item => { item.reportPeriodText = '2026年第3季度报告'; }, item => { item.sourceUrl = 'https://localhost/report.pdf'; },
    item => { item.assetTable.text = item.assetTable.text.replace('金额（元）', '金额（万元）'); },
    item => { item.assetTable.text = item.assetTable.text.replace('基金总资产', '基金资产净值'); },
    item => { item.schemaVersion = true; }, item => { item.assetTable.rows[0].rank = true; }];
  for (const edit of edits) { const modified = structuredClone(proof); edit(modified); assert.equal(validNonstockAssets(modified), false); }
});

test('large monetary values use integer cents rather than JavaScript floating point sums', () => {
  const replacements = [['4,000.00', '400000000000000.00'], ['3,000.00', '300000000000000.00'],
    ['2,999.90', '299990000000000.00'], ['10,000.00', '1000000000000000.00'], ['0.10', '10000000000.00']];
  let asset = fixture.pages[0]; for (const [before, after] of replacements) asset = asset.replaceAll(before, after);
  const input = structuredClone(fixture); input.pages[0] = asset;
  input.pages[1] = input.pages[1].replace('0.04', '4000000000.00').replace('0.06', '6000000000.00').replace('0.10', '10000000000.00');
  const output = parsePython(input);
  assert.equal(validNonstockAssets(output), true);
  assert.equal(output.assetTable.total.amount, '1000000000000000.00');
  assert.ok(BigInt(output.assetTable.total.amount.replace('.', '')) > BigInt(Number.MAX_SAFE_INTEGER));
});

test('normalization strips unrelated diagnostics and retains only bounded public evidence', () => {
  const modified = structuredClone(proof); modified.diagnostic = 'not public evidence'; modified.assetTable.extra = 'not public evidence';
  const result = normalizeNonstockAssets(modified);
  assert.equal(result.diagnostic, undefined); assert.equal(result.assetTable.extra, undefined);
  assert.equal(result.assetTable.text.length <= 800 && result.otherAssetsTable.text.length <= 600, true);
});
