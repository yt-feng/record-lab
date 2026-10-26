'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { analyzeQuarter } = require('../scripts/analyze-data');
const ui = require('../web/analysis');

const NOW = '2026-10-26T12:00:00.000Z';
const config = id => id === '2026Q2'
  ? { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' }
  : { targetPeriodEnd: '2026-09-30', baselinePeriodEnd: '2026-06-30' };
function emptyManifest(id = '2026Q2') {
  const catalog = { companies: [{ code: 'co1', name: '合成公司', fundCount: 1 }],
    funds: [{ code: '000001', name: '合成基金', companyCode: 'co1', companyName: '合成公司' }] };
  // An entirely failed collection emits no usable part; the initial merger
  // still publishes catalog coverage without an observation timestamp.
  assert.throws(() => buildSnapshot({ asOf: '2026-10-26', retrievedAt: NOW, funds: [{ code: '000001', reports: [] }],
    errors: [], barsByStock: {} }, { now: NOW, config: { ...config(id), funds: [{ code: '000001' }] } }), /NO_VALID_FRESH_FUNDS/);
  const merged = mergeSnapshots({ snapshots: [null], catalog, config: config(id), now: NOW, receiptCodes: ['000001'] });
  assert.equal(merged.counts.acceptedFreshFundCount, 0);
  assert.equal(merged.manifest.sourceMode, 'actions');
  assert.equal(merged.manifest.coverage.status, 'awaiting-data');
  assert.deepEqual(merged.companySnapshots, {});
  return merged.manifest;
}
function analyze(manifest, options = {}) {
  return analyzeQuarter(manifest, () => assert.fail('empty manifests must not load company partitions'), {
    now: NOW, writePart: () => assert.fail('empty analysis must not emit detail partitions'), ...options });
}

test('all-failed initial Q2 and Q3 merges generate valid empty analysis with null observation dates', () => {
  for (const id of ['2026Q2', '2026Q3']) {
    const manifest = emptyManifest(id);
    const original = structuredClone(manifest);
    const result = analyze(manifest, { progress: { phase: config(id), completionStatus: 'complete', processedFunds: 1,
      research: { completeFunds: 0, applicableFunds: 0 } } });
    assert.equal(result.analysisVersion, 7);
    assert.equal(result.coverage.status, 'awaiting-data');
    assert.equal(result.coverage.complete, false);
    assert.equal(result.coverage.historicalUniverseVerified, false);
    assert.equal(result.asOf, null);
    assert.equal(result.retrievedAt, null);
    assert.equal(result.priceUpdatedAt, null);
    assert.equal(result.generatedAt, NOW);
    assert.equal(result.generation, NOW);
    assert.deepEqual([result.stocks, result.themes, result.portfolioFiles, result.topStockKeys], [[], [], [], []]);
    for (const key of ['inputRecords', 'analyzedRecords', 'stockCount', 'contributionCount']) assert.equal(result.coverage[key], 0);
    assert.equal(result.coverage.sourceCatalogShareCodes, 1);
    assert.deepEqual(manifest, original);
    assert.equal(ui.validate(result, id), result);
  }
});

test('empty-state permission rejects records, partitions, missing dates and contradictory observed counts', () => {
  const mutations = [
    m => { m.coverage.status = 'partial'; },
    m => { m.sourceMode = 'awaiting-data'; },
    m => { m.asOf = '2026-10-26'; },
    m => { m.retrievedAt = NOW; },
    m => { delete m.asOf; },
    m => { delete m.retrievedAt; },
    m => { m.priceUpdatedAt = NOW; },
    m => { m.asOf = '2026-10-26'; m.retrievedAt = NOW; },
    m => { m.funds.push({ code: '000001' }); },
    m => { delete m.funds; },
    m => { m.companyFiles.push({ path: './data/companies/co1.json', fundCount: 0 }); },
    m => { m.coverage.fundCount = 1; },
    m => { m.coverage.fundCount = '0'; },
    m => { delete m.coverage.fundCount; },
    m => { m.coverage.holdingCount = 1; },
    m => { m.coverage.companyCount = 1; },
    m => { m.coverage.priceAvailable = 1; },
    m => { m.coverage.complete = true; },
    m => { m.companyCoverage[0].availableFundCount = 1; },
    m => { m.companyCoverage[0].availablePortfolioCount = 1; },
  ];
  for (const change of mutations) {
    const manifest = emptyManifest(); change(manifest);
    assert.throws(() => analyze(manifest), /INVALID_ANALYSIS_MANIFEST/);
  }
  const wrongPeriod = emptyManifest(); wrongPeriod.baselinePeriodEnd = '2025-12-31';
  assert.throws(() => analyze(wrongPeriod), /INVALID_ANALYSIS_PERIOD/);
  assert.throws(() => analyze(emptyManifest(), { now: 'invalid' }), /ANALYSIS_FUTURE_OBSERVATION/);
  assert.throws(() => analyze(emptyManifest(), { progress: { phase: config('2026Q3') } }), /ANALYSIS_PROGRESS_PERIOD_MISMATCH/);
});

test('Actions CLI writes readable empty index, removes stale partitions and rejects malformed replacement', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analysis-empty-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(root, 'latest.json'), JSON.stringify(emptyManifest('2026Q3')));
  fs.mkdirSync(path.join(root, 'analysis'));
  fs.writeFileSync(path.join(root, 'analysis', 'details-0000.json'), '{}');
  const args = [path.resolve(__dirname, '../scripts/analyze-data.js'), '--data', root];
  const env = { ...process.env, GITHUB_ACTIONS: 'true' };
  const output = spawnSync(process.execPath, args, { env, encoding: 'utf8' });
  assert.equal(output.status, 0, output.stdout + output.stderr);
  assert.deepEqual(JSON.parse(output.stdout), { status: 'awaiting-data', records: 0, stocks: 0, strategyRecords: 0 });
  const indexText = fs.readFileSync(path.join(root, 'analysis.json'), 'utf8');
  const index = ui.validate(JSON.parse(indexText), '2026Q3');
  assert.equal(index.asOf, null); assert.equal(index.retrievedAt, null);
  assert.ok(Number.isFinite(Date.parse(index.generatedAt)));
  assert.deepEqual(fs.readdirSync(path.join(root, 'analysis')), []);
  const malformed = emptyManifest('2026Q3'); delete malformed.asOf;
  fs.writeFileSync(path.join(root, 'latest.json'), JSON.stringify(malformed));
  const rejected = spawnSync(process.execPath, args, { env, encoding: 'utf8' });
  assert.equal(rejected.status, 1);
  assert.equal(JSON.parse(rejected.stdout).errorCode, 'INVALID_ANALYSIS_MANIFEST');
  assert.equal(fs.readFileSync(path.join(root, 'analysis.json'), 'utf8'), indexText);
  assert.equal(fs.readdirSync(root).some(name => name.startsWith('.analysis-')), false);
});

