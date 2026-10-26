'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeFiles, mergeSnapshots } = require('../scripts/merge-parts');
const { bindingFor } = require('../lib/price-patches');
const { expectedSessions } = require('../lib/price-coverage');
const now = '2026-10-26T12:00:00Z';
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const catalog = { fundCount: 4, companyCount: 2,
  companies: [{ code: 'co1', name: '合成甲', fundCount: 2 }, { code: 'co2', name: '合成乙', fundCount: 1 }],
  funds: [{ code: '000001', companyCode: 'co1', companyName: '合成甲' }, { code: '000002', companyCode: 'co1', companyName: '合成甲' },
    { code: '000003', companyCode: 'co2', companyName: '合成乙' }, { code: '000004' }] };
const config = { ...phase, funds: catalog.funds };
const holding = { market: 'CN', stockCode: '600001', stockName: '合成证券', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 };
const clone = value => structuredClone(value);
function bars(kind = 'complete', low = 10) {
  return kind === 'missing' ? {} : { 'CN:600001': { market: 'CN', stockCode: '600001', currency: 'CNY', priceBasis: 'unadjusted',
    requestedStart: '2026-04-01', requestedEnd: phase.targetPeriodEnd, sourceUrl: 'https://example.org/prices',
    sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: (kind === 'partial' ? ['2026-04-01'] : expectedSessions('CN', '2026-04-01', phase.targetPeriodEnd))
      .map(date => ({ date, low, high: 20, close: 15 })) } };
}
function snapshot(code, { at = '2026-10-26T10:00:00Z', kind = 'missing', low = 10, overrides = {} } = {}) {
  const reports = [['2026-06-30', '2026-07-20'], ['2026-03-31', '2026-04-20']].map(([periodEnd, publishedAt]) => ({
    periodEnd, publishedAt, kind: 'quarterly', coverage: 'top10', navYuan: 10000, holdings: [holding],
    sourceUrl: `https://example.org/${code}-${periodEnd}.pdf` }));
  return buildSnapshot({ asOf: at.slice(0, 10), retrievedAt: at, funds: [{ code, name: '合成基金', reports, ...overrides }],
    errors: [], barsByStock: bars(kind, low) }, { config: { ...config, funds: [{ code }] }, catalog, now });
}
function setup(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'merge-storage-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const parts = path.join(root, 'parts'), data = path.join(root, 'data');
  fs.mkdirSync(parts); fs.mkdirSync(data);
  return { root, parts, data };
}
const json = file => JSON.parse(fs.readFileSync(file, 'utf8'));
function input(state, snapshots) {
  fs.rmSync(state.parts, { recursive: true, force: true }); fs.mkdirSync(state.parts);
  snapshots.forEach((value, index) => fs.writeFileSync(path.join(state.parts, `part-${String(index).padStart(3, '0')}.json`), JSON.stringify(value)));
}
function saved(state, manifest) {
  const result = {};
  for (const file of manifest.companyFiles || []) result[path.basename(file.path, '.json')] = json(path.join(state.data, 'companies', path.basename(file.path)));
  return result;
}
const funds = snapshots => Object.values(snapshots).flatMap(value => value.funds).sort((a, b) => a.code.localeCompare(b.code));
function run(state, options = {}) {
  const file = path.join(state.data, 'latest.json');
  return mergeFiles({ ...state, config, catalog, now, previousManifest: fs.existsSync(file) ? json(file) : null, ...options });
}
function equivalent(state, snapshots, options = {}) {
  const file = path.join(state.data, 'latest.json'), previousManifest = fs.existsSync(file) ? json(file) : null;
  const previousCompanies = previousManifest ? saved(state, previousManifest) : {};
  const expected = mergeSnapshots({ config, catalog, now, previousManifest, previousCompanies, snapshots, ...options });
  input(state, snapshots);
  const result = run(state, options);
  assert.equal(result.changed, expected.changed);
  assert.deepEqual(result.manifest.coverage, expected.manifest.coverage);
  assert.deepEqual(result.manifest.companyCoverage, expected.manifest.companyCoverage);
  assert.deepEqual(result.receipt, expected.receipt);
  assert.deepEqual(result.counts, expected.counts);
  assert.deepEqual([...result.errors].sort(), [...expected.errors].sort());
  if (expected.changed) assert.deepEqual(funds(saved(state, result.manifest)), funds(expected.companySnapshots));
  return result;
}

