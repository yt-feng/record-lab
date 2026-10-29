'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const { normalizeNonstockAssets } = require('../web/nonstock-assets');
const { buildSnapshot } = require('../scripts/build-data');
const ui = require('../web/app');
const root = path.resolve(__dirname, '..');
const fixture = require('./fixtures/nonstock-zero-other-assets.json');
function python(mode, value) {
  const script = `import sys,json;sys.path.insert(0,'scripts');from nonstock_assets import parse_nonstock_assets,normalize_nonstock_assets;x=json.load(sys.stdin);print(json.dumps(${mode === 'parse' ? "parse_nonstock_assets(x['pages'],x['report'],page_numbers=x['pageNumbers'])" : 'normalize_nonstock_assets(x)'},ensure_ascii=False))`;
  const proc = spawnSync('python3', ['-c', script], { cwd: root, input: JSON.stringify(value), encoding: 'utf8', timeout: 5000 });
  assert.equal(proc.status, 0); return JSON.parse(proc.stdout);
}
const proof = python('parse', fixture);
const now = '2026-10-29T01:00:00Z';
const oldClock = '2026-10-20T01:02:03.123456Z';
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '003589' }] };
const current = () => ({ ...fixture.report, fundCode: '003589', holdings: [], coverage: 'full',
  noEquitiesConfirmed: true, noEquitiesEvidence: structuredClone(proof), evidenceRetrievedAt: now });
const unknown = () => ({ sourceUrl: 'https://example.org/interim.pdf', periodEnd: '2026-06-30', publishedAt: '2026-08-31',
  kind: 'semiannual', coverage: 'unknown', holdings: [], reportOnly: true, noEquitiesConfirmed: false,
  parseMethod: 'pypdf', sourceTextHash: 'b'.repeat(64), pageStats: { charCount: 100, totalPages: 1, processedPages: 1, pageCount: 1 },
  evidenceRetrievedAt: oldClock });
const stock = () => ({ ...fixture.report, coverage: 'top10', holdings: [{ stockCode: '600001', market: 'CN', currency: 'CNY',
  stockName: '合成证券', shares: 10, marketValueYuan: 100, weightPct: 1 }], evidenceRetrievedAt: oldClock });
function build(reports, previous = null) {
  return buildSnapshot({ asOf: now.slice(0, 10), retrievedAt: now, errors: [], barsByStock: {},
    funds: [{ code: '003589', name: '合成基金', companyId: 'co1', managers: [], reports }] }, { now, config, previous });
}

test('Python and browser retain an independent bounded statement without inventing table cells', () => {
  assert.ok(proof);
  assert.deepEqual(normalizeNonstockAssets(proof), proof);
  assert.equal(proof.otherAssetsTable, undefined);
  assert.equal(proof.otherAssetsStatement.rows, undefined);
  assert.equal(proof.otherAssetsStatement.total, undefined);
  assert.equal(proof.otherAssetsStatement.amountUnit, undefined);
  assert.deepEqual(proof.pages, [9, 12]);
  const legacy = python('parse', require('./fixtures/nonstock-assets.json'));
  assert.deepEqual(normalizeNonstockAssets(legacy), legacy);
  assert.equal(legacy.method, 'two-closed-nonstock-asset-tables');
  assert.equal(legacy.otherAssetsStatement, undefined);
});

test('both validators reject altered statement meaning, nonzero assets, bounds and ambiguous forms', () => {
  const mutations = [p => { p.otherAssetsStatement.text = p.otherAssetsStatement.text.replace('未持有', '无法确认未持有'); },
    p => { p.otherAssetsStatement.text = p.otherAssetsStatement.text.replace('本报告期末', '上报告期末'); },
    p => { p.otherAssetsStatement.text += '\n另有其他投资。'; },
    p => { p.otherAssetsStatement.sectionBounded = false; },
    p => { p.otherAssetsStatement.pageNumbers = [12, 14]; },
    p => { p.otherAssetsStatement.statementType = 'generic-empty'; },
    p => { p.assetTable.rows.at(-1).amount = '0.01'; },
    p => { p.assetTable.text = p.assetTable.text.replace('其他资产  -  -', '其他资产  0.01  0.00'); },
    p => { p.otherAssetsTable = {}; },
    p => { p.reportPeriodText = '2026年第3季度报告'; },
    p => { p.method = 'two-closed-nonstock-asset-tables'; }];
  for (const mutate of mutations) {
    const changed = structuredClone(proof); mutate(changed);
    assert.equal(normalizeNonstockAssets(changed), null);
    assert.equal(python('normalize', changed), null);
  }
});

test('unknown report-only holdings can recover without changing the old observation clock', () => {
  const old = build([unknown()]);
  assert.equal(old.funds[0].status, 'unavailable');
  const before = structuredClone(old);
  const recovered = build([current()], old);
  assert.deepEqual(old, before);
  assert.deepEqual(recovered.errors, []);
  const fund = recovered.funds[0];
  assert.equal(fund.status, 'no-equities');
  assert.equal(fund.selectedReport.sourceUrl, fixture.report.sourceUrl);
  assert.equal(fund.selectedReport.kind, 'quarterly');
  assert.equal(fund.reports.find(r => r.sourceUrl === 'https://example.org/interim.pdf').evidenceRetrievedAt, oldClock);
  assert.equal(fund.noEquitiesEvidence.method, proof.method);
  assert.equal(ui.validateDataset(recovered), recovered);
});

test('verified stock contradictions and invalid new proofs remain rejected', () => {
  assert.throws(() => build([current()], build([stock()])), /NO_VALID_FRESH_FUNDS/);
  assert.throws(() => build([stock()], build([current()])), /NO_VALID_FRESH_FUNDS/);
  for (const mutate of [r => { r.noEquitiesEvidence.otherAssetsStatement.sectionBounded = false; },
    r => { r.kind = 'semiannual'; }, r => { r.noEquitiesEvidence.sourceUrl = 'https://example.org/unrelated.pdf'; }]) {
    const bad = current(); mutate(bad);
    assert.throws(() => build([bad], build([unknown()])), /NO_VALID_FRESH_FUNDS/);
  }
});

class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.events = {}; this.value = ''; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent ?? String(child)).join(' '); }
  get options() { return this.children; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
}

test('actual UI renders the original statement and its page rather than a fabricated table', async () => {
  const elements = new Map();
  const document = { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }), addEventListener() {} };
  const context = vm.createContext({ document, URL, AbortController, Intl, console,
    setTimeout: () => 1, clearTimeout() {}, fetch: () => new Promise(() => {}), location: { href: 'https://example.org/' } });
  for (const filename of ['lifecycle.js', 'zero-stock.js', 'nonstock-assets.js', 'source-dates.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'web', filename), 'utf8'), context, { filename });
  }
  const data = JSON.stringify(build([current()]));
  await document.getElementById('import-file').events.change({ target: { files: [{ size: data.length, text: async () => data }], value: '' } });
  const output = document.getElementById('fund-results');
  assert.match(output.textContent, /原 PDF 第 9 页/);
  assert.match(output.textContent, /原 PDF 第 12 页/);
  assert.match(output.textContent, /本基金本报告期末未持有其他资产/);
  assert.match(output.textContent, /查看原声明/);
  assert.doesNotMatch(output.textContent, /其他资产明细逐项按分/);
  const walk = node => [node, ...(node.children || []).flatMap(walk)];
  assert.ok(walk(output).some(node => node.href === fixture.report.sourceUrl + '#page=12'));
});