test('browser rejects contradictory awaiting-data indices and presents a valid empty state without detail requests', async () => {
  const index = analyze(emptyManifest('2026Q3'));
  for (const change of [
    x => { x.asOf = '2026-10-26'; }, x => { delete x.retrievedAt; },
    x => { x.priceUpdatedAt = NOW; }, x => { x.coverage.complete = true; },
    x => { x.coverage.inputRecords = 1; }, x => { x.coverage.analyzedRecords = 1; },
    x => { x.themes.push({ name: '合成主题', portfolioCount: 0 }); },
    x => { x.portfolioFiles.push({ path: './analysis/details-portfolios-0000.json', ids: [] }); },
    x => { x.topStockKeys.push('CN:600001'); },
  ]) {
    const invalid = structuredClone(index); change(invalid);
    assert.throws(() => ui.validate(invalid, '2026Q3'), /INVALID_ANALYSIS/);
  }
  const nodes = new Map();
  const node = () => ({ textContent: '', children: [], addEventListener() {}, append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; } });
  const doc = { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }, createElement: node };
  const requests = [];
  const win = { AbortController, setTimeout, clearTimeout, fetch: async url => {
    requests.push(url); return { ok: true, headers: { get: () => null }, text: async () => JSON.stringify(index) };
  } };
  await ui.mount(doc, win).loadQuarter({ id: '2026Q3' });
  assert.deepEqual(requests, ['./data/periods/2026Q3/analysis.json']);
  assert.match(nodes.get('analysis-status').textContent, /等待有效报告数据/);
  assert.doesNotMatch(nodes.get('analysis-status').textContent, /观察截至|已建立分析索引|暂时无法载入/);
  assert.match(nodes.get('analysis-result-count').textContent, /0 条已取得记录/);
  assert.equal(nodes.get('analysis-export').disabled, true);
  assert.equal(nodes.get('analysis-more').hidden, true);
  assert.deepEqual(nodes.get('analysis-results').children, []);
});
