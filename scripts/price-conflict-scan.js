#!/usr/bin/env node
'use strict';
// Read-only diagnostic. Never updates normalized prices, progress or analysis.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createPriceIndex, addPriceSources } = require('../lib/reuse-prices');
const { validCompletePrice, calendar } = require('../lib/price-coverage');
const { publicUrl } = require('./build-data');
const PHASES = { '2026Q2': { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' },
  '2026Q3': { targetPeriodEnd: '2026-09-30', baselinePeriodEnd: '2026-06-30' } };
const MAX_OUTPUT = 65536, MAX_KEYS = 30000, MAX_GROUPS = 8;
const MAX_REGISTRY_BYTES = 24_000_000, MAX_CONFLICT_KEYS = 5000, MAX_FACTS = 32;
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const stamp = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && Number.isFinite(Date.parse(value)) ? value : null;
const fail = code => { throw new Error(code); };
function readJson(file, maxBytes = 25_000_000) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maxBytes) fail('DIAGNOSTIC_INPUT_INVALID');
  const bytes = fs.readFileSync(file);
  return { value: JSON.parse(bytes), hash: sha(bytes), bytes: bytes.length };
}
function identity(row, phase) {
  const start = new Date(Date.parse(phase.baselinePeriodEnd) + 86400000).toISOString().slice(0, 10);
  const code = row?.stockCode, market = row?.market;
  const pattern = market === 'US' ? /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/ : market === 'CN' ? /^\d{6}$/ : market === 'HK' ? /^\d{5}$/ : null;
  return pattern?.test(code) && row.currency === calendar.markets[market].currency
    && row.priceRange?.rangeStart === start && row.priceRange?.rangeEnd === phase.targetPeriodEnd
    && row.priceRange.currency === row.currency ? [market, code, row.currency, 'unadjusted', start, phase.targetPeriodEnd, calendar.id].join('|') : null;
}
function requestFacts(sourceUrl) {
  if (!publicUrl(sourceUrl) || sourceUrl.length > 2048) return { provider: 'unrecognized', basisSignal: 'unverified', sourceUrl: null };
  const url = new URL(sourceUrl), q = url.searchParams;
  if (url.hostname === 'push2his.eastmoney.com' && url.pathname === '/api/qt/stock/kline/get')
    return { provider: 'eastmoney', sourceUrl, basisSignal: q.get('fqt') === '0' ? 'explicit-unadjusted-request' : 'adjusted-or-unspecified-request', adjustmentParameter: q.get('fqt') };
  if (['query1.finance.yahoo.com', 'query2.finance.yahoo.com'].includes(url.hostname) && url.pathname.startsWith('/v8/finance/chart/'))
    return { provider: 'yahoo', sourceUrl, basisSignal: q.get('includeAdjustedClose') === 'false' && q.get('events')?.includes('splits')
      ? 'quote-array-with-requested-split-check' : 'corporate-action-check-unverified' };
  if (url.hostname === 'web.ifzq.gtimg.cn' && url.pathname === '/appstock/app/fqkline/get') {
    const mode = (q.get('param') || '').split(',').at(-1);
    return { provider: 'tencent', sourceUrl, basisSignal: mode === '' ? 'explicit-unadjusted-request' : 'adjusted-or-unspecified-request', adjustmentParameter: mode };
  }
  return { provider: 'unrecognized', sourceUrl, basisSignal: 'unverified' };
}
function numericDifference(a, b) {
  const difference = (x, y) => ({ first: x, second: y, absoluteDelta: Math.abs(x - y),
    relativeDelta: Math.abs(x - y) / Math.max(Math.abs(x), Math.abs(y)), ratio: x / y });
  const sameAtDecimals = [];
  for (let digits = 2; digits <= 6; digits++) if (Number(a.low.toFixed(digits)) === Number(b.low.toFixed(digits))
    && Number(a.high.toFixed(digits)) === Number(b.high.toFixed(digits))) sameAtDecimals.push(digits);
  return { low: difference(a.low, b.low), high: difference(a.high, b.high), exactEqual: a.low === b.low && a.high === b.high,
    sameFloat32Representation: Math.fround(a.low) === Math.fround(b.low) && Math.fround(a.high) === Math.fround(b.high),
    sameAtDecimalPlaces: sameAtDecimals, uniformRatioExact: a.low / b.low === a.high / b.high,
    interpretation: 'Representation and scaling hints only; no tolerance, correction or preferred source is applied.' };
}
function selectedKeys(index) {
  const conflicted = [...index.entries].filter(([, entry]) => entry.conflict).map(([key]) => key).sort();
  const selected = [];
  for (const market of ['CN', 'HK', 'US']) {
    const key = conflicted.find(value => value.startsWith(market + '|'));
    if (key) selected.push(key);
  }
  for (const key of conflicted) if (selected.length < 3 && !selected.includes(key)) selected.push(key);
  return selected;
}
function scanQuarter(root, quarter, now) {
  const phase = PHASES[quarter], directory = path.join(root, quarter);
  if (fs.lstatSync(directory).isSymbolicLink() || fs.lstatSync(path.join(directory, 'companies')).isSymbolicLink()) fail('DIAGNOSTIC_INPUT_INVALID');
  const input = readJson(path.join(directory, 'latest.json'));
  const manifest = input.value;
  if (manifest?.schemaVersion !== 1 || manifest.sourceMode !== 'actions' || manifest.targetPeriodEnd !== phase.targetPeriodEnd
    || manifest.baselinePeriodEnd !== phase.baselinePeriodEnd || !Array.isArray(manifest.companyFiles) || manifest.companyFiles.length > 10000) fail('DIAGNOSTIC_MANIFEST_INVALID');
  const seen = new Set();
  const files = manifest.companyFiles.map(item => {
    if (!/^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/.test(item?.path || '') || seen.has(item.path)) fail('DIAGNOSTIC_COMPANY_PATH_INVALID');
    seen.add(item.path); return { relative: item.path, file: path.join(directory, item.path.slice('./data/'.length)) };
  });
  const load = item => {
    const document = readJson(item.file).value;
    if (document?.schemaVersion !== 1 || document.sourceMode !== 'actions' || document.targetPeriodEnd !== phase.targetPeriodEnd
      || document.baselinePeriodEnd !== phase.baselinePeriodEnd || !Array.isArray(document.funds) || document.funds.length > 1000) fail('DIAGNOSTIC_COMPANY_INVALID');
    return document.funds;
  };
  const index = createPriceIndex(phase, now); let fundCount = 0;
  for (const item of files) {
    const funds = load(item); fundCount += funds.length; addPriceSources(index, funds);
    if (index.entries.size > MAX_KEYS) fail('DIAGNOSTIC_KEY_LIMIT');
  }
  const selected = new Map(selectedKeys(index).map(key => [key, { key: key.split('|').slice(0, 2).join(':'), market: key.split('|')[0],
    stockCode: key.split('|')[1], currency: key.split('|')[2], rangeStart: index.interval.start, rangeEnd: index.interval.end,
    calendarId: calendar.id, observedRowCount: 0, completeSourceRowCount: 0, groups: [], omittedGroupRows: 0 }]));
  let partialExtremeConflictRows = 0;
  for (const item of files) for (const fund of load(item)) for (const row of Array.isArray(fund?.rows) ? fund.rows : []) {
    if (!['ok', 'no-equities'].includes(fund?.status) || fund.selectedReport?.periodEnd !== phase.targetPeriodEnd
      || !stamp(fund.retrievedAt) || Date.parse(fund.retrievedAt) > Date.parse(now) + 300000
      || fund.priceUpdatedAt != null && (!stamp(fund.priceUpdatedAt) || Date.parse(fund.priceUpdatedAt) > Date.parse(now) + 300000)) continue;
    const key = identity(row, phase), entry = key && index.entries.get(key), price = row.priceRange;
    if (!key || price.status !== 'observed' || !Number.isFinite(price.low) || !Number.isFinite(price.high) || price.low <= 0 || price.high < price.low) continue;
    const complete = validCompletePrice(price, row.market, row.stockCode, index.interval.start, index.interval.end);
    if (entry && !entry.conflict && !complete && price.priceBasis === 'unadjusted'
      && (price.low < entry.source.price.low || price.high > entry.source.price.high)) partialExtremeConflictRows++;
    const selectedRow = selected.get(key); if (!selectedRow) continue;
    selectedRow.observedRowCount++;
    const candidateIndex = createPriceIndex(phase, now);
    addPriceSources(candidateIndex, [{ ...fund, rows: [row] }]);
    const reusable = candidateIndex.entries.has(key);
    selectedRow.completeSourceRowCount += Number(reusable);
    const facts = requestFacts(price.sourceUrl);
    if (!facts.sourceUrl) { selectedRow.omittedGroupRows++; continue; }
    const signature = JSON.stringify([price.low, price.high, facts.sourceUrl, complete, reusable]);
    let group = selectedRow.groups.find(value => value.signature === signature);
    if (!group && reusable && selectedRow.groups.length >= MAX_GROUPS) {
      let replace = selectedRow.groups.findIndex(value => !value.reusableCompleteSource);
      if (replace < 0 && !selectedRow.groups.some(value => value.low === price.low && value.high === price.high))
        replace = selectedRow.groups.findIndex((value, index, groups) => groups.some((other, position) => position !== index && other.low === value.low && other.high === value.high));
      if (replace >= 0) selectedRow.omittedGroupRows += selectedRow.groups.splice(replace, 1)[0].rowCount;
    }
    if (!group && selectedRow.groups.length < MAX_GROUPS) {
      group = { signature, low: price.low, high: price.high, currency: price.currency, priceBasis: price.priceBasis,
        ...facts, fullCalendarVerified: complete, reusableCompleteSource: reusable, observedCount: price.observedCount,
        expectedSessionCount: price.expectedSessionCount, observedDateSetHash: price.observedDateSetHash,
        expectedDateSetHash: price.expectedDateSetHash, coverageVersion: price.coverageVersion,
        observedStart: price.observedStart, observedEnd: price.observedEnd, missingSessionCount: price.missingSessionCount,
        rowCount: 0, firstFetchedAt: null, lastFetchedAt: null, observations: [] };
      selectedRow.groups.push(group);
    }
    if (!group) { selectedRow.omittedGroupRows++; continue; }
    group.rowCount++;
    if (Number.isFinite(Date.parse(price.fetchedAt))) {
      if (!group.firstFetchedAt || Date.parse(price.fetchedAt) < Date.parse(group.firstFetchedAt)) group.firstFetchedAt = price.fetchedAt;
      if (!group.lastFetchedAt || Date.parse(price.fetchedAt) > Date.parse(group.lastFetchedAt)) group.lastFetchedAt = price.fetchedAt;
    }
    if (group.observations.length < 2 && /^\d{6}$/.test(fund.code)) group.observations.push({ fundCode: fund.code,
      companyFile: item.relative, reportRetrievedAt: stamp(fund.retrievedAt), priceUpdatedAt: stamp(fund.priceUpdatedAt),
      quoteFetchedAt: stamp(price.fetchedAt), dataStatus: ['fresh', 'cached'].includes(price.dataStatus) ? price.dataStatus : null });
  }
  let analysis;
  try {
    const value = readJson(path.join(directory, 'analysis.json'));
    if (value.value.targetPeriodEnd !== phase.targetPeriodEnd || value.value.baselinePeriodEnd !== phase.baselinePeriodEnd
      || !Array.isArray(value.value.stocks)) fail('DIAGNOSTIC_ANALYSIS_INVALID');
    analysis = { contentHash: value.hash, generatedAt: stamp(value.value.generatedAt), retrievedAt: stamp(value.value.retrievedAt),
      priceUpdatedAt: stamp(value.value.priceUpdatedAt), stocks: new Map(value.value.stocks.map(stock => [stock.key, stock.priceRange])) };
  } catch (error) { if (error.code === 'ENOENT') analysis = null; else throw error; }
  for (const row of selected.values()) {
    row.groups.forEach(group => { delete group.signature; });
    row.groups.sort((a, b) => Number(b.reusableCompleteSource) - Number(a.reusableCompleteSource) || a.sourceUrl.localeCompare(b.sourceUrl));
    const completeGroups = row.groups.filter(group => group.reusableCompleteSource);
    const pairs = [];
    for (let i = 0; i < completeGroups.length; i++) for (let j = i + 1; j < completeGroups.length && pairs.length < 4; j++) {
      const a = completeGroups[i], b = completeGroups[j];
      if (a.low !== b.low || a.high !== b.high) pairs.push({ firstGroup: row.groups.indexOf(a), secondGroup: row.groups.indexOf(b), ...numericDifference(a, b) });
    }
    row.differences = pairs;
    const value = analysis?.stocks.get(row.key);
    row.analysis = value ? { status: value.status, completeness: value.completeness, low: value.low, high: value.high,
      conflictingObservations: value.conflictingObservations, sourceCount: value.sourceCount } : null;
    row.resolution = 'unresolved';
  }
  return { quarter, ...phase, manifestContentHash: input.hash, manifestRetrievedAt: stamp(manifest.retrievedAt),
    manifestPriceUpdatedAt: stamp(manifest.priceUpdatedAt),
    analysis: analysis ? { contentHash: analysis.contentHash, generatedAt: analysis.generatedAt, retrievedAt: analysis.retrievedAt, priceUpdatedAt: analysis.priceUpdatedAt } : null,
    scannedCompanyPages: files.length, scannedFunds: fundCount, eligiblePriceKeys: index.entries.size,
    completeSourceConflictKeys: [...index.entries.values()].filter(entry => entry.conflict).length,
    partialExtremeConflictRows, selected: [...selected.values()] };
}
function scan(root, { now = new Date(), dataCommit = null } = {}) {
  if (dataCommit !== null && !/^[a-f0-9]{40}$/.test(dataCommit)) fail('DIAGNOSTIC_COMMIT_INVALID');
  const result = { schemaVersion: 1, kind: 'price-conflict-diagnostic', generatedAt: new Date(now).toISOString(), dataCommit,
    productionDataModified: false, automaticResolution: false, maxKeysPerQuarter: 3, maxRequeriesPerKey: 2,
    scope: 'Published company observations only; sources removed by earlier portfolio deduplication cannot be reconstructed here.',
    selection: 'One conflicting key per CN/HK/US market where available; unused slots filled deterministically.',
    quarters: Object.keys(PHASES).map(quarter => scanQuarter(root, quarter, now)) };
  // Leave space for at most twelve small requery summaries. Sampling metadata
  // may shrink, but preserve two distinct complete facts for every sampled key.
  for (const quarter of result.quarters) for (const row of quarter.selected) {
    if (Buffer.byteLength(JSON.stringify(result)) <= 48000) break;
    const pair = row.differences[0];
    if (!pair) continue;
    const essential = [row.groups[pair.firstGroup], row.groups[pair.secondGroup]];
    row.omittedGroupRows += row.groups.filter(group => !essential.includes(group)).reduce((n, group) => n + group.rowCount, 0);
    row.groups = essential;
    row.groups.forEach(group => { group.observations = group.observations.slice(0, 1); });
    row.differences = [{ firstGroup: 0, secondGroup: 1, ...numericDifference(...essential) }];
  }
  if (Buffer.byteLength(JSON.stringify(result)) + 1 > 48000) fail('DIAGNOSTIC_OUTPUT_LIMIT');
  return result;
}
// Full conflict registry input for the independent, bounded recheck lane. This
// retains price facts only; fund documents and PDF text never enter the output.
const PRICE_FIELDS = ['status', 'low', 'high', 'market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd',
  'coverageVersion', 'calendarId', 'sessionScope', 'completeness', 'observedCount', 'observedStart', 'observedEnd',
  'expectedSessionCount', 'expectedDateSetHash', 'observedDateSetHash', 'missingSessionCount', 'missingSessionDates',
  'unexpectedSessionCount', 'unexpectedSessionDates', 'rejectedRowCount', 'conflictingDateCount'];
