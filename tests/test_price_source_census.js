'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const { execFileSync, spawnSync } = require('node:child_process');
const census = require('../scripts/price-source-census');
const h = require('../lib/price-consistency');
const { expectedSessions, dateHash, calendar } = require('../lib/price-coverage');
const NOW = '2026-10-27T05:00:00Z', FETCHED = '2026-10-26T20:00:00.123456+00:00';
const ROOT = path.join(__dirname, '..');
function price({ low = 10, high = 20, source = 'https://example.org/prices', partial = false, fetchedAt = FETCHED } = {}) {
  const days = expectedSessions('CN', '2026-07-01', '2026-09-30'), observed = partial ? days.slice(0, -1) : days;
  return { ...census.IDENTITY, status: 'observed', low, high, priceBasis: 'unadjusted', coverageVersion: 2,
    sessionScope: calendar.sessionScope, completeness: partial ? 'partial' : 'complete', observedCount: observed.length,
    observedStart: observed[0], observedEnd: observed.at(-1), expectedSessionCount: days.length,
    expectedDateSetHash: dateHash(days), observedDateSetHash: dateHash(observed), missingSessionCount: days.length - observed.length,
    missingSessionDates: days.slice(observed.length), unexpectedSessionCount: 0, unexpectedSessionDates: [],
    rejectedRowCount: 0, conflictingDateCount: 0, sourceUrl: source, sourceUrls: [source], fetchedAt, dataStatus: 'fresh' };
}
function fund(code, ranges = [price()]) {
  return { code, status: 'ok', retrievedAt: '2026-10-26T21:00:00Z', priceUpdatedAt: '2026-10-26T21:00:00.000001Z',
    selectedReport: { periodEnd: '2026-09-30' }, rows: ranges.map(priceRange => ({ market: 'CN', stockCode: '000636', currency: 'CNY', priceRange })) };
}
function repo(t, pages, mutate = () => {}) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'source-census-test-'))), checkout = path.join(tmp, 'data');
  const root = path.join(checkout, 'web/data/periods'), quarter = path.join(root, '2026Q3');
  fs.mkdirSync(path.join(quarter, 'companies'), { recursive: true });
  const base = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: '2026-09-30', baselinePeriodEnd: '2026-06-30' };
  const files = pages.map((funds, i) => { const file = `p${i}.json`; fs.writeFileSync(path.join(quarter, 'companies', file), JSON.stringify({ ...base, funds }));
    return { path: './data/companies/' + file }; });
  fs.writeFileSync(path.join(quarter, 'latest.json'), JSON.stringify({ ...base, companyFiles: files }));
  mutate({ checkout, root, quarter });
  const git = args => execFileSync('git', ['-C', checkout, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  git(['init', '-q']); git(['add', '.']); git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'Synthetic source data']);
  const dataCommit = git(['rev-parse', 'HEAD']).trim();
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  return { root, checkout, quarter, dataCommit, git, run: options => census.scan(root, { dataCommit, now: NOW, ...options }) };
}
const factInput = range => ({ price: range, sourceUrl: range.sourceUrl, sourceUrls: range.sourceUrls, fetchedAt: range.fetchedAt });

test('all parts, complete and inside-range partial facts and exact observations are retained without writes', t => {
  const a = price(), b = price({ low: 9, source: 'https://example.org/other' }), inside = price({ partial: true, low: 11, high: 19 });
  const db = repo(t, [[fund('000001', [a])], [fund('000002', [b, inside]), fund('000003', [a])]]);
  const result = db.run();
  assert.equal(result.complete, true); assert.equal(result.counts.factCount, 3); assert.equal(result.counts.sourceObservationCount, 3);
  assert.equal(result.counts.validOccurrences, 4); assert.equal(result.counts.locationCount, 4);
  assert.equal(result.parts.length, 2); assert.equal(result.counts.scannedCompanyPages, 2); assert.equal(result.quoteRequests, 0);
  assert.equal(result.facts.filter(fact => !fact.complete).length, 1);
  assert.deepEqual(result.rawRegistry.types, ['complete-source-disagreement']); assert.equal(result.rawRegistry.factIds.length, 2);
  assert.equal(result.rawRegistry.digest, h.digest({ key: census.KEY, types: result.rawRegistry.types, factIds: result.rawRegistry.factIds }));
  assert.equal(result.sourceUniverseDigest, h.digest({ key: census.KEY, factIds: result.facts.map(x => x.factId),
    observationIds: result.sourceObservations.map(x => x.observationId) }));
  assert.equal(db.git(['status', '--porcelain']), '');
  assert.deepEqual(db.run(), result);
});

