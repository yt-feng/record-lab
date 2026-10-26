'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { priceRange } = require('../lib/fund-analysis');
const { expectedSessions } = require('../lib/price-coverage');
const { scan, scanRegistry, numericDifference, requestFacts, PHASES, MAX_OUTPUT } = require('../scripts/price-conflict-scan');
const NOW = '2026-10-27T02:00:00Z';
const COMMIT = 'a'.repeat(40);
const source = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600001&klt=101&fqt=0&beg=20260401&end=20260630&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61';
function makeFund(code, market, stockCode, quarter, low, high, url = source, kind = 'complete') {
  const phase = PHASES[quarter], start = new Date(Date.parse(phase.baselinePeriodEnd) + 86400000).toISOString().slice(0, 10);
  const holding = { market, stockCode, currency: { CN: 'CNY', HK: 'HKD', US: 'USD' }[market] };
  const dates = expectedSessions(market, start, phase.targetPeriodEnd);
  const price = priceRange(holding, { ...holding, priceBasis: 'unadjusted', requestedStart: start, requestedEnd: phase.targetPeriodEnd,
    sourceUrl: url, sourceUrls: [url], sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
    rows: (kind === 'partial' ? dates.slice(0, 2) : dates).map(date => ({ date, low, high, close: (low + high) / 2 })) }, { start, end: phase.targetPeriodEnd });
  Object.assign(price, { fetchedAt: '2026-10-26T10:00:00Z', dataStatus: 'fresh' });
  return { code, status: 'ok', retrievedAt: '2026-10-26T10:00:00Z', selectedReport: { periodEnd: phase.targetPeriodEnd },
    rows: [{ ...holding, priceRange: price }] };
}
function fixture(t, make = quarter => [makeFund('000001', 'CN', '600001', quarter, 1, 2), makeFund('000002', 'CN', '600001', quarter, .9, 2.1)]) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'price-conflict-fixture-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const quarter of Object.keys(PHASES)) {
    const directory = path.join(root, quarter); fs.mkdirSync(path.join(directory, 'companies'), { recursive: true });
    const funds = make(quarter), phase = PHASES[quarter];
    const manifest = { schemaVersion: 1, sourceMode: 'actions', ...phase, retrievedAt: '2026-10-26T10:00:00Z', priceUpdatedAt: null,
      companyFiles: funds.map((fund, index) => ({ path: `./data/companies/co-${index}.json` })) };
    fs.writeFileSync(path.join(directory, 'latest.json'), JSON.stringify(manifest));
    for (const [index, fund] of funds.entries()) fs.writeFileSync(path.join(directory, 'companies', `co-${index}.json`),
      JSON.stringify({ schemaVersion: 1, sourceMode: 'actions', ...phase, funds: [fund], rawText: 'MUST_NOT_LEAK_RAW_INPUT' }));
    fs.writeFileSync(path.join(directory, 'analysis.json'), JSON.stringify({ schemaVersion: 1, ...phase, generatedAt: NOW,
      stocks: [{ key: 'CN:600001', priceRange: { low: .9, high: 2.1, status: 'observed', completeness: 'complete', conflictingObservations: false, sourceCount: 2 } }] }));
  }
  return root;
}
function treeBytes(root) {
  return Object.fromEntries(fs.readdirSync(root, { recursive: true }).filter(name => fs.statSync(path.join(root, name)).isFile())
    .map(name => [name, fs.readFileSync(path.join(root, name), 'utf8')]));
}
test('strict source conflicts stay unresolved and diagnostics do not rewrite any source byte', t => {
  const root = fixture(t), before = treeBytes(root), result = scan(root, { now: NOW, dataCommit: COMMIT });
  assert.deepEqual(treeBytes(root), before); assert.equal(result.dataCommit, COMMIT);
  assert.equal(result.productionDataModified, false); assert.equal(result.automaticResolution, false);
  assert.ok(Buffer.byteLength(JSON.stringify(result)) < MAX_OUTPUT);
  assert.equal(JSON.stringify(result).includes('MUST_NOT_LEAK_RAW_INPUT'), false);
  for (const quarter of result.quarters) {
    assert.equal(quarter.completeSourceConflictKeys, 1); assert.equal(quarter.selected.length, 1);
    const row = quarter.selected[0];
    assert.equal(row.analysis.completeness, 'complete'); assert.equal(row.analysis.conflictingObservations, false);
    assert.equal(row.resolution, 'unresolved'); assert.equal(row.groups.length, 2);
    assert.ok(row.groups.every(group => group.reusableCompleteSource && group.fullCalendarVerified));
    assert.equal(row.differences[0].exactEqual, false); assert.equal(row.differences[0].low.absoluteDelta, Math.abs(1 - .9));
    assert.equal(row.groups[0].observations[0].quoteFetchedAt, '2026-10-26T10:00:00Z');
  }
});
test('selection is stratified CN HK US and limited to three keys per quarter', t => {
  const root = fixture(t, quarter => {
    const values = [['CN', '600001'], ['CN', '600002'], ['HK', '00700'], ['US', 'AAPL']];
    return values.flatMap(([market, code], index) => [makeFund(String(100000 + index * 2), market, code, quarter, 1, 2),
      makeFund(String(100001 + index * 2), market, code, quarter, .99, 2.01)]);
  });
  for (const quarter of scan(root, { now: NOW }).quarters) {
    assert.equal(quarter.completeSourceConflictKeys, 4); assert.deepEqual(quarter.selected.map(row => row.market), ['CN', 'HK', 'US']);
  }
});
test('partial extremes are separate from conflicting complete sources, invalid calendar and timestamps do not create donors', t => {
  const root = fixture(t, quarter => {
    const invalid = makeFund('000003', 'CN', '600001', quarter, .5, 3);
    invalid.rows[0].priceRange.observedDateSetHash = '0'.repeat(64);
    const future = makeFund('000004', 'CN', '600001', quarter, .4, 4);
    future.retrievedAt = '2026-10-28T12:00:00Z'; future.rows[0].priceRange.fetchedAt = future.retrievedAt;
    return [makeFund('000001', 'CN', '600001', quarter, 1, 2), makeFund('000002', 'CN', '600001', quarter, .9, 2.1, source, 'partial'), invalid, future];
  });
  const result = scan(root, { now: NOW });
  assert.ok(result.quarters.every(quarter => quarter.completeSourceConflictKeys === 0 && quarter.partialExtremeConflictRows === 2 && !quarter.selected.length));
});
test('numeric diagnostics distinguish float representation hints without treating unequal values as equal', () => {
  const hint = numericDifference({ low: 1.23, high: 2.34 }, { low: 1.2300000190734863, high: 2.3399999141693115 });
  assert.equal(hint.exactEqual, false); assert.equal(hint.sameFloat32Representation, true);
  assert.ok(hint.sameAtDecimalPlaces.includes(2)); assert.ok(hint.low.absoluteDelta > 0);
  assert.equal(numericDifference({ low: 1, high: 2 }, { low: .5, high: 1 }).uniformRatioExact, true);
  assert.equal(requestFacts(source).basisSignal, 'explicit-unadjusted-request');
  assert.equal(requestFacts(source.replace('fqt=0', 'fqt=1')).basisSignal, 'adjusted-or-unspecified-request');
  assert.equal(requestFacts('https://localhost/quote').sourceUrl, null);
});
test('group cap preserves at least two distinct complete facts after many identical ranges', t => {
  const root = fixture(t, quarter => [
    ...Array.from({ length: 10 }, (_, index) => makeFund(String(100000 + index), 'CN', '600001', quarter, 1, 2, `https://example.org/quote-${index}`)),
    makeFund('000001', 'CN', '600001', quarter, .9, 2.1, 'https://example.org/last')]);
  for (const row of scan(root, { now: NOW }).quarters.map(quarter => quarter.selected[0])) {
    assert.equal(row.groups.length, 8); assert.ok(row.omittedGroupRows > 0); assert.ok(row.differences.length > 0);
  }
});
test('worst-case sampled source URLs shrink diagnostics while retaining both conflicting complete facts', t => {
  const root = fixture(t, quarter => [['CN', '600001'], ['HK', '00700'], ['US', 'AAPL']].flatMap(([market, code], m) =>
    Array.from({ length: 8 }, (_, i) => makeFund(String(100000 + m * 100 + i), market, code, quarter, 1 - i / 100, 2 + i / 100,
      'https://example.org/' + 'x'.repeat(1700) + i))));
  const result = scan(root, { now: NOW });
  assert.ok(Buffer.byteLength(JSON.stringify(result)) < 48000);
  assert.ok(result.quarters.every(quarter => quarter.selected.every(row => row.differences.length && row.groups.length >= 2)));
});
test('path escapes, missing pages and cross-quarter observations are rejected', t => {
  for (const failure of ['path', 'missing', 'quarter']) {
    const root = fixture(t), file = path.join(root, '2026Q2', 'latest.json'), manifest = JSON.parse(fs.readFileSync(file));
    if (failure === 'path') manifest.companyFiles[0].path = '../private.json';
    if (failure === 'missing') fs.unlinkSync(path.join(root, '2026Q2', 'companies', 'co-0.json'));
    if (failure === 'quarter') manifest.targetPeriodEnd = '2026-09-30';
    fs.writeFileSync(file, JSON.stringify(manifest));
    assert.throws(() => scan(root, { now: NOW }));
  }
});
test('CLI refuses local real scans and forbids output inside input data', t => {
  const root = fixture(t), cli = path.join(__dirname, '..', 'scripts', 'price-conflict-scan.js');
  const local = spawnSync(process.execPath, [cli, '--data', root, '--output', path.join(root, 'result.json')], { encoding: 'utf8', env: { ...process.env, GITHUB_ACTIONS: 'false' } });
  assert.equal(local.status, 1); assert.match(local.stdout, /DIAGNOSTIC_REQUIRES_ACTIONS/);
  const inside = spawnSync(process.execPath, [cli, '--data', root, '--output', path.join(root, 'result.json')], { encoding: 'utf8', env: { ...process.env, GITHUB_ACTIONS: 'true' } });
  assert.equal(inside.status, 1); assert.match(inside.stdout, /DIAGNOSTIC_OUTPUT_INSIDE_SOURCE/);
});
test('full registry keeps complete conflicts and partial extremes across pages without fund bodies', t => {
  const root = fixture(t, quarter => [
    makeFund('000001', 'CN', '600001', quarter, 1, 2),
    makeFund('000002', 'CN', '600001', quarter, .9, 2.1),
    makeFund('000003', 'HK', '00700', quarter, 1, 2),
    makeFund('000004', 'HK', '00700', quarter, .5, 3, source, 'partial'),
    makeFund('000005', 'US', 'AAPL', quarter, 1, 2),
    makeFund('000006', 'US', 'AAPL', quarter, 1.1, 1.9, source, 'partial')]);
  const before = treeBytes(root), result = scanRegistry(root, '2026Q2', { now: NOW, dataCommit: COMMIT });
  assert.deepEqual(treeBytes(root), before); assert.equal(result.records.length, 2);
  assert.deepEqual(result.records.map(row => row.key.split('|')[0]), ['CN', 'HK']);
  assert.ok(result.records.every(row => row.groups.length === 2));
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes('MUST_NOT_LEAK_RAW_INPUT'), false);
  assert.equal(serialized.includes('reportRetrievedAt'), false); assert.equal(serialized.includes('fundCode'), false);
  assert.ok(result.records.every(row => row.groups.every(group => group.observations[0].fetchedAt === '2026-10-26T10:00:00Z')));
});
test('registry deduplicates copied funds and Yahoo retrieval-day changes without dropping original source times', t => {
  const yahoo = day => `https://query1.finance.yahoo.com/v8/finance/chart/600001.SS?period1=1775001600&period2=${day}&interval=1d&events=splits&includeAdjustedClose=false`;
  const root = fixture(t, quarter => [makeFund('000001', 'CN', '600001', quarter, 1, 2),
    makeFund('000002', 'CN', '600001', quarter, .9, 2.1, yahoo(1792972800)),
    makeFund('000003', 'CN', '600001', quarter, .9, 2.1, yahoo(1793059200).replace('query1.', 'query2.')),
    makeFund('000004', 'CN', '600001', quarter, .9, 2.1, yahoo(1792972800))]);
  const row = scanRegistry(root, '2026Q2', { now: NOW }).records[0];
  assert.equal(row.groups.length, 2);
  const group = row.groups.find(group => group.price.low === .9);
  assert.equal(group.occurrenceCount, 3); assert.equal(group.observations.length, 2);
  assert.notEqual(group.observations[0].sourceUrl, group.observations[1].sourceUrl);
});
test('registry excludes invalid future partial quotes and fails explicitly at the per-key fact limit', t => {
  const futureRoot = fixture(t, quarter => {
    const future = makeFund('000003', 'CN', '600001', quarter, .1, 5, source, 'partial');
    future.rows[0].priceRange.fetchedAt = '2026-10-28T12:00:00Z';
    return [makeFund('000001', 'CN', '600001', quarter, 1, 2), future];
  });
  assert.equal(scanRegistry(futureRoot, '2026Q2', { now: NOW }).records.length, 0);
  const root = fixture(t, quarter => Array.from({ length: 33 }, (_, index) =>
    makeFund(String(100000 + index), 'CN', '600001', quarter, 1, 2 + index)));
  assert.throws(() => scanRegistry(root, '2026Q2', { now: NOW }), /DIAGNOSTIC_FACT_LIMIT/);
});
test('registry expands validated retained witnesses even when the local quote or other fund is absent', t => {
  for (const missing of [false, true]) {
    const root = fixture(t, quarter => {
      const a = makeFund('000001', 'CN', '600001', quarter, 1, 2);
      const b = makeFund('000002', 'CN', '600001', quarter, .9, 2.1, 'https://example.org/second');
      b.retrievedAt = b.rows[0].priceRange.fetchedAt = '2026-10-26T11:00:00+00:00';
      require('../lib/reuse-prices').reuseCompletePrices([a, b], { phase: PHASES[quarter], now: NOW, recoverPrice() { assert.fail('no quote reuse'); } });
      if (missing) {
        const price = a.rows[0].priceRange;
        a.rows[0].priceRange = { status: 'missing', rangeStart: price.rangeStart, rangeEnd: price.rangeEnd,
          currency: price.currency, globalPriceConflict: price.globalPriceConflict };
      }
      return [a];
    });
    const before = treeBytes(root), row = scanRegistry(root, '2026Q2', { now: NOW }).records[0];
    assert.deepEqual(treeBytes(root), before); assert.equal(row.groups.length, 2);
    assert.ok(row.groups.every(group => group.occurrenceCount === 1));
    assert.deepEqual(row.groups.map(group => group.observations[0].fetchedAt).sort(), ['2026-10-26T10:00:00Z', '2026-10-26T11:00:00+00:00']);
  }
});
test('tampered retained witness cannot introduce missing source facts into the registry', t => {
  for (const tamper of [marker => { marker.facts[0].price.low *= .5; },
    marker => { marker.key = marker.key.replace('600001', '600002'); },
    marker => { marker.facts[0].fetchedAt = '2026-10-28T11:00:00Z'; }]) {
    const root = fixture(t, quarter => {
      const a = makeFund('000001', 'CN', '600001', quarter, 1, 2), b = makeFund('000002', 'CN', '600001', quarter, .9, 2.1);
      require('../lib/reuse-prices').reuseCompletePrices([a, b], { phase: PHASES[quarter], now: NOW, recoverPrice() { assert.fail(); } });
      tamper(a.rows[0].priceRange.globalPriceConflict); return [a];
    });
    assert.deepEqual(scanRegistry(root, '2026Q2', { now: NOW }).records, []);
  }
});
