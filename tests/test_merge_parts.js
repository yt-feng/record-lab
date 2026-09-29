'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');

const now = '2026-09-29T12:00:00Z';
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const catalog = { fundCount: 3, companyCount: 2,
  companies: [{ code: 'co1', name: '测试公司甲', fundCount: 2 }, { code: 'co2', name: '测试公司乙', fundCount: 1 }],
  funds: [{ code: '000001', name: '合成甲A', companyCode: 'co1', companyName: '测试公司甲' },
    { code: '000002', name: '合成甲C', companyCode: 'co1', companyName: '测试公司甲' },
    { code: '000003', name: '合成乙', companyCode: 'co2', companyName: '测试公司乙' }] };
const report = (code, changes = {}) => ({ fundCode: code, periodEnd: '2026-06-30', publishedAt: '2026-07-20',
  kind: 'quarterly', coverage: 'top10', sourceUrl: 'https://example.org/report.pdf', navYuan: 10000,
  holdings: [{ stockCode: '600001', stockName: '合成股票', market: 'CN', currency: 'CNY', shares: 100,
    weightPct: 5, marketValueYuan: 500 }], ...changes });
function snapshot(codes = ['000001'], { retrievedAt = '2026-09-29T10:00:00Z', overrides = {}, barsByStock = {} } = {}) {
  return buildSnapshot({ asOf: '2026-09-29', retrievedAt, errors: [], barsByStock,
    funds: codes.map(code => ({ code, name: '合成基金', company: '概况公司名称', managers: [{ name: '合成经理' }],
      reports: [report(code)], shareClassCodes: [code], ...overrides })) },
  { now, config: { ...config, funds: codes.map(code => ({ code })) } });
}
const merge = changes => mergeSnapshots({ catalog, config, now, ...changes });

test('publishes company envelopes and a small manifest with catalog coverage', () => {
  const result = merge({ snapshots: [snapshot(['000001', '000003'])] });
  assert.equal(result.changed, true);
  assert.deepEqual(Object.keys(result.companySnapshots), ['co1', 'co2']);
  assert.deepEqual(result.manifest.funds, []);
  assert.equal(result.manifest.coverage.fundCount, 2);
  assert.equal(result.manifest.coverage.catalogFundCount, 3);
  assert.equal(result.manifest.coverage.catalogAvailableFundCount, 2);
  assert.equal(result.manifest.coverage.targetPeriodEnd, '2026-06-30');
  assert.equal(result.manifest.companyFiles[0].path, './data/companies/co1.json');
  assert.deepEqual(result.manifest.companyFiles[0].managers, ['合成经理']);
  assert.equal(result.companySnapshots.co1.funds[0].company, '测试公司甲');
  assert.equal(result.manifest.companyCoverage[0].status, 'partial');
});

test('missing or failed parts preserve old observations and do not refresh the manifest timestamp', () => {
  const first = merge({ snapshots: [snapshot()] });
  const result = merge({ snapshots: [null], previousCompanies: first.companySnapshots, previousManifest: first.manifest });
  assert.equal(result.changed, false);
  assert.equal(result.manifest.retrievedAt, first.manifest.retrievedAt);
  assert.deepEqual(result.companySnapshots, {});
  const empty = merge({ snapshots: [] });
  assert.equal(empty.manifest.retrievedAt, null);
  assert.equal(empty.manifest.asOf, null);
  assert.equal(empty.manifest.coverage.status, 'awaiting-data');
  assert.equal(empty.manifest.coverage.requestedFundCount, 3);
});

test('new company batches retain earlier companies and only replace with newer observations', () => {
  const first = merge({ snapshots: [snapshot(['000001'], { retrievedAt: '2026-09-29T09:00:00Z' })] });
  const second = merge({ snapshots: [snapshot(['000003'])], previousCompanies: first.companySnapshots, previousManifest: first.manifest });
  assert.equal(second.companySnapshots.co1.funds[0].retrievedAt, '2026-09-29T09:00:00Z');
  assert.equal(second.companySnapshots.co2.funds[0].retrievedAt, '2026-09-29T10:00:00Z');
  const older = merge({ snapshots: [snapshot(['000001'], { retrievedAt: '2026-09-29T08:00:00Z' })],
    previousCompanies: second.companySnapshots, previousManifest: second.manifest });
  assert.equal(older.changed, false);
});

