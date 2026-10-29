'use strict';
// Reclassify stored quote endpoints only. This module has no provider client.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
const { coverageFor, splitCompanyFunds } = require('./merge-parts');
const { catalogDetails } = require('./build-data');
const { canonical } = require('../lib/strategy-claim-patches');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const fail = code => { throw new Error(code); };
const periods = { '2026Q2': ['2026-06-30', '2026-03-31'], '2026Q3': ['2026-09-30', '2026-06-30'] };
const read = file => {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 25000000) fail('REFERENCE_INPUT_INVALID');
  return JSON.parse(fs.readFileSync(file, 'utf8'));
};
const write = (file, value) => {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(temp, JSON.stringify(value) + '\n'); fs.renameSync(temp, file);
};
function sourceProjection(fund) {
  const result = structuredClone(fund);
  for (const row of result.rows || []) if (row.priceRange) {
    delete row.priceRange.rangeReference; delete row.priceRange.globalPriceConflict;
  }
  if (result.summary) delete result.summary.observedPriceCount;
  return canonical(result);
}
function rebuild(root, quarter, { now = new Date().toISOString(), pageLimits, apply = applyPriceIndex } = {}) {
  if (!Object.hasOwn(periods, quarter)) fail('REFERENCE_QUARTER_INVALID');
  const folder = path.join(root, 'web/data/periods', quarter), file = path.join(folder, 'latest.json');
  const original = read(file), manifest = structuredClone(original), config = {
    targetPeriodEnd: periods[quarter][0], baselinePeriodEnd: periods[quarter][1] };
  if (manifest.targetPeriodEnd !== config.targetPeriodEnd || manifest.baselinePeriodEnd !== config.baselinePeriodEnd
    || !Array.isArray(manifest.companyFiles)) fail('REFERENCE_PHASE_INVALID');
  const directory = catalogDetails(read(path.join(folder, 'catalog.json')));
  if (!directory.known) fail('REFERENCE_CATALOG_INVALID');
  const index = createPriceIndex(config, now), seen = new Set();
  const load = item => {
    if (!/^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/.test(item.path)) fail('REFERENCE_PATH_INVALID');
    const doc = read(path.join(folder, item.path.slice('./data/'.length)));
    if (doc.sourceMode !== 'actions' || doc.targetPeriodEnd !== config.targetPeriodEnd
      || doc.baselinePeriodEnd !== config.baselinePeriodEnd || !Array.isArray(doc.funds)) fail('REFERENCE_COMPANY_INVALID');
    return doc;
  };
  for (const item of manifest.companyFiles) {
    if (seen.has(item.path)) fail('REFERENCE_DUPLICATE_PAGE'); seen.add(item.path);
    addPriceSources(index, load(item).funds);
  }
  const counts = apply(index, [], () => false);
  const summary = { schemaVersion: 1, quarter, generatedAt: now, marketDataRequests: 0,
    originalResponsesReverified: false, dailySeriesReverified: false, sourceObservationsPreserved: true,
    eligiblePriceKeyCount: counts.eligiblePriceKeyCount, conflictingPriceKeyCount: counts.conflictingPriceKeyCount,
    referenceAnnotatedRowCount: 0, conflictMarkedRowCount: 0, changedFundCount: 0, changedPageCount: 0,
    checkedFundCount: 0, beforePriceComplete: manifest.coverage?.priceComplete || 0, afterPriceComplete: 0 };
  const staged = [], files = [], groups = new Map();
  let complete = 0, available = 0;
  // Stage all rewritten pages in memory one at a time on private disk; switch
  // the manifest only after every original observation passed the assertion.
  const temp = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'stored-reference-'));
  try {
    for (const item of manifest.companyFiles) {
      const doc = load(item), before = doc.funds.map(sourceProjection);
      const result = apply(index, doc.funds, () => false);
      doc.funds.forEach((fund, i) => { if (sourceProjection(fund) !== before[i]) fail('REFERENCE_SOURCE_OBSERVATION_CHANGED'); });
      summary.checkedFundCount += doc.funds.length;
      summary.changedFundCount += result.mutatedFunds.length;
      summary.referenceAnnotatedRowCount += result.referenceAnnotatedRowCount || 0;
      summary.conflictMarkedRowCount += result.conflictMarkedRowCount || 0;
      const make = funds => ({ ...doc, funds, coverage: coverageFor(funds, directory, config, doc.coverage?.requestedFundCount) });
      const recomputed = make(doc.funds);
      complete += recomputed.coverage.priceComplete; available += recomputed.coverage.priceAvailable;
      if (!result.mutatedFunds.length && canonical(doc.coverage) === canonical(recomputed.coverage)) files.push(item);
      else {
        summary.changedPageCount++;
        for (const funds of splitCompanyFunds(doc.funds, make, pageLimits)) {
          const page = make(funds), bytes = JSON.stringify(page) + '\n';
          const name = `${item.companyCode}-r${sha(bytes).slice(0, 20)}.json`;
          if (!/^[A-Za-z0-9_-]{1,64}\.json$/.test(name)) fail('REFERENCE_COMPANY_ID_INVALID');
          fs.writeFileSync(path.join(temp, name), bytes); staged.push(name);
          files.push({ ...item, path: './data/companies/' + name, fundCount: funds.length,
            holdingCount: page.coverage.holdingCount,
            managers: [...new Set(funds.flatMap(fund => (fund.reportManagers?.length ? fund.reportManagers : fund.managers || []).map(m => m.name)))].sort() });
        }
      }
    }
    for (const item of files) {
      const group = groups.get(item.companyCode) || []; group.push(item); groups.set(item.companyCode, group);
    }
    for (const group of groups.values()) group.forEach((item, i) => Object.assign(item, { partIndex: i + 1,
      partCount: group.length, companyFundCount: group.reduce((n, f) => n + f.fundCount, 0),
      companyHoldingCount: group.reduce((n, f) => n + f.holdingCount, 0) }));
    manifest.companyFiles = files; manifest.coverage.priceComplete = complete; manifest.coverage.priceAvailable = available;
    summary.afterPriceComplete = complete;
    const changed = canonical(original) !== canonical(manifest);
    if (changed) {
      for (const name of staged) fs.copyFileSync(path.join(temp, name), path.join(folder, 'companies', name));
      write(file, manifest);
    }
    return { ...summary, changed, manifestDigest: sha(canonical(manifest)) };
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
}
if (require.main === module) {
  if (process.env.GITHUB_ACTIONS !== 'true') fail('REFERENCE_REBUILD_REQUIRES_ACTIONS');
  const results = Object.keys(periods).map(quarter => rebuild(process.cwd(), quarter));
  write('.cache/stored-price-reference/result.json', { quarters: results, marketDataRequests: 0 });
  console.log(JSON.stringify({ status: 'reclassified', quarters: results }));
}
module.exports = { rebuild, sourceProjection };