test('disk pipeline matches ordinary merging across companies, unknown mapping and repeated observations', t => {
  const state = setup(t);
  equivalent(state, [snapshot('000001'), snapshot('000003'), snapshot('000004')]);
  equivalent(state, [snapshot('000001', { at: '2026-10-26T11:00:00Z', kind: 'partial' }), snapshot('000002')]);
  const before = fs.readFileSync(path.join(state.data, 'latest.json'));
  equivalent(state, []);
  assert.deepEqual(fs.readFileSync(path.join(state.data, 'latest.json')), before);
});

test('split alias evidence is co-located while conflicting company identities remain rejected', t => {
  const state = setup(t), identity = { sourcePortfolioCode: '000001', portfolioSourceUrl: 'https://example.org/identity', shareClassCodes: ['000001', '000002'] };
  const result = equivalent(state, [snapshot('000001', { overrides: identity }), snapshot('000002', { overrides: identity })]);
  assert.equal(result.manifest.coverage.fundCount, 1);
  assert.equal(result.manifest.coverage.catalogAvailableFundCount, 2);
  const conflict = { ...identity, shareClassCodes: ['000001', '000003'] };
  const next = equivalent(state, [snapshot('000003', { overrides: conflict })]);
  assert.ok(next.errors.includes('CATALOG_IDENTITY_MISMATCH'));
});

test('overlapping claims from different parts preserve global portfolio conflict detection', t => {
  const state = setup(t);
  const source = code => ({ sourcePortfolioCode: code, portfolioSourceUrl: 'https://example.org/identity', shareClassCodes: ['000001', '000002'] });
  const result = equivalent(state, [snapshot('000001', { overrides: source('000001') }), snapshot('000002', { overrides: source('000002') })]);
  assert.ok(result.errors.includes('PORTFOLIO_IDENTITY_CONFLICT'));
});

test('global prices are sealed across components before reuse; a late conflicting donor blocks every recipient', t => {
  const state = setup(t);
  const first = equivalent(state, [snapshot('000004'), snapshot('000001', { kind: 'complete' }), snapshot('000003')]);
  assert.equal(first.counts.reusedPriceFundCount, 2); assert.equal(first.counts.eligiblePriceKeyCount, 1);
  const old = funds(saved(state, first.manifest));
  for (const fund of old) assert.equal(fund.retrievedAt, '2026-10-26T10:00:00Z');
  const second = setup(t);
  const result = equivalent(second, [snapshot('000004'), snapshot('000001', { kind: 'complete' }), snapshot('000003', { kind: 'complete', low: 9 })]);
  assert.equal(result.counts.reusedPriceFundCount, 0); assert.equal(result.counts.conflictingPriceKeyCount, 1);
  assert.equal(funds(saved(second, result.manifest)).find(fund => fund.code === '000004').rows[0].priceRange.status, 'missing');
  const partial = setup(t);
  const conflict = equivalent(partial, [snapshot('000003', { kind: 'partial', low: 9 }), snapshot('000001', { kind: 'complete' })]);
  assert.equal(conflict.counts.partialConflictRowCount, 1); assert.equal(conflict.counts.reusedPriceFundCount, 0);
});

test('price-only patches retain report timestamps, budget receipts and exact bindings through disk storage', t => {
  const state = setup(t), first = equivalent(state, [snapshot('000001')]);
  const original = funds(saved(state, first.manifest))[0], binding = bindingFor(original, config);
  const batch = { ...config, funds: [{ code: '000001', companyCode: 'co1', priceOnly: binding, priceTargets: [{ market: 'CN', stockCode: '600001' }] }],
    traversal: { plannedAt: '2026-10-26T10:30:00Z' } };
  const patch = buildSnapshot({ asOf: '2026-10-26', retrievedAt: '2026-10-26T11:00:00Z', funds: [], errors: [], barsByStock: bars(),
    pricePatches: [{ code: '000001', binding, attemptedStockKeys: ['CN:600001'], attemptedAt: '2026-10-26T10:40:00Z' }] }, { config: batch, now });
  const result = equivalent(state, [patch]);
  const actual = funds(saved(state, result.manifest))[0];
  assert.equal(actual.retrievedAt, original.retrievedAt); assert.deepEqual(actual.selectedReport, original.selectedReport);
  assert.equal(actual.priceUpdatedAt, '2026-10-26T11:00:00Z');
  assert.equal(result.counts.acceptedFreshFundCount, 0); assert.equal(result.counts.acceptedPricePatchCount, 1);
  assert.equal(result.receipt.funds[0].attempted, undefined);
  equivalent(state, [patch]);
  const invalid = clone(patch); invalid.pricePatches[0].binding.selectedReport.sourceUrl = 'https://example.org/other.pdf';
  const refused = equivalent(state, [invalid]); assert.equal(refused.receipt.pricePatches[0].accepted, false);
  const foreign = clone(patch); foreign.targetPeriodEnd = '2026-09-30'; equivalent(state, [foreign]);
});

