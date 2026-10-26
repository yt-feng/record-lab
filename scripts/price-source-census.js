#!/usr/bin/env node
'use strict';
// Independent single-key evidence census. No network or production writes.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { canonical, digest, sourceIdentity, normalizeFact, normalizeConflict } = require('../lib/price-consistency');
const { calendar, validCompletePrice } = require('../lib/price-coverage');
const QUARTER = '2026Q3';
const IDENTITY = Object.freeze({ market: 'CN', stockCode: '000636', currency: 'CNY', rangeStart: '2026-07-01',
  rangeEnd: '2026-09-30', calendarId: 'exchange-sessions-2026-q2-q3-v1' });
const KEY = [IDENTITY.market, IDENTITY.stockCode, IDENTITY.currency, 'unadjusted', IDENTITY.rangeStart, IDENTITY.rangeEnd, IDENTITY.calendarId].join('|');
const PREFIX = 'web/data/periods/2026Q3/';
const LIMITS = Object.freeze({ maxOutputBytes: 8 * 1024 * 1024, maxInputBytes: 2 * 1024 * 1024 * 1024,
  maxPartBytes: 25 * 1024 * 1024, maxManifestBytes: 4 * 1024 * 1024, maxPages: 10000, maxFunds: 100000,
  maxFundsPerPart: 1000, maxRows: 5000000, maxRowsPerFund: 30000, maxFacts: 128, maxObservations: 4096,
  maxLocations: 20000, maxSeconds: 600 });
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const fail = code => { throw new Error(code); };
const code = value => typeof value === 'string' && /^[0-9]{6}$/.test(value) && value !== '000000';
function instant(value) {
  if (typeof value !== 'string') return null;
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!m || !Number.isFinite(Date.parse(m[1])) || new Date(m[1]).toISOString().slice(0, 10) !== m[1]
    || +m[2] > 23 || +m[3] > 59 || +m[4] > 59 || m[6] !== 'Z' && (+m[6].slice(1, 3) > 23 || +m[6].slice(4) > 59)) return null;
  return BigInt(Date.parse(`${m[1]}T${m[2]}:${m[3]}:${m[4]}${m[6]}`)) * 1000n + BigInt((m[5] || '').padEnd(6, '0'));
}
function textOrder(a, b) {
  const aa = Array.from(a, x => x.codePointAt(0)), bb = Array.from(b, x => x.codePointAt(0));
  for (let i = 0; i < Math.min(aa.length, bb.length); i++) if (aa[i] !== bb[i]) return aa[i] - bb[i];
  return aa.length - bb.length;
}
function limited(value = {}) {
  if (!object(value) || Object.keys(value).some(key => !Object.hasOwn(LIMITS, key))) fail('CENSUS_LIMIT_INVALID');
  const result = { ...LIMITS, ...value };
  if (Object.entries(result).some(([key, n]) => !Number.isSafeInteger(n) || n < 1 || n > LIMITS[key])) fail('CENSUS_LIMIT_INVALID');
  return result;
}
function git(repo, args) {
  return execFileSync('git', ['--no-optional-locks', '-C', repo, ...args], { encoding: 'utf8', timeout: 30000,
    maxBuffer: 8 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });
}
function cleanCheckout(repo, commit) {
  if (git(repo, ['rev-parse', '--show-toplevel']).trim() !== repo || git(repo, ['rev-parse', 'HEAD']).trim() !== commit
    || git(repo, ['status', '--porcelain=v1', '--untracked-files=all']).trim()) fail('CENSUS_CHECKOUT_INVALID');
}
function noLinks(repo, relative) {
  let current = repo;
  if (fs.lstatSync(current).isSymbolicLink()) fail('CENSUS_PATH_INVALID');
  for (const [i, part] of relative.split('/').entries()) {
    current = path.join(current, part); const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink() || i < relative.split('/').length - 1 && !stat.isDirectory()) fail('CENSUS_PATH_INVALID');
  }
  return current;
}
function treeEntries(repo, commit) {
  const rows = git(repo, ['ls-tree', '-r', '-z', '--long', commit, '--', PREFIX + 'latest.json', PREFIX + 'companies/']).split('\0').filter(Boolean);
  const result = new Map();
  for (const row of rows) {
    const match = /^(\d{6}) (blob|tree) ([a-f0-9]{40}) +([0-9]+)\t(.+)$/.exec(row);
    if (!match || result.has(match[5])) fail('CENSUS_TREE_INVALID');
    result.set(match[5], { mode: match[1], gitBlobSha: match[3], bytes: Number(match[4]) });
  }
  return result;
}
function rawRegistry(facts) {
  const complete = facts.filter(fact => fact.complete), partial = facts.filter(fact => !fact.complete);
  const outliers = complete.length ? partial.filter(fact => fact.price.low < Math.min(...complete.map(x => x.price.low))
    || fact.price.high > Math.max(...complete.map(x => x.price.high))) : [];
  const types = [];
  if (new Set(complete.map(fact => canonical([fact.price.low, fact.price.high]))).size > 1) types.push('complete-source-disagreement');
  if (outliers.length) types.push('partial-extreme');
  const factIds = [...complete, ...outliers].map(fact => fact.factId).sort();
  return { key: KEY, types, factIds, digest: types.length ? digest({ key: KEY, types, factIds }) : null };
}
function scan(root, { dataCommit, now = new Date().toISOString(), limits: override = {} } = {}) {
  const limits = limited(override), nowAt = instant(now), started = process.hrtime.bigint();
  if (!/^[a-f0-9]{40}$/.test(dataCommit || '') || nowAt === null || calendar.id !== IDENTITY.calendarId) fail('CENSUS_ARGUMENT_INVALID');
  root = path.resolve(root); const repo = path.resolve(root, '../../..');
  if (root !== path.join(repo, 'web/data/periods')) fail('CENSUS_PATH_INVALID');
  cleanCheckout(repo, dataCommit);
  const tree = treeEntries(repo, dataCommit), reasons = new Set(), facts = new Map(), observations = new Map();
  const counts = { declaredCompanyPages: 0, scannedCompanyPages: 0, scannedFunds: 0, scannedRows: 0, targetRows: 0,
    unobservedTargetRows: 0, ordinaryOccurrences: 0, witnessOccurrences: 0, validOccurrences: 0,
    retainedOccurrences: 0, invalidTargetOccurrences: 0, omittedOccurrences: 0, invalidDocuments: 0,
    scannedInputBytes: 0, factCount: 0, sourceObservationCount: 0, locationCount: 0, unscannedCompanyPages: 0 };
  const result = { schemaVersion: 1, kind: 'price-source-census', quarter: QUARTER, key: KEY,
    targetPeriodEnd: IDENTITY.rangeEnd, baselinePeriodEnd: '2026-06-30', generatedAt: now, dataCommit,
    productionDataModified: false, automaticResolution: false, quoteRequests: 0, resolution: 'unresolved',
    scope: 'All target-key observations in this commit referenced company parts, including retained witnesses; no claim about discarded unretained sources.',
    limits, manifest: null, parts: [], counts };
  let retainedBytes = 0;
  const issue = (reason, invalid = false) => { reasons.add(reason); if (invalid) counts.invalidTargetOccurrences++; };
  function timedOut() { return process.hrtime.bigint() - started > BigInt(limits.maxSeconds) * 1000000000n; }
  function load(relative, maximum) {
    if (timedOut()) fail('CENSUS_TIME_LIMIT');
    const entry = tree.get(relative);
    if (!entry || entry.mode !== '100644' || entry.bytes > maximum) fail(entry?.bytes > maximum ? 'CENSUS_INPUT_LIMIT' : 'CENSUS_BLOB_INVALID');
    if (counts.scannedInputBytes + entry.bytes > limits.maxInputBytes) fail('CENSUS_INPUT_LIMIT');
    const file = noLinks(repo, relative), stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.size !== entry.bytes) fail('CENSUS_BLOB_INVALID');
    const bytes = fs.readFileSync(file), blob = crypto.createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (blob !== entry.gitBlobSha) fail('CENSUS_BLOB_INVALID');
    counts.scannedInputBytes += bytes.length;
    const evidence = { path: relative, gitBlobSha: blob, sha256: sha(bytes), bytes: bytes.length };
    return { value: JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)), evidence };
  }
  function add(raw, location) {
    const clean = normalizeFact(raw, IDENTITY, now);
    if (!clean) { issue('CENSUS_TARGET_FACT_INVALID', true); return; }
    counts.validOccurrences++;
    const entry = { factId: clean.factId, price: clean.price,
      sourceIdentity: [...new Set([clean.sourceUrl, ...clean.sourceUrls].map(sourceIdentity))].sort(textOrder),
      complete: validCompletePrice(clean.price, IDENTITY.market, IDENTITY.stockCode, IDENTITY.rangeStart, IDENTITY.rangeEnd) };
    const observation = { factId: clean.factId, sourceUrl: clean.sourceUrl, sourceUrls: clean.sourceUrls, fetchedAt: clean.fetchedAt };
    const observationId = digest(observation), old = observations.get(observationId), isNewFact = !facts.has(clean.factId);
    if (facts.has(clean.factId) && canonical(facts.get(clean.factId)) !== canonical(entry)) { issue('CENSUS_FACT_COLLISION', true); return; }
    const extra = (isNewFact ? Buffer.byteLength(canonical(entry)) : 0) + (old ? 0 : Buffer.byteLength(canonical(observation)) * 2 + 256)
      + Buffer.byteLength(canonical(location)) + 32;
    if (isNewFact && facts.size >= limits.maxFacts || !old && observations.size >= limits.maxObservations
      || counts.locationCount >= limits.maxLocations || retainedBytes + extra > limits.maxOutputBytes - 1500000) {
      issue('CENSUS_EVIDENCE_LIMIT'); counts.omittedOccurrences++; return;
    }
    if (isNewFact) facts.set(clean.factId, entry);
    if (!old) observations.set(observationId, { observationId, ...observation, occurrenceCount: 0, locations: [] });
    const item = observations.get(observationId); item.occurrenceCount++; item.locations.push(location);
    counts.retainedOccurrences++; counts.locationCount++; retainedBytes += extra;
  }
  try {
    const input = load(PREFIX + 'latest.json', limits.maxManifestBytes), manifest = input.value;
    result.manifest = input.evidence;
    if (manifest?.schemaVersion !== 1 || manifest.sourceMode !== 'actions' || manifest.targetPeriodEnd !== IDENTITY.rangeEnd
      || manifest.baselinePeriodEnd !== '2026-06-30' || !Array.isArray(manifest.companyFiles)) fail('CENSUS_MANIFEST_INVALID');
    counts.declaredCompanyPages = manifest.companyFiles.length;
    if (manifest.companyFiles.length > limits.maxPages) fail('CENSUS_PAGE_LIMIT');
    const paths = new Set(), files = [];
    for (const item of manifest.companyFiles) {
      if (typeof item?.path !== 'string' || !/^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/.test(item.path)
        || paths.has(item.path)) fail('CENSUS_COMPANY_PATH_INVALID');
      paths.add(item.path); files.push(PREFIX + item.path.slice('./data/'.length));
    }
    files.sort();
    for (const relative of files) {
      if (timedOut()) { issue('CENSUS_TIME_LIMIT'); break; }
      let document;
      try { const input = load(relative, limits.maxPartBytes); document = input.value; result.parts.push(input.evidence); }
      catch (error) { issue(/^CENSUS_[A-Z_]+$/.test(error.message) ? error.message : 'CENSUS_DOCUMENT_INVALID'); counts.invalidDocuments++; continue; }
      if (document?.schemaVersion !== 1 || document.sourceMode !== 'actions' || document.targetPeriodEnd !== IDENTITY.rangeEnd
        || document.baselinePeriodEnd !== '2026-06-30' || !Array.isArray(document.funds) || document.funds.length > limits.maxFundsPerPart) {
        issue('CENSUS_DOCUMENT_INVALID'); counts.invalidDocuments++; continue;
      }
      let pageComplete = true;
      for (const [fundIndex, fund] of document.funds.entries()) {
        if (timedOut() || counts.scannedFunds >= limits.maxFunds) { issue(timedOut() ? 'CENSUS_TIME_LIMIT' : 'CENSUS_FUND_LIMIT'); pageComplete = false; break; }
        counts.scannedFunds++;
        if (!object(fund) || !Array.isArray(fund.rows) || fund.rows.length > limits.maxRowsPerFund) {
          issue('CENSUS_FUND_INVALID'); counts.invalidDocuments++; pageComplete = false; continue;
        }
        const observed = instant(fund.retrievedAt), updated = fund.priceUpdatedAt == null ? null : instant(fund.priceUpdatedAt);
        const fundValid = code(fund.code) && ['ok', 'no-equities'].includes(fund.status) && fund.selectedReport?.periodEnd === IDENTITY.rangeEnd
          && observed !== null && observed <= nowAt + 300000000n
          && (fund.priceUpdatedAt == null || updated !== null && updated <= nowAt + 300000000n);
        const bound = observed === null ? null : updated !== null && updated > observed ? updated : observed;
        for (const [rowIndex, row] of fund.rows.entries()) {
          if (counts.scannedRows >= limits.maxRows || timedOut()) { issue(timedOut() ? 'CENSUS_TIME_LIMIT' : 'CENSUS_ROW_LIMIT'); pageComplete = false; break; }
          counts.scannedRows++;
          if (!object(row)) { issue('CENSUS_ROW_INVALID'); counts.invalidDocuments++; pageComplete = false; continue; }
          const price = row.priceRange, marker = price?.globalPriceConflict;
          const target = row.market === IDENTITY.market && row.stockCode === IDENTITY.stockCode
            || price?.market === IDENTITY.market && price?.stockCode === IDENTITY.stockCode || marker?.key === KEY;
          if (!target) continue;
          counts.targetRows++;
          if (!fundValid || row.market !== IDENTITY.market || row.stockCode !== IDENTITY.stockCode || row.currency !== IDENTITY.currency || !object(price)) {
            issue('CENSUS_TARGET_BINDING_INVALID', true); continue;
          }
          const base = { partPath: relative, fundCode: fund.code, fundIndex, rowIndex,
            reportRetrievedAt: fund.retrievedAt, priceUpdatedAt: fund.priceUpdatedAt ?? null };
          if (price.status === 'observed') {
            counts.ordinaryOccurrences++;
            const fetched = instant(price.fetchedAt);
            if (fetched === null || bound === null || fetched > bound) issue('CENSUS_TARGET_TIME_INVALID', true);
            else add({ price, sourceUrl: price.sourceUrl, sourceUrls: price.sourceUrls === undefined ? [price.sourceUrl] : price.sourceUrls,
              fetchedAt: price.fetchedAt }, { ...base, origin: 'ordinary', witnessDigest: null });
          } else if (price.status === 'missing') counts.unobservedTargetRows++;
          else issue('CENSUS_TARGET_FACT_INVALID', true);
          if (Object.hasOwn(price, 'globalPriceConflict')) {
            const witness = normalizeConflict(marker, IDENTITY, now);
            if (!witness) issue('CENSUS_TARGET_WITNESS_INVALID', true);
            else for (const fact of witness.facts) {
              counts.witnessOccurrences++;
              add(fact, { ...base, origin: 'retained-witness', witnessDigest: witness.witnessDigest });
            }
          }
        }
        if (reasons.has('CENSUS_ROW_LIMIT') || timedOut()) break;
      }
      if (pageComplete) counts.scannedCompanyPages++;
      if (counts.scannedRows >= limits.maxRows || counts.scannedFunds >= limits.maxFunds || timedOut()) break;
    }
  } catch (error) { issue(/^CENSUS_[A-Z_]+$/.test(error.message) ? error.message : 'CENSUS_DOCUMENT_INVALID'); }
  try { cleanCheckout(repo, dataCommit); } catch { issue('CENSUS_CHECKOUT_CHANGED'); }
  result.facts = [...facts.values()].sort((a, b) => a.factId.localeCompare(b.factId));
  result.sourceObservations = [...observations.values()].sort((a, b) => a.observationId.localeCompare(b.observationId));
  result.groups = result.facts.map(fact => {
    const items = result.sourceObservations.filter(item => item.factId === fact.factId).sort((a, b) => {
      const left = instant(a.fetchedAt), right = instant(b.fetchedAt); return left < right ? -1 : left > right ? 1 : a.observationId.localeCompare(b.observationId);
    });
    return { price: fact.price, observations: items.map(({ sourceUrl, sourceUrls, fetchedAt }) => ({ sourceUrl, sourceUrls, fetchedAt })),
      occurrenceCount: items.reduce((n, item) => n + item.occurrenceCount, 0), omittedObservationCount: 0,
      firstFetchedAt: items[0].fetchedAt, lastFetchedAt: items.at(-1).fetchedAt };
  });
  result.rawRegistry = rawRegistry(result.facts);
  result.sourceUniverseDigest = digest({ key: KEY, factIds: result.facts.map(item => item.factId),
    observationIds: result.sourceObservations.map(item => item.observationId).sort() });
  counts.factCount = facts.size; counts.sourceObservationCount = observations.size;
  counts.unscannedCompanyPages = Math.max(0, counts.declaredCompanyPages - counts.scannedCompanyPages);
  if (counts.unscannedCompanyPages) issue('CENSUS_INCOMPLETE_PAGES');
  if (!result.facts.length) issue('CENSUS_NO_TARGET_FACTS');
  result.complete = reasons.size === 0; result.status = result.complete ? 'complete' : 'partial'; result.reasonCodes = [...reasons].sort();
  if (Buffer.byteLength(JSON.stringify(result)) + 1 > limits.maxOutputBytes) {
    counts.omittedOccurrences += counts.retainedOccurrences; counts.retainedOccurrences = 0; counts.locationCount = 0;
    counts.factCount = 0; counts.sourceObservationCount = 0;
    result.facts = []; result.sourceObservations = []; result.groups = []; result.parts = [];
    result.rawRegistry = rawRegistry([]); result.sourceUniverseDigest = null;
    result.complete = false; result.status = 'partial'; result.reasonCodes = [...new Set([...result.reasonCodes, 'CENSUS_OUTPUT_LIMIT'])].sort();
  }
  return result;
}
function main(argv = process.argv.slice(2)) {
  try {
    if (process.env.GITHUB_ACTIONS !== 'true') fail('CENSUS_REQUIRES_ACTIONS');
    if (argv.length !== 6 || argv[0] !== '--data' || argv[2] !== '--data-commit' || argv[4] !== '--output') fail('CENSUS_ARGUMENT_INVALID');
    const repo = path.resolve(argv[1], '../../..'), output = path.resolve(argv[5]), relative = path.relative(repo, output);
    if (relative === '' || !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative)) fail('CENSUS_OUTPUT_INSIDE_CHECKOUT');
    // Refuse output through an existing link, including ancestor directories.
    let parent = path.dirname(output);
    while (!fs.existsSync(parent)) parent = path.dirname(parent);
    if (fs.realpathSync(parent) !== parent || fs.existsSync(output) && fs.lstatSync(output).isSymbolicLink()) fail('CENSUS_OUTPUT_PATH_INVALID');
    const result = scan(argv[1], { dataCommit: argv[3] });
    fs.mkdirSync(path.dirname(output), { recursive: true }); fs.writeFileSync(output, JSON.stringify(result) + '\n', { flag: 'wx' });
    process.stdout.write(JSON.stringify({ status: result.status, complete: result.complete, facts: result.counts.factCount,
      sourceObservations: result.counts.sourceObservationCount, quoteRequests: 0 }) + '\n');
    return 0;
  } catch (error) {
    const errorCode = /^CENSUS_[A-Z_]+$/.test(error?.message) ? error.message : 'CENSUS_FAILED';
    process.stdout.write(JSON.stringify({ status: 'failed', errorCode, quoteRequests: 0 }) + '\n'); return 1;
  }
}
if (require.main === module) process.exitCode = main();
module.exports = { scan, rawRegistry, instant, main, KEY, QUARTER, IDENTITY, LIMITS };