test('retained witnesses survive local missing prices and duplicate ordinary copies retain locations', t => {
  const a = price(), b = price({ low: 9, source: 'https://example.org/other' });
  const witness = h.conflictFor([factInput(a), factInput(b)], census.IDENTITY, NOW);
  const first = fund('000001', [{ status: 'missing', globalPriceConflict: witness }]), second = fund('000002', [a]);
  const result = repo(t, [[first], [second]]).run();
  assert.equal(result.complete, true); assert.equal(result.counts.factCount, 2); assert.equal(result.counts.sourceObservationCount, 2);
  assert.equal(result.counts.witnessOccurrences, 2); assert.equal(result.counts.unobservedTargetRows, 1);
  const copied = result.sourceObservations.find(x => x.sourceUrl === a.sourceUrl);
  assert.equal(copied.occurrenceCount, 2); assert.deepEqual(copied.locations.map(x => x.origin), ['retained-witness', 'ordinary']);
  assert.equal(result.rawRegistry.digest, witness.witnessDigest);
});

test('same fact new original timestamp and equivalent Yahoo URL remain new observations', t => {
  const url = 'https://query1.finance.yahoo.com/v8/finance/chart/000636.SZ?period1=1&period2=2';
  const a = price({ source: url }), b = price({ source: url.replace('query1', 'query2').replace('period2=2', 'period2=3') });
  const c = price({ source: url, fetchedAt: '2026-10-26T20:00:00.123457+00:00' });
  const result = repo(t, [[fund('000001', [a, b, c])]]).run();
  assert.equal(result.complete, true); assert.equal(result.counts.factCount, 1); assert.equal(result.counts.sourceObservationCount, 3);
  assert.equal(result.groups[0].firstFetchedAt, FETCHED); assert.equal(result.groups[0].lastFetchedAt, c.fetchedAt);
  for (const item of result.sourceObservations) assert.equal(item.observationId,
    h.digest(Object.fromEntries(['factId', 'sourceUrl', 'sourceUrls', 'fetchedAt'].map(key => [key, item[key]]))));
});

test('new source and partial extremes change raw digest; narrower partial changes only full census digest', t => {
  const a = price(), b = price({ low: 9, source: 'https://example.org/b' });
  const baseline = repo(t, [[fund('000001', [a, b])]]).run();
  const partial = repo(t, [[fund('000001', [a, b, price({ partial: true, low: 11, high: 19 })])]]).run();
  assert.equal(partial.rawRegistry.digest, baseline.rawRegistry.digest); assert.notEqual(partial.sourceUniverseDigest, baseline.sourceUniverseDigest);
  const third = repo(t, [[fund('000001', [a, b, price({ source: 'https://example.org/c' })])]]).run();
  assert.notEqual(third.rawRegistry.digest, baseline.rawRegistry.digest);
  const outside = repo(t, [[fund('000001', [a, b, price({ partial: true, low: 8 })])]]).run();
  assert.deepEqual(outside.rawRegistry.types, ['complete-source-disagreement', 'partial-extreme']); assert.equal(outside.rawRegistry.factIds.length, 3);
});

test('target wrong proof, binding, clocks, or forged witness makes census partial', t => {
  const changes = [x => { x.rows[0].priceRange.currency = 'USD'; }, x => { x.rows[0].currency = 'USD'; },
    x => { x.rows[0].priceRange.rangeEnd = '2026-06-30'; }, x => { x.rows[0].priceRange.observedDateSetHash = '0'.repeat(64); },
    x => { x.rows[0].priceRange.sourceUrl = 'https://example.org/q?' + 'token=' + 'hidden'; },
    x => { x.rows[0].priceRange.globalPriceConflict = { key: census.KEY, facts: [] }; },
    x => { x.rows[0].priceRange.fetchedAt = '2026-10-26T21:00:00.000002Z'; },
    x => { x.retrievedAt = '2026-10-28T00:00:00Z'; }, x => { x.selectedReport.periodEnd = '2026-06-30'; }];
  for (const change of changes) {
    const row = fund('000001'); change(row); const result = repo(t, [[row]]).run();
    assert.equal(result.complete, false); assert.ok(result.counts.invalidTargetOccurrences > 0); assert.equal(result.resolution, 'unresolved');
  }
});