test('unstarted work and source-only receipts remain unstarted without changing observations', t => {
  const state = setup(t); equivalent(state, [snapshot('000001')]);
  const deferred = buildSnapshot({ asOf: '2026-10-26', retrievedAt: '2026-10-26T11:00:00Z', errors: [],
    funds: [{ code: '000001', reports: [], collectionAttempt: { status: 'deferred-budget', attempted: false, errorCode: 'BATCH_DEADLINE_REACHED' } }] }, { config, now });
  const result = equivalent(state, [deferred]);
  assert.equal(result.changed, false); assert.equal(result.receipt.funds[0].attempted, false);
  const empty = setup(t), failed = equivalent(empty, [deferred]);
  assert.equal(failed.manifest.coverage.fundCount, 0); assert.equal(failed.manifest.retrievedAt, null);
  assert.equal(failed.receipt.funds[0].attempted, false);
});

test('a failed staged publication or oversized component leaves every prior referenced byte intact', t => {
  const state = setup(t); equivalent(state, [snapshot('000001')]);
  const original = new Map([['latest.json', fs.readFileSync(path.join(state.data, 'latest.json'))]]);
  for (const file of fs.readdirSync(path.join(state.data, 'companies'))) original.set('companies/' + file, fs.readFileSync(path.join(state.data, 'companies', file)));
  input(state, [snapshot('000003')]);
  let staging;
  assert.throws(() => run(state, { beforeCommit: ({ workspace }) => { staging = workspace; throw new Error('injected publication failure'); } }), /injected publication failure/);
  assert.equal(fs.existsSync(staging), false);
  assert.equal(path.relative(state.data, staging).startsWith('..'), true);
  for (const [file, bytes] of original) assert.deepEqual(fs.readFileSync(path.join(state.data, file)), bytes);
  assert.equal(fs.readdirSync(state.root).some(file => file.startsWith('.merge-stage-')), false);
  assert.throws(() => run(state, { componentLimit: 1024 }), /MERGE_COMPONENT_TOO_LARGE/);
  for (const [file, bytes] of original) assert.deepEqual(fs.readFileSync(path.join(state.data, file)), bytes);
  assert.equal(fs.readdirSync(path.join(state.data, 'companies')).length, original.size - 1);
});

test('missing or symbolic prior manifest pages cannot be silently dropped during a fresh merge', t => {
  for (const kind of ['missing', 'symlink']) {
    const state = setup(t), first = equivalent(state, [snapshot('000001')]);
    const before = fs.readFileSync(path.join(state.data, 'latest.json'));
    const file = path.join(state.data, 'companies', path.basename(first.manifest.companyFiles[0].path));
    const copy = path.join(state.root, 'saved.json'); fs.copyFileSync(file, copy); fs.unlinkSync(file);
    if (kind === 'symlink') fs.symlinkSync(copy, file);
    input(state, [snapshot('000003')]);
    assert.throws(() => run(state), /MERGE_READ_FAILED/);
    assert.deepEqual(fs.readFileSync(path.join(state.data, 'latest.json')), before);
  }
});

test('company pages stay bounded and private input fields never enter public output', t => {
  const state = setup(t), items = ['000001', '000002', '000003', '000004'].map(code => snapshot(code));
  items.forEach(item => { item.funds[0].rawPdfText = 'discard-this-full-document'.repeat(1000); item.debug = 'discard-this-debug'; });
  const result = equivalent(state, items, { pageLimits: { maxFunds: 1, maxBytes: 100000 } });
  assert.equal(result.manifest.companyFiles.length, 4);
  for (const file of result.manifest.companyFiles) {
    const text = fs.readFileSync(path.join(state.data, 'companies', path.basename(file.path)), 'utf8');
    assert.ok(Buffer.byteLength(text) <= 100000); assert.equal(text.includes('discard-this'), false);
    assert.equal(json(path.join(state.data, 'companies', path.basename(file.path))).funds.length, 1);
  }
});

test('bootstrap observations migrate by component with the same original times and receipt aliases', t => {
  const state = setup(t), bootstrap = snapshot('000001');
  bootstrap.funds.push(...snapshot('000003').funds);
  fs.writeFileSync(path.join(state.data, 'latest.json'), JSON.stringify(bootstrap));
  const result = equivalent(state, [snapshot('000002')], { receiptCodes: ['000001', '000003'] });
  assert.equal(result.counts.migratedBootstrapFundCount, 2); assert.equal(result.manifest.coverage.fundCount, 3);
  assert.ok(funds(saved(state, result.manifest)).every(fund => fund.retrievedAt === '2026-10-26T10:00:00Z'));
});