function stableSource(source) {
  const url = new URL(source);
  if (['query1.finance.yahoo.com', 'query2.finance.yahoo.com'].includes(url.hostname)) {
    url.hostname = 'query1.finance.yahoo.com'; url.searchParams.delete('period2');
  }
  url.searchParams.sort(); return url.toString();
}
function scanRegistry(root, quarter, { now = new Date(), dataCommit = null, cacheValidation = true, onProgress = null } = {}) {
  if (!Object.hasOwn(PHASES, quarter) || !Number.isFinite(Date.parse(now))
    || dataCommit !== null && !/^[a-f0-9]{40}$/.test(dataCommit)) fail('DIAGNOSTIC_ARGUMENTS_INVALID');
  const phase = PHASES[quarter], directory = path.join(root, quarter);
  if (fs.lstatSync(directory).isSymbolicLink() || fs.lstatSync(path.join(directory, 'companies')).isSymbolicLink()) fail('DIAGNOSTIC_INPUT_INVALID');
  const input = readJson(path.join(directory, 'latest.json')), manifest = input.value;
  if (manifest?.schemaVersion !== 1 || !['actions', 'awaiting-data'].includes(manifest.sourceMode)
    || manifest.targetPeriodEnd !== phase.targetPeriodEnd || manifest.baselinePeriodEnd !== phase.baselinePeriodEnd
    || !Array.isArray(manifest.companyFiles) || manifest.companyFiles.length > 10000
    || manifest.sourceMode === 'awaiting-data' && (manifest.companyFiles.length || manifest.asOf !== null || manifest.retrievedAt !== null)) fail('DIAGNOSTIC_MANIFEST_INVALID');
  const seen = new Set(), files = manifest.companyFiles.map(item => {
    if (!/^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/.test(item?.path || '') || seen.has(item.path)) fail('DIAGNOSTIC_COMPANY_PATH_INVALID');
    seen.add(item.path); return path.join(directory, item.path.slice('./data/'.length));
  });
  const load = file => {
    const document = readJson(file).value;
    if (document?.schemaVersion !== 1 || document.sourceMode !== 'actions' || document.targetPeriodEnd !== phase.targetPeriodEnd
      || document.baselinePeriodEnd !== phase.baselinePeriodEnd || !Array.isArray(document.funds) || document.funds.length > 1000) fail('DIAGNOSTIC_COMPANY_INVALID');
    return document.funds;
  };
  const index = createPriceIndex(phase, now, { cacheValidation }); let scannedFunds = 0;
  const progress = (phase, completedCompanyPages) => {
    if (typeof onProgress === 'function') onProgress({ phase, completedCompanyPages, totalCompanyPages: files.length, validation: index.validation.stats() });
  };
  progress('index', 0);
  for (const [position, file] of files.entries()) {
    const funds = load(file); scannedFunds += funds.length; addPriceSources(index, funds);
    if (index.entries.size > MAX_KEYS) fail('DIAGNOSTIC_KEY_LIMIT');
    if ((position + 1) % 25 === 0 || position + 1 === files.length) progress('index', position + 1);
  }
  const keys = new Set([...index.entries].filter(([, value]) => value.conflict).map(([key]) => key));
  const eligibleRows = function* (file) {
    for (const fund of load(file)) {
      if (!/^\d{6}$/.test(fund?.code || '') || !['ok', 'no-equities'].includes(fund.status)
        || fund.selectedReport?.periodEnd !== phase.targetPeriodEnd || !Array.isArray(fund.rows)
        || !stamp(fund.retrievedAt) || Date.parse(fund.retrievedAt) > Date.parse(now) + 300000
        || fund.priceUpdatedAt != null && (!stamp(fund.priceUpdatedAt) || Date.parse(fund.priceUpdatedAt) > Date.parse(now) + 300000)) continue;
      const bound = Math.max(Date.parse(fund.retrievedAt), stamp(fund.priceUpdatedAt) ? Date.parse(fund.priceUpdatedAt) : -Infinity);
      for (const row of fund.rows) {
        const key = identity(row, phase), price = row.priceRange;
        if (!key || !index.entries.has(key)) continue;
        const candidates = [];
        if (price.status === 'observed' && price.priceBasis === 'unadjusted'
          && Number.isFinite(price.low) && Number.isFinite(price.high) && price.low > 0 && price.high >= price.low
          && publicUrl(price.sourceUrl) && price.sourceUrl.length <= 2048 && stamp(price.fetchedAt)
          && Date.parse(price.fetchedAt) >= Date.parse(phase.targetPeriodEnd) && Date.parse(price.fetchedAt) <= bound) candidates.push(row);
        // A retained two-source witness can outlive either local quote after
        // portfolio deduplication. Validate its full identity, hashes, calendar
        // proof and original timestamps before allowing it to supply facts.
        const witness = index.validation.normalizeConflict(price.globalPriceConflict, { market: row.market, stockCode: row.stockCode,
          currency: row.currency, rangeStart: index.interval.start, rangeEnd: index.interval.end, calendarId: calendar.id });
        if (witness) for (const fact of witness.facts) candidates.push({ market: row.market, stockCode: row.stockCode, currency: row.currency,
          priceRange: { ...fact.price, sourceUrl: fact.sourceUrl, sourceUrls: fact.sourceUrls, fetchedAt: fact.fetchedAt } });
        const emitted = new Set();
        for (const candidate of candidates) {
          const value = candidate.priceRange;
          const signature = JSON.stringify([PRICE_FIELDS.map(field => value[field]), value.sourceUrl,
            [...(value.sourceUrls || [value.sourceUrl])].sort(), value.fetchedAt]);
          if (emitted.has(signature)) continue;
          emitted.add(signature); yield [key, candidate];
        }
      }
    }
  };
  for (const [position, file] of files.entries()) {
   for (const [key, row] of eligibleRows(file)) {
    const source = index.entries.get(key).source.price, price = row.priceRange;
    if (!validCompletePrice(price, row.market, row.stockCode, index.interval.start, index.interval.end)
      && (price.low < source.low || price.high > source.high)) keys.add(key);
    if (keys.size > MAX_CONFLICT_KEYS) fail('DIAGNOSTIC_CONFLICT_KEY_LIMIT');
   }
   if ((position + 1) % 25 === 0 || position + 1 === files.length) progress('extremes', position + 1);
  }
  if (keys.size > MAX_CONFLICT_KEYS) fail('DIAGNOSTIC_CONFLICT_KEY_LIMIT');
  const facts = new Map([...keys].sort().map(key => [key, new Map()])); let retainedBytes = 0;
  for (const [position, file] of files.entries()) {
   for (const [key, row] of eligibleRows(file)) {
    if (!facts.has(key)) continue;
    const price = Object.fromEntries(PRICE_FIELDS.filter(field => row.priceRange[field] !== undefined).map(field => [field, row.priceRange[field]]));
    const sourceUrl = row.priceRange.sourceUrl, fetchedAt = row.priceRange.fetchedAt;
    const sourceUrls = row.priceRange.sourceUrls === undefined ? [sourceUrl] : row.priceRange.sourceUrls;
    if (!Array.isArray(sourceUrls) || !sourceUrls.length || sourceUrls.length > 8
      || sourceUrls.some(url => !publicUrl(url) || url.length > 2048)) fail('DIAGNOSTIC_SOURCE_INVALID');
    const signature = JSON.stringify([[...new Set([sourceUrl, ...sourceUrls].map(stableSource))].sort(), price]), groups = facts.get(key);
    let group = groups.get(signature);
    if (!group) {
      if (groups.size >= MAX_FACTS) fail('DIAGNOSTIC_FACT_LIMIT');
      group = { price, observations: [], occurrenceCount: 0, omittedObservationCount: 0, firstFetchedAt: fetchedAt, lastFetchedAt: fetchedAt };
      groups.set(signature, group); retainedBytes += Buffer.byteLength(JSON.stringify(group));
    }
    const oldBytes = Buffer.byteLength(JSON.stringify(group)); group.occurrenceCount++;
    if (Date.parse(fetchedAt) < Date.parse(group.firstFetchedAt)) group.firstFetchedAt = fetchedAt;
    if (Date.parse(fetchedAt) > Date.parse(group.lastFetchedAt)) group.lastFetchedAt = fetchedAt;
    if (!group.observations.some(value => value.sourceUrl === sourceUrl && value.fetchedAt === fetchedAt
      && JSON.stringify(value.sourceUrls) === JSON.stringify(sourceUrls))) {
      group.observations.push({ sourceUrl, sourceUrls, fetchedAt });
      group.observations.sort((a, b) => Date.parse(a.fetchedAt) - Date.parse(b.fetchedAt) || a.sourceUrl.localeCompare(b.sourceUrl));
      if (group.observations.length > 4) { group.observations.splice(1, 1); group.omittedObservationCount++; }
    }
    retainedBytes += Buffer.byteLength(JSON.stringify(group)) - oldBytes;
    if (retainedBytes > MAX_REGISTRY_BYTES - 500000) fail('DIAGNOSTIC_REGISTRY_LIMIT');
   }
   if ((position + 1) % 25 === 0 || position + 1 === files.length) progress('facts', position + 1);
  }
  const result = { schemaVersion: 1, kind: 'price-conflict-observations', quarter, ...phase,
    generatedAt: new Date(now).toISOString(), dataCommit, manifestContentHash: input.hash,
    scannedCompanyPages: files.length, scannedFunds, eligiblePriceKeys: index.entries.size,
    productionDataModified: false, automaticResolution: false,
    records: [...facts].map(([key, groups]) => ({ key, groups: [...groups.values()] })) };
  if (Buffer.byteLength(JSON.stringify(result)) > MAX_REGISTRY_BYTES) fail('DIAGNOSTIC_REGISTRY_LIMIT');
  return result;
}
function main(argv = process.argv.slice(2)) {
  try {
    if (process.env.GITHUB_ACTIONS !== 'true') fail('DIAGNOSTIC_REQUIRES_ACTIONS');
    const registryMode = argv.length === 6 && argv[4] === '--registry-quarter' && Object.hasOwn(PHASES, argv[5]);
    if ((!registryMode && argv.length !== 4) || argv[0] !== '--data' || argv[2] !== '--output') fail('DIAGNOSTIC_ARGUMENTS_INVALID');
    const relative = path.relative(path.resolve(argv[1]), path.resolve(argv[3]));
    if (!relative.startsWith('..' + path.sep) && !path.isAbsolute(relative)) fail('DIAGNOSTIC_OUTPUT_INSIDE_SOURCE');
    const options = { dataCommit: process.env.PRICE_DIAGNOSTIC_DATA_COMMIT || null,
      onProgress: value => process.stderr.write(JSON.stringify({ kind: 'price-conflict-scan-progress', ...value }) + '\n') };
    const result = registryMode ? scanRegistry(argv[1], argv[5], options) : scan(argv[1], options);
    fs.mkdirSync(path.dirname(argv[3]), { recursive: true }); fs.writeFileSync(argv[3], JSON.stringify(result) + '\n');
    process.stdout.write(JSON.stringify({ status: 'diagnosed', selectedKeys: registryMode ? result.records.length : result.quarters.reduce((n, quarter) => n + quarter.selected.length, 0) }) + '\n');
    return 0;
  } catch (error) {
    const code = /^DIAGNOSTIC_[A-Z_]+$/.test(error?.message) ? error.message : 'DIAGNOSTIC_FAILED';
    process.stdout.write(JSON.stringify({ status: 'failed', errorCode: code }) + '\n'); return 1;
  }
}
if (require.main === module) process.exitCode = main();
module.exports = { scan, scanQuarter, scanRegistry, numericDifference, requestFacts, main, PHASES, MAX_OUTPUT, MAX_REGISTRY_BYTES };