test('migrates validated bootstrap funds to company files without creating a new observation time', () => {
  const bootstrap = snapshot(['000001', '000003'], { retrievedAt: '2026-09-29T09:00:00Z' });
  const result = merge({ previousManifest: bootstrap, snapshots: [] });
  assert.equal(result.changed, true);
  assert.equal(result.counts.acceptedFreshFundCount, 0);
  assert.equal(result.counts.migratedBootstrapFundCount, 2);
  assert.equal(result.manifest.coverage.fundCount, 2);
  assert.equal(result.manifest.retrievedAt, bootstrap.retrievedAt);
  assert.equal(result.companySnapshots.co1.funds[0].retrievedAt, bootstrap.retrievedAt);
  assert.deepEqual(result.manifest.funds, []);
  const replay = merge({ previousManifest: result.manifest, previousCompanies: result.companySnapshots, snapshots: [] });
  assert.equal(replay.changed, false);
});

test('a new partial batch preserves other bootstrap companies during migration', () => {
  const bootstrap = snapshot(['000001', '000003'], { retrievedAt: '2026-09-29T09:00:00Z' });
  const result = merge({ previousManifest: bootstrap, snapshots: [snapshot(['000002'])] });
  assert.equal(result.manifest.coverage.fundCount, 3);
  assert.equal(result.companySnapshots.co1.funds.length, 2);
  assert.equal(result.companySnapshots.co2.funds[0].retrievedAt, bootstrap.retrievedAt);
});

test('rebuilds whitelisted data instead of copying arbitrary part payloads', () => {
  const part = snapshot();
  part.debug = 'raw diagnostic';
  part.funds[0].privateMetadata = 'raw diagnostic';
  part.funds[0].rows[0].position.valueYuan = 999999999;
  part.funds[0].rows[0].change.label = 'unverified label';
  const result = merge({ snapshots: [part] });
  const clean = result.companySnapshots.co1.funds[0];
  assert.equal(clean.rows[0].position.valueYuan, 500);
  assert.equal(JSON.stringify(result.companySnapshots).includes('raw diagnostic'), false);
  assert.equal(JSON.stringify(result.companySnapshots).includes('unverified label'), false);
  const invalid = snapshot();
  invalid.funds[0].selectedReport.sourceUrl = 'file:report.pdf';
  invalid.funds[0].reports = [invalid.funds[0].selectedReport];
  assert.equal(merge({ snapshots: [invalid] }).manifest.coverage.fundCount, 0);
});

test('company mapping uses catalog identities and keeps unlisted funds explicitly unknown', () => {
  const result = merge({ snapshots: [snapshot(['999999'])] });
  assert.equal(result.companySnapshots.unknown.funds[0].company, '目录公司未匹配');
  assert.equal(result.manifest.coverage.unmatchedCompanyFundCount, 1);
  assert.equal(result.manifest.coverage.catalogAvailableFundCount, 0);
  const conflict = snapshot(['000001'], { overrides: { sourcePortfolioCode: '000001',
    shareClassCodes: ['000001', '000003'], portfolioSourceUrl: 'https://example.org/classes.pdf' } });
  const rejected = merge({ snapshots: [conflict] });
  assert.equal(rejected.manifest.coverage.fundCount, 0);
  assert.ok(rejected.errors.includes('CATALOG_IDENTITY_MISMATCH'));
});

test('confirmed same-company share classes retain aliases without adding their positions', () => {
  const part = snapshot(['000001', '000002'], { overrides: { sourcePortfolioCode: '000001',
    shareClassCodes: ['000001', '000002'], portfolioSourceUrl: 'https://example.org/classes.pdf' } });
  const result = merge({ snapshots: [part] });
  assert.equal(result.manifest.coverage.fundCount, 1);
  assert.equal(result.manifest.coverage.catalogAvailableFundCount, 2);
  assert.equal(result.companySnapshots.co1.funds[0].summary.knownPositionYuan, 500);
  assert.deepEqual(result.companySnapshots.co1.funds[0].shareClassCodes, ['000001', '000002']);
});