test('fact, observation, location, row and page limits cannot silently truncate complete census', t => {
  const a = price(), b = price({ low: 9, source: 'https://example.org/b' });
  for (const limits of [{ maxFacts: 1 }, { maxObservations: 1 }, { maxLocations: 1 }, { maxRows: 1 }, { maxPages: 1 }, { maxFunds: 1 }]) {
    const result = repo(t, [[fund('000001', [a]), fund('000002', [b])], [fund('000003')]]).run({ limits });
    assert.equal(result.complete, false, JSON.stringify(limits)); assert.ok(result.reasonCodes.length > 0);
    assert.ok(result.counts.omittedOccurrences > 0 || result.counts.unscannedCompanyPages > 0);
  }
  assert.throws(() => census.scan('/ignored', { limits: { maxFacts: census.LIMITS.maxFacts + 1 } }), /CENSUS_LIMIT_INVALID/);
});

test('dirty checkout, changed commit and outside output fail without disclosing paths or bodies', t => {
  const db = repo(t, [[fund('000001')]]);
  assert.throws(() => census.scan(db.root, { dataCommit: '0'.repeat(40), now: NOW }), /CENSUS_CHECKOUT_INVALID/);
  fs.appendFileSync(path.join(db.quarter, 'companies/p0.json'), ' ');
  assert.throws(() => db.run(), /CENSUS_CHECKOUT_INVALID/);
  const child = spawnSync(process.execPath, [path.join(ROOT, 'scripts/price-source-census.js'), '--data', db.root,
    '--data-commit', db.dataCommit, '--output', path.join(db.checkout, 'bad.json')], { env: { ...process.env, GITHUB_ACTIONS: 'true' }, encoding: 'utf8' });
  assert.equal(child.status, 1); assert.equal(JSON.parse(child.stdout).errorCode, 'CENSUS_OUTPUT_INSIDE_CHECKOUT'); assert.equal(child.stderr, '');
});

test('manifest duplicate/path/schema and Git symlink are incomplete evidence', t => {
  for (const mutation of [value => { value.companyFiles.push(value.companyFiles[0]); }, value => { value.companyFiles[0].path = '../outside.json'; },
    value => { value.targetPeriodEnd = '2026-06-30'; }]) {
    const db = repo(t, [[fund('000001')]], ({ quarter }) => {
      const file = path.join(quarter, 'latest.json'), value = JSON.parse(fs.readFileSync(file)); mutation(value); fs.writeFileSync(file, JSON.stringify(value));
    });
    assert.equal(db.run().complete, false);
  }
  const db = repo(t, [[fund('000001')]], ({ quarter }) => {
    const file = path.join(quarter, 'companies/p0.json'); fs.renameSync(file, path.join(quarter, 'companies/real.json')); fs.symlinkSync('real.json', file);
  });
  assert.equal(db.run().complete, false); assert.ok(db.run().reasonCodes.includes('CENSUS_BLOB_INVALID'));
});

test('complete empty target set does not manufacture coverage or a registry conflict', t => {
  const row = fund('000001'); row.rows = [];
  const result = repo(t, [[row]]).run(); assert.equal(result.complete, false); assert.ok(result.reasonCodes.includes('CENSUS_NO_TARGET_FACTS'));
  assert.equal(result.rawRegistry.digest, null); assert.deepEqual(result.sourceObservations, []);
});

test('CLI refuses outside Actions and emits only bounded fixed failure metadata', t => {
  const db = repo(t, [[fund('000001')]]), output = path.join(path.dirname(db.checkout), 'census.json');
  const child = spawnSync(process.execPath, [path.join(ROOT, 'scripts/price-source-census.js'), '--data', db.root,
    '--data-commit', db.dataCommit, '--output', output], { env: { ...process.env, GITHUB_ACTIONS: 'false' }, encoding: 'utf8' });
  assert.equal(child.status, 1); assert.deepEqual(JSON.parse(child.stdout), { status: 'failed', errorCode: 'CENSUS_REQUIRES_ACTIONS', quoteRequests: 0 });
  assert.equal(fs.existsSync(output), false); assert.equal(child.stderr, '');
});

