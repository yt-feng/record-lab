'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const { buildSnapshot, isValidNoEq } = require('../scripts/build-data');
const { normalizeReport } = require('../lib/fund-analysis');
const { mergeSnapshots } = require('../scripts/merge-parts');
const ui = require('../web/app');
const root = path.resolve(__dirname, '..');
const fixture = require('./fixtures/nonstock-assets.json');
const parser = "import sys,json;sys.path.insert(0,'scripts');from nonstock_assets import parse_nonstock_assets;x=json.load(sys.stdin);print(json.dumps(parse_nonstock_assets(x['pages'],x['report'],page_numbers=x['pageNumbers'])))";
const parsed = spawnSync('python3', ['-c', parser], { cwd: root, input: JSON.stringify(fixture), encoding: 'utf8', timeout: 5000 });
assert.equal(parsed.status, 0);
const proof = JSON.parse(parsed.stdout);
const now = '2026-10-26T12:00:00Z';
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const config = { ...phase, funds: [{ code: '000001' }] };
const holding = { stockCode: '600001', stockName: '合成上期证券', market: 'CN', currency: 'CNY', shares: 100, marketValueYuan: 500, weightPct: 5 };
const current = () => ({ ...fixture.report, fundCode: '000001', holdings: [], coverage: 'full', navYuan: 10000,
  noEquitiesConfirmed: true, noEquitiesEvidence: structuredClone(proof) });
const prior = () => ({ fundCode: '000001', periodEnd: phase.baselinePeriodEnd, publishedAt: '2026-04-20', kind: 'quarterly',
  coverage: 'top10', sourceUrl: 'https://example.org/prior.pdf', navYuan: 10000, holdings: [holding] });
function build(reports = [current(), prior()]) {
  return buildSnapshot({ asOf: now.slice(0, 10), retrievedAt: now, errors: [], barsByStock: {},
    funds: [{ code: '000001', name: '合成基金', company: '合成机构', companyId: 'co1', managers: [], reports }] }, { now, config });
}

test('both report and fund-level builder paths retain normalized proof without invented quote text', () => {
  assert.equal(isValidNoEq(proof, { asOf: now.slice(0, 10), code: '000001' }), true);
  assert.deepEqual(normalizeReport(current()).noEquitiesEvidence, proof);
  const snapshot = build();
  assert.deepEqual(snapshot.errors, []);
  const fund = snapshot.funds[0];
  assert.equal(fund.status, 'no-equities');
  assert.deepEqual(fund.noEquitiesEvidence, proof);
  assert.equal(fund.noEquitiesEvidence.text, undefined);
  assert.equal(fund.previousReport.periodEnd, phase.baselinePeriodEnd);
  assert.equal(fund.rows.length, 1);
  assert.equal(fund.rows[0].current, null);
  assert.equal(fund.rows[0].previous.shares, 100);
  assert.equal(fund.rows[0].change.kind, 'left-disclosure');
  assert.equal(fund.rows[0].change.sharesDelta, null);
  assert.equal(fund.rows[0].position.disclosedYuan, null);
  assert.equal(ui.validateDataset(snapshot), snapshot);
  const envelope = { asOf: now.slice(0, 10), retrievedAt: now, errors: [], barsByStock: {},
    funds: [{ code: '000001', name: '合成基金', managers: [], reports: [prior()], noEquitiesEvidence: proof }] };
  assert.deepEqual(buildSnapshot(envelope, { now, config }).funds[0].noEquitiesEvidence, proof);
});

test('merge and final observation keep full nested proof and the previous-only security', () => {
  const catalog = { fundCount: 1, companyCount: 1, companies: [{ code: 'co1', name: '合成机构', fundCount: 1 }],
    funds: [{ code: '000001', name: '合成基金', companyCode: 'co1', companyName: '合成机构' }] };
  const merged = mergeSnapshots({ snapshots: [build()], catalog, config, now });
  const company = merged.companySnapshots.co1;
  assert.equal(company.funds[0].rows[0].previous.shares, 100);
  assert.deepEqual(company.funds[0].noEquitiesEvidence, proof);
  assert.equal(ui.validateDataset(company), company);
  assert.deepEqual(merged.receipt.funds[0].reportEvidence.noEquities, proof);
  const check = "import sys,json;sys.path.insert(0,'scripts');import traversal;x=json.load(sys.stdin);print(json.dumps(traversal.valid_event_evidence(x['evidence'],'no-equities',x['phase'],traversal.utc_now(x['now']))))";
  const result = spawnSync('python3', ['-c', check], { cwd: root, input: JSON.stringify({ evidence: merged.receipt.funds[0].reportEvidence, phase, now }), encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 0); assert.equal(JSON.parse(result.stdout), true);
});

test('invalid accounting, period/source mismatch and current stock rows cannot survive build or browser validation', () => {
  for (const change of [report => { report.noEquitiesEvidence.assetTable.rows[0].amount = '4000.01'; },
    report => { report.noEquitiesEvidence.periodEnd = '2026-09-30'; }, report => { report.noEquitiesEvidence.sourceUrl = 'https://example.org/another.pdf'; },
    report => { report.kind = 'semiannual'; },
    report => { report.holdings = [holding]; }]) {
    const report = current(); change(report);
    assert.throws(() => build([report]), /NO_VALID_FRESH_FUNDS/);
  }
  for (const change of [fund => { fund.noEquitiesEvidence.otherAssetsTable.total.amount = '0.00'; },
    fund => { fund.noEquitiesEvidence.pages = [1, 2, 3]; }, fund => { fund.rows[0].current = holding; }]) {
    const snapshot = build(); change(snapshot.funds[0]);
    assert.throws(() => ui.validateDataset(snapshot), /INVALID_DATASET/);
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

test('actual browser scripts render both source tables, physical page links and retained prior disclosure', async () => {
  const elements = new Map();
  const document = { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }), addEventListener() {} };
  const context = vm.createContext({ document, URL, AbortController, Intl, console,
    setTimeout: () => 1, clearTimeout() {}, fetch: () => new Promise(() => {}), location: { href: 'https://example.org/' } });
  for (const filename of ['lifecycle.js', 'zero-stock.js', 'nonstock-assets.js', 'source-dates.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'web', filename), 'utf8'), context, { filename });
  }
  const json = JSON.stringify(build());
  const input = document.getElementById('import-file');
  await input.events.change({ target: { files: [{ size: json.length, text: async () => json }], value: '' } });
  const rendered = document.getElementById('fund-results').textContent;
  assert.match(document.getElementById('load-status').textContent, /正在查看本地导入/);
  assert.match(rendered, /期末股票投资账面金额为零/);
  assert.match(rendered, /结论仅对应本期期末股票投资账面金额/);
  assert.match(rendered, /原 PDF 第 8 页/); assert.match(rendered, /原 PDF 第 12、13 页/);
  assert.match(rendered, /合成上期证券/); assert.match(rendered, /保留上期披露股票/);
  const walk = node => [node, ...(node.children || []).flatMap(walk)];
  const urls = walk(document.getElementById('fund-results')).map(node => node.href).filter(Boolean);
  assert.ok(urls.includes(`${proof.sourceUrl}#page=8`)); assert.ok(urls.includes(`${proof.sourceUrl}#page=12`));
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  assert.ok(html.indexOf('./nonstock-assets.js') < html.indexOf('./app.js'));
});