test('no-equities is a valid company observation and is counted separately', () => {
  const part = snapshot(['000001'], { overrides: { reports: [], noEquitiesEvidence: {
    confirmed: true, sourceUrl: 'https://example.org/empty-report.pdf', periodEnd: '2026-06-30',
    publishedAt: '2026-07-20', page: 6, text: '本基金本报告期末未持有股票。', kind: 'quarterly',
  } } });
  const result = merge({ snapshots: [part] });
  assert.equal(result.companySnapshots.co1.funds[0].status, 'no-equities');
  assert.equal(result.manifest.coverage.noEquitiesFundCount, 1);
  assert.equal(result.manifest.coverage.holdingCount, 0);
});

test('pending company directories retain unknown fund totals instead of a false zero', () => {
  const incomplete = { fundCount: 100, companyCount: 1, funds: [],
    companies: [{ code: 'co1', name: '测试公司甲', fundCount: null, directoryStatus: 'pending' }] };
  const result = merge({ catalog: incomplete, snapshots: [] });
  assert.equal(result.manifest.companyCoverage[0].catalogFundCount, null);
  assert.equal(result.manifest.companyCoverage[0].missingFundCount, null);
  assert.equal(result.manifest.companyCoverage[0].status, 'directory-incomplete');
});

test('valid same-window prices survive a new observation with no fresh market data', () => {
  const prices = { 'CN:600001': { currency: 'CNY', priceBasis: 'unadjusted', sourceUrl: 'https://example.org/prices',
    rows: [{ date: '2026-04-10', low: 8, high: 12 }] } };
  const first = merge({ snapshots: [snapshot(['000001'], { retrievedAt: '2026-09-29T09:00:00Z', barsByStock: prices })] });
  const next = merge({ snapshots: [snapshot()], previousCompanies: first.companySnapshots, previousManifest: first.manifest });
  const price = next.companySnapshots.co1.funds[0].rows[0].priceRange;
  assert.equal(price.low, 8);
  assert.equal(price.dataStatus, 'cached');
  assert.equal(price.fetchedAt, '2026-09-29T09:00:00Z');
});

test('CLI merges snapshot files atomically and leaves bytes unchanged on a later empty batch', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'record-parts-test-'));
  try {
    const parts = path.join(temporary, 'parts');
    const data = path.join(temporary, 'data');
    fs.mkdirSync(path.join(parts, 'part0'), { recursive: true });
    fs.mkdirSync(data);
    const today = new Date().toISOString();
    const item = snapshot();
    item.asOf = today.slice(0, 10); item.retrievedAt = today;
    item.funds[0].asOf = today.slice(0, 10); item.funds[0].retrievedAt = today;
    fs.writeFileSync(path.join(parts, 'part0', 'snapshot.json'), JSON.stringify(item));
    const catalogFile = path.join(data, 'catalog.json');
    const configFile = path.join(temporary, 'config.json');
    fs.writeFileSync(catalogFile, JSON.stringify(catalog));
    fs.writeFileSync(configFile, JSON.stringify(config));
    const args = [path.join(__dirname, '..', 'scripts', 'merge-parts.js'), '--parts', parts, '--data', data,
      '--catalog', catalogFile, '--config', configFile];
    const first = spawnSync(process.execPath, args, { encoding: 'utf8' });
    assert.equal(first.status, 0);
    const before = fs.readFileSync(path.join(data, 'latest.json'), 'utf8');
    const companyBefore = fs.readFileSync(path.join(data, 'companies', 'co1.json'), 'utf8');
    fs.rmSync(path.join(parts, 'part0', 'snapshot.json'));
    const second = spawnSync(process.execPath, args, { encoding: 'utf8' });
    assert.equal(second.status, 0);
    assert.equal(JSON.parse(second.stdout).status, 'unchanged');
    assert.equal(second.stderr, '');
    assert.equal(fs.readFileSync(path.join(data, 'latest.json'), 'utf8'), before);
    assert.equal(fs.readFileSync(path.join(data, 'companies', 'co1.json'), 'utf8'), companyBefore);
    assert.equal(fs.readdirSync(data).some(name => name.includes('.tmp-')), false);
  } finally { fs.rmSync(temporary, { recursive: true, force: true }); }
});