module.exports = { price, fund, repo, NOW, FETCHED };

test('raw conflict digest exactly matches Python registry with partial extreme and inside-range source', t => {
  const db = repo(t, [[fund('000001', [price(), price({ low: 9, source: 'https://example.org/b' }),
    price({ partial: true, low: 8, source: 'https://example.org/c' }), price({ partial: true, low: 11, high: 19 })])]]);
  const result = db.run(), input = { schemaVersion: 1, kind: 'price-conflict-observations', quarter: '2026Q3',
    targetPeriodEnd: result.targetPeriodEnd, baselinePeriodEnd: result.baselinePeriodEnd, generatedAt: NOW, dataCommit: db.dataCommit,
    manifestContentHash: result.manifest.sha256, records: [{ key: census.KEY, groups: result.groups }],
    scannedCompanyPages: result.counts.scannedCompanyPages, scannedFunds: result.counts.scannedFunds, eligiblePriceKeys: 1 };
  const child = spawnSync('python3', ['-c', 'import sys,json;sys.path.insert(0,"scripts");import price_conflicts as p;v=json.load(sys.stdin);print(json.dumps(p.registry_from_observations(v,p.instant(v["generatedAt"]))["records"][0]))'],
    { cwd: ROOT, input: JSON.stringify(input), encoding: 'utf8' });
  assert.equal(child.status, 0, child.stderr); const expected = JSON.parse(child.stdout);
  assert.equal(result.rawRegistry.digest, expected.digest); assert.deepEqual(result.rawRegistry.factIds, expected.facts.map(x => x.factId));
});

test('more than four distinct observation times are kept without old registry sampling', t => {
  const ranges = Array.from({ length: 6 }, (_, i) => price({ fetchedAt: `2026-10-26T20:00:00.12345${i}+00:00` }));
  const result = repo(t, [[fund('000001', ranges)]]).run();
  assert.equal(result.complete, true); assert.equal(result.groups[0].observations.length, 6);
  assert.equal(result.groups[0].omittedObservationCount, 0); assert.equal(result.sourceObservations.length, 6);
});

test('Git blob hashing catches modified content hidden from ordinary index status', t => {
  const db = repo(t, [[fund('000001')]]), relative = 'web/data/periods/2026Q3/companies/p0.json';
  db.git(['update-index', '--assume-unchanged', relative]); fs.appendFileSync(path.join(db.checkout, relative), ' ');
  assert.equal(db.git(['status', '--porcelain']), '');
  const result = db.run(); assert.equal(result.complete, false); assert.ok(result.reasonCodes.includes('CENSUS_BLOB_INVALID'));
});

test('input byte exhaustion is explicit partial and retains no unsupported complete claim', t => {
  const result = repo(t, [[fund('000001')]]).run({ limits: { maxInputBytes: 1 } });
  assert.equal(result.complete, false); assert.ok(result.reasonCodes.includes('CENSUS_INPUT_LIMIT'));
  assert.equal(result.counts.scannedInputBytes, 0); assert.equal(result.quoteRequests, 0);
});

test('minimal sparse module set runs successful Actions CLI with no production mutation', t => {
  const db = repo(t, [[fund('000001')]]), parent = path.dirname(db.checkout), source = path.join(parent, 'source'), output = path.join(parent, 'result.json');
  for (const relative of ['scripts/price-source-census.js', 'lib/price-consistency.js', 'lib/price-coverage.js', 'config/trading-calendars-2026.json']) {
    fs.mkdirSync(path.dirname(path.join(source, relative)), { recursive: true }); fs.copyFileSync(path.join(ROOT, relative), path.join(source, relative));
  }
  const child = spawnSync(process.execPath, [path.join(source, 'scripts/price-source-census.js'), '--data', db.root,
    '--data-commit', db.dataCommit, '--output', output], { env: { ...process.env, GITHUB_ACTIONS: 'true' }, encoding: 'utf8' });
  assert.equal(child.status, 0, child.stdout); assert.equal(child.stderr, '');
  const result = JSON.parse(fs.readFileSync(output)); assert.equal(result.complete, true); assert.equal(result.quoteRequests, 0);
  assert.equal(result.productionDataModified, false); assert.equal(result.automaticResolution, false); assert.equal(db.git(['status', '--porcelain']), '');
});