test('metadata-only inception conflicts and their later resolution retain the old report observation', t => {
  const state = setup(t);
  const sourceUrl = 'https://www.csrc.gov.cn/csrc/c101900/c1029655/1029655/files/公募基金产品索引（截至20260630）.xlsx';
  const evidence = { schemaVersion: 1, fundCode: '000003', periodEnd: '2026-06-30', date: '2026-04-10',
    sourceId: 'csrc-fund-products-20260630', sourceUrl,
    contentHash: '0f6ac234b792bb6334c0c94ec37fa6c0548363c80017c946d11a5c7e95d3a9e0',
    sheet: 'Sheet1', row: 3, retrievedAt: '2026-10-26T00:00:00Z' };
  const historical = { inceptionDate: evidence.date, inceptionSourceUrl: sourceUrl, inceptionEvidence: evidence };
  const first = equivalent(state, [snapshot('000003', { at: '2026-10-26T00:00:00Z', overrides: historical })]);
  const original = funds(saved(state, first.manifest))[0];
  const profileEvidence = { schemaVersion: 1, fundCode: '000003', date: '2026-07-10',
    sourceUrl: 'https://fundf10.eastmoney.com/jbgk_000003.html', retrievedAt: '2026-10-26T01:00:00Z' };
  const inceptionConflict = { schemaVersion: 1, fundCode: '000003', periodEnd: '2026-06-30', status: 'conflicting-sources',
    historicalEvidence: evidence, profileEvidence };
  const failed = snapshot('000003', { at: '2026-10-26T02:00:00Z', overrides: { reports: [], inceptionConflict, inceptionProfileEvidence: profileEvidence } });
  assert.equal(failed.funds.length, 0); assert.equal(failed.inceptionPatches.length, 1);
  const conflict = equivalent(state, [failed]), altered = funds(saved(state, conflict.manifest))[0];
  assert.deepEqual(altered.inceptionConflict, inceptionConflict);
  assert.deepEqual(altered.selectedReport, original.selectedReport); assert.deepEqual(altered.rows, original.rows);
  assert.equal(altered.retrievedAt, original.retrievedAt); assert.equal(conflict.counts.acceptedFreshFundCount, 0);
  assert.equal(conflict.counts.acceptedInceptionPatchCount, 1);
  const consistent = snapshot('000003', { at: '2026-10-26T04:00:00Z', overrides: { ...historical, reports: [],
    inceptionProfileEvidence: { ...profileEvidence, date: evidence.date, retrievedAt: '2026-10-26T03:00:00Z' } } });
  const resolved = equivalent(state, [consistent]), final = funds(saved(state, resolved.manifest))[0];
  assert.equal(final.inceptionConflict, null); assert.deepEqual(final.inceptionEvidence, evidence);
  assert.equal(final.retrievedAt, original.retrievedAt);
});

test('bounded components process many inputs without retaining their combined payload; related oversize records abort', t => {
  const state = setup(t), codes = Array.from({ length: 12 }, (_, index) => String(100000 + index));
  const largeCatalog = { funds: codes.map(code => ({ code, companyCode: 'co1', companyName: '合成甲' })),
    companies: [{ code: 'co1', name: '合成甲', fundCount: codes.length }] };
  const items = codes.map(code => { const item = snapshot(code); item.funds[0].rawPdfText = 'x'.repeat(250000); return item; });
  input(state, items);
  // Total synthetic input is > 3 MB, but no connected component exceeds 300 KB.
  const result = run(state, { catalog: largeCatalog, componentLimit: 300000 });
  assert.equal(result.manifest.coverage.fundCount, codes.length);
  assert.ok(fs.readdirSync(path.join(state.data, 'companies')).every(name => fs.statSync(path.join(state.data, 'companies', name)).size < 300000));
  const before = fs.readFileSync(path.join(state.data, 'latest.json'));
  for (const item of items.slice(0, 2)) Object.assign(item.funds[0], { sourcePortfolioCode: codes[0],
    portfolioSourceUrl: 'https://example.org/identity', shareClassCodes: codes.slice(0, 2) });
  input(state, items.slice(0, 2));
  assert.throws(() => run(state, { catalog: largeCatalog, componentLimit: 300000 }), /MERGE_COMPONENT_TOO_LARGE/);
  assert.deepEqual(fs.readFileSync(path.join(state.data, 'latest.json')), before);
});
