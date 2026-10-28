'use strict';

// Disk-backed CLI orchestration. Full input trees are never resident together:
// one bounded input file, then one explicitly connected portfolio component,
// then one output page. The in-memory merge engine remains the validator.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { checkpoint: mergeCheckpoint, capture: captureMergeFailure } = require('../lib/merge-diagnostics');
const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
const CODE = /^\d{6}$/;
const COMPANY_FILE = /^[A-Za-z0-9_-]{1,64}$/;
const ADDITIVE = ['fundCount', 'holdingCount', 'priceAvailable', 'priceComplete', 'narrativeCount',
  'noEquitiesFundCount', 'notApplicableFundCount', 'establishedAfterPeriodFundCount',
  'terminatedBeforePeriodFundCount', 'reportOnlyFundCount'];
const LIMIT = 128_000_000;
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const failure = code => Object.assign(new Error(code), { code });

function partFiles(root) {
  const files = [];
  const visit = (directory, depth) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue;
      const file = path.join(directory, entry.name);
      if (entry.isDirectory() && depth < 2) visit(file, depth + 1);
      else if (entry.isFile() && (entry.name === 'snapshot.json' || entry.name === 'part.json' || /^part[^/]*\.json$/.test(entry.name))) files.push(file);
    }
  };
  if (fs.existsSync(root)) visit(root, 0);
  return files;
}

class Components {
  constructor() { this.parents = new Map(); }
  root(code) {
    if (!this.parents.has(code)) this.parents.set(code, code);
    let value = code;
    while (this.parents.get(value) !== value) value = this.parents.get(value);
    while (this.parents.get(code) !== code) { const next = this.parents.get(code); this.parents.set(code, value); code = next; }
    return value;
  }
  join(a, b) { a = this.root(a); b = this.root(b); if (a !== b) this.parents.set(a < b ? b : a, a < b ? a : b); }
}

function mergeOnDisk({ parts, data, catalog, config, previousManifest, receiptCodes = [], engine, helpers,
  now = new Date(), pageLimits, componentLimit = LIMIT, reuseAcrossComponents = true, beforeCommit = null } = {}) {
  const { readJson, validEnvelope, coverageFor, observationDates, recoverPrice, writeAtomic, publicUrl, catalogDetails } = helpers;
  const current = now instanceof Date ? now : new Date(now);
  const directory = catalogDetails(catalog);
  if (!directory.known || !Number.isFinite(current.getTime())) throw failure('INVALID_CATALOG');
  const directoryByCode = new Map(directory.funds.map(fund => [fund.code, fund]));
  const companyByCode = new Map(directory.companies.map(company => [company.id, company]));
  const maxBytes = pageLimits?.maxBytes || helpers.maxCompanyBytes;
  const maxFunds = pageLimits?.maxFunds || helpers.maxCompanyFunds;
  if (!Number.isInteger(componentLimit) || componentLimit < 1024 || componentLimit > LIMIT) throw failure('INVALID_ARGUMENTS');
  // Raw bounded inputs are temporary work, never files under the publication
  // tree (including if the process is killed before its finally block).
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'record-merge-stage-'));
  const inputRoot = path.join(workspace, 'inputs'), normalizedRoot = path.join(workspace, 'funds'), outputRoot = path.join(workspace, 'companies');
  for (const root of [inputRoot, normalizedRoot, outputRoot]) fs.mkdirSync(root);
  const companyRoot = path.join(data, 'companies'), manifestFile = path.join(data, 'latest.json');
  const components = new Components(), codeBytes = new Map(), requested = new Set(receiptCodes.filter(code => CODE.test(code)));
  const errors = new Set(); let errorCount = 0, needsRepartition = false, inputSequence = 0;
  const addError = code => { errors.add(code); errorCount++; };
  const append = (code, record) => {
    const text = JSON.stringify(record) + '\n', size = Buffer.byteLength(text), total = (codeBytes.get(code) || 0) + size;
    if (total > componentLimit) throw failure('MERGE_COMPONENT_TOO_LARGE');
    codeBytes.set(code, total); components.root(code);
    fs.appendFileSync(path.join(inputRoot, code + '.jsonl'), text);
  };
  const recordLinks = fund => {
    if (!fund || !CODE.test(fund.code)) return;
    components.root(fund.code);
    // This only co-locates potential relatives; the engine still validates
    // identity and conflicts before any deduplication or attribution.
    if (CODE.test(fund.sourcePortfolioCode || '') && publicUrl(fund.portfolioSourceUrl)
      && Array.isArray(fund.shareClassCodes) && fund.shareClassCodes.includes(fund.code)
      && fund.shareClassCodes.includes(fund.sourcePortfolioCode)) {
      for (const code of fund.shareClassCodes) if (CODE.test(code)) components.join(fund.code, code);
    }
  };
  const ingest = (envelope, kind) => {
    // Rejected envelopes still identify which requested codes need the final
    // retained observation receipt, exactly as the validating engine does.
    if (kind === 'snapshot') for (const key of ['funds', 'batchResults', 'pricePatches', 'strategyClaimsReceipts', 'strategyClaimsPatches']) {
      for (const item of Array.isArray(envelope?.[key]) ? envelope[key] : []) if (CODE.test(item?.code || '')) requested.add(item.code);
    }
    if (!validEnvelope(envelope, current, config)) { addError(kind === 'snapshot' ? 'INVALID_PART' : 'INVALID_COMPANY_SNAPSHOT'); return; }
    const sequence = inputSequence++;
    const meta = { schemaVersion: envelope.schemaVersion, sourceMode: envelope.sourceMode, asOf: envelope.asOf,
      retrievedAt: envelope.retrievedAt, priceUpdatedAt: envelope.priceUpdatedAt,
      strategyClaimsPlannedAt: envelope.strategyClaimsPlannedAt,
      targetPeriodEnd: envelope.targetPeriodEnd, baselinePeriodEnd: envelope.baselinePeriodEnd };
    const records = new Map();
    const forCode = code => {
      if (!records.has(code)) records.set(code, { kind, sequence, envelope: { ...meta, funds: [], batchResults: [], pricePatches: [], inceptionPatches: [],
        strategyClaimsTasks: [], strategyClaimsReceipts: [], strategyClaimsPatches: [] } });
      return records.get(code).envelope;
    };
    for (const fund of envelope.funds) {
      if (!fund || !CODE.test(fund.code)) { addError('INVALID_FUND'); continue; }
      forCode(fund.code).funds.push(fund); recordLinks(fund);
      if (kind === 'snapshot') requested.add(fund.code);
    }
    if (kind === 'snapshot') for (const key of ['batchResults', 'pricePatches', 'inceptionPatches', 'strategyClaimsTasks', 'strategyClaimsReceipts', 'strategyClaimsPatches']) {
      for (const item of Array.isArray(envelope[key]) ? envelope[key] : []) {
        if (!item || !CODE.test(item.code)) continue;
        forCode(item.code)[key].push(item);
        if (!['inceptionPatches', 'strategyClaimsTasks'].includes(key)) requested.add(item.code);
      }
    }
    for (const [code, record] of records) append(code, record);
  };
  const newFiles = [];
  let committed = false, primaryFailure = null;
  try {
    mergeCheckpoint('inputs', 0);
    const references = Array.isArray(previousManifest?.companyFiles)
      ? previousManifest.companyFiles.map(item => item?.path?.match(/^\.\/data\/companies\/([A-Za-z0-9_-]{1,64})\.json$/)?.[1]) : null;
    if (references?.some(value => !value)) throw failure('MERGE_READ_FAILED');
    const referenced = references && new Set(references);
    if (previousManifest?.funds?.length && !previousManifest.companyFiles?.length && validEnvelope(previousManifest, current, config)) ingest(previousManifest, 'bootstrap');
    const priorFiles = referenced ? [...referenced].sort() : fs.existsSync(companyRoot)
      ? fs.readdirSync(companyRoot, { withFileTypes: true }).filter(entry => entry.isFile() && entry.name.endsWith('.json')
        && COMPANY_FILE.test(entry.name.slice(0, -5))).map(entry => entry.name.slice(0, -5)) : [];
    for (const key of priorFiles) {
      // A missing or unreadable referenced page is not an empty company: abort
      // without replacing the prior manifest rather than silently losing it.
      const envelope = readJson(path.join(companyRoot, key + '.json'));
      if (envelope.funds?.length > maxFunds || Buffer.byteLength(JSON.stringify(envelope)) + 1 > maxBytes) needsRepartition = true;
      ingest(envelope, 'previous');
    }
    for (const file of partFiles(parts)) {
      let envelope;
      try { envelope = readJson(file); } catch { addError('INVALID_PART'); continue; }
      ingest(envelope, 'snapshot');
    }
    for (const code of requested) components.root(code);
    const groups = new Map();
    for (const code of components.parents.keys()) {
      const root = components.root(code);
      if (!groups.has(root)) groups.set(root, { codes: [], bytes: 0 });
      const group = groups.get(root); group.codes.push(code); group.bytes += codeBytes.get(code) || 0;
      if (group.bytes > componentLimit) throw failure('MERGE_COMPONENT_TOO_LARGE');
    }
    const priceIndex = createPriceIndex(config, current);
    const funds = [], receipt = { schemaVersion: 1, phase: { targetPeriodEnd: config.targetPeriodEnd, baselinePeriodEnd: config.baselinePeriodEnd },
      generatedAt: current.toISOString(), requestedCodes: [...requested].sort(), funds: [], pricePatches: [], strategyClaims: [] };
    const counts = { acceptedFreshFundCount: 0, acceptedPricePatchCount: 0, acceptedInceptionPatchCount: 0, acceptedStrategyClaimsPatchCount: 0,
      migratedBootstrapFundCount: 0, reusedPriceRowCount: 0, reusedPriceFundCount: 0, partialConflictRowCount: 0,
      conflictMarkedRowCount: 0, conflictMarkedFundCount: 0, conflictingPriceRowCount: 0 };
    let completedComponents = 0;
    for (const group of groups.values()) {
      mergeCheckpoint('components', completedComponents++);
      // Each component is bounded before any of its records are parsed.
      const records = [];
      for (const code of group.codes) {
        const filename = path.join(inputRoot, code + '.jsonl');
        if (!fs.existsSync(filename)) continue;
        for (const line of fs.readFileSync(filename, 'utf8').split('\n')) if (line) records.push(JSON.parse(line));
      }
      records.sort((a, b) => a.sequence - b.sequence);
      const previousCompanies = {}, snapshots = []; let bootstrap = null;
      for (const record of records) {
        if (record.kind === 'snapshot') snapshots.push(record.envelope);
        else if (record.kind === 'bootstrap') {
          if (!bootstrap) bootstrap = { ...record.envelope, funds: [] };
          bootstrap.funds.push(...record.envelope.funds);
        } else previousCompanies['previous-' + Object.keys(previousCompanies).length] = record.envelope;
      }
      const componentFunds = group.codes.map(code => directoryByCode.get(code)).filter(Boolean);
      const componentCompanies = [...new Set(componentFunds.map(fund => fund.companyId))].map(code => companyByCode.get(code)).filter(Boolean)
        .map(company => ({ code: company.id, name: company.name, directoryStatus: company.directoryStatus, fundCount: company.declaredFundCount }));
      const componentCatalog = { funds: componentFunds, companies: componentCompanies,
        fundCount: directory.fundCount, companyCount: directory.companyCount };
      const result = engine({ snapshots, previousCompanies, previousManifest: bootstrap, catalog: componentCatalog, config, now: current,
        // Component-local bootstrap is null for ordinary persisted pages.
        // Keep the outer manifest's provenance without passing that global
        // manifest into a component's no-change/output decision.
        preservePersisted: Boolean(previousManifest),
        receiptCodes: group.codes.filter(code => requested.has(code)), pageLimits, reusePrices: false,
        onSourceFunds: reuseAcrossComponents ? values => addPriceSources(priceIndex, values) : null });
      for (const key of ['acceptedFreshFundCount', 'acceptedPricePatchCount', 'acceptedInceptionPatchCount', 'acceptedStrategyClaimsPatchCount', 'migratedBootstrapFundCount']) counts[key] += result.counts[key] || 0;
      errorCount += result.counts.errorCount || 0;
      result.errors.forEach(code => errors.add(code));
      receipt.funds.push(...result.receipt.funds); receipt.pricePatches.push(...result.receipt.pricePatches);
      receipt.strategyClaims.push(...result.receipt.strategyClaims);
      for (const envelope of Object.values(result.companySnapshots)) for (const fund of envelope.funds) {
        const filename = path.join(normalizedRoot, fund.code + '.json');
        fs.writeFileSync(filename, JSON.stringify(fund));
        funds.push({ code: fund.code, companyId: fund.companyId, file: filename });
      }
      // A component has been fully validated and staged. Its private input
      // spool is no longer needed; original inputs and published pages remain.
      for (const code of group.codes) fs.rmSync(path.join(inputRoot, code + '.jsonl'), { force: true });
    }
    // Global index is sealed only after all independent source components were
    // examined; a late conflicting source blocks reuse in every component.
    const dates = [], available = new Set(), companies = new Map();
    const totalCoverage = coverageFor([], directory, config);
    const companyFiles = [];
    let currentCompany = null, page = [], bytes = 0, makeEnvelope;
    const flush = () => {
      if (!page.length) return;
      const envelope = makeEnvelope(page), serialized = JSON.stringify(envelope) + '\n';
      if (Buffer.byteLength(serialized) > maxBytes) throw failure('COMPANY_FUND_TOO_LARGE');
      const key = currentCompany + '-r' + sha(serialized).slice(0, 20);
      fs.writeFileSync(path.join(outputRoot, key + '.json'), serialized);
      const info = companies.get(currentCompany);
      companyFiles.push({ companyCode: currentCompany, companyName: info.name, path: './data/companies/' + key + '.json',
        partIndex: ++info.partCount, partCount: 0, companyFundCount: 0, companyHoldingCount: 0,
        fundCount: page.length, holdingCount: envelope.coverage.holdingCount,
        managers: [...new Set(page.flatMap(fund => (fund.reportManagers?.length ? fund.reportManagers : fund.managers).map(manager => manager.name)))].sort() });
      page = [];
    };
    let completedPriceFunds = 0;
    for (const entry of funds.sort((a, b) => a.companyId.localeCompare(b.companyId) || a.code.localeCompare(b.code))) {
      mergeCheckpoint('prices', completedPriceFunds++);
      const fund = readJson(entry.file);
      // The bounded page below owns this parsed fund until it is staged.
      // Release only its private normalized copy before writing output pages.
      fs.unlinkSync(entry.file);
      const applied = reuseAcrossComponents ? applyPriceIndex(priceIndex, [fund], recoverPrice) : {
        reusedPriceRowCount: 0, reusedPriceFundCount: 0, partialConflictRowCount: 0,
        conflictMarkedRowCount: 0, conflictMarkedFundCount: 0, conflictingPriceRowCount: 0,
        eligiblePriceKeyCount: 0, conflictingPriceKeyCount: 0,
      };
      for (const key of ['reusedPriceRowCount', 'reusedPriceFundCount', 'partialConflictRowCount', 'conflictMarkedRowCount', 'conflictMarkedFundCount', 'conflictingPriceRowCount']) counts[key] += applied[key];
      counts.eligiblePriceKeyCount = applied.eligiblePriceKeyCount; counts.conflictingPriceKeyCount = applied.conflictingPriceKeyCount;
      if (currentCompany !== entry.companyId) {
        flush(); currentCompany = entry.companyId;
        const info = directory.companies.find(company => company.id === currentCompany);
        const companyName = info?.name || fund.company || '目录公司未匹配';
        const expected = directory.funds.filter(item => item.companyId === currentCompany).length;
        companies.set(currentCompany, { name: companyName, fundCount: 0, holdingCount: 0, partCount: 0 });
        makeEnvelope = values => ({ schemaVersion: 1, ...observationDates(values), sourceMode: 'actions',
          targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
          company: { code: currentCompany, name: companyName }, coverage: coverageFor(values, directory, config, expected), funds: values, errors: [] });
        bytes = Buffer.byteLength(JSON.stringify(makeEnvelope([]))) + 1024;
      }
      const coverage = coverageFor([fund], directory, config);
      ADDITIVE.forEach(key => { totalCoverage[key] += coverage[key]; });
      const company = companies.get(currentCompany); company.fundCount++; company.holdingCount += coverage.holdingCount;
      dates.push({ asOf: fund.asOf, retrievedAt: fund.retrievedAt, priceUpdatedAt: fund.priceUpdatedAt });
      if (['ok', 'no-equities'].includes(fund.status)) for (const code of fund.sourcePortfolioCode && publicUrl(fund.portfolioSourceUrl) ? fund.shareClassCodes : [fund.code]) available.add(code);
      const size = Buffer.byteLength(JSON.stringify(fund)) + 1;
      if (page.length && (page.length >= maxFunds || bytes + size > maxBytes)) { flush(); bytes = Buffer.byteLength(JSON.stringify(makeEnvelope([]))) + 1024; }
      if (bytes + size > maxBytes) throw failure('COMPANY_FUND_TOO_LARGE');
      page.push(fund); bytes += size;
    }
    flush();
    if (!funds.length && reuseAcrossComponents) {
      const empty = applyPriceIndex(priceIndex, [], recoverPrice);
      counts.eligiblePriceKeyCount = empty.eligiblePriceKeyCount; counts.conflictingPriceKeyCount = empty.conflictingPriceKeyCount;
    }
    for (const file of companyFiles) {
      const info = companies.get(file.companyCode);
      Object.assign(file, { partCount: info.partCount, companyFundCount: info.fundCount, companyHoldingCount: info.holdingCount });
    }
    const companyCoverage = directory.companies.map(company => {
      const records = directory.funds.filter(fund => fund.companyId === company.id), count = records.filter(fund => available.has(fund.code)).length;
      const expected = company.declaredFundCount === null && ['pending', 'failed'].includes(company.directoryStatus) ? null : Math.max(company.declaredFundCount || 0, records.length);
      return { id: company.id, companyCode: company.id, name: company.name, catalogFundCount: expected,
        availableFundCount: count, directoryStatus: company.directoryStatus, mappedFundCount: records.length,
        availablePortfolioCount: companies.get(company.id)?.fundCount || 0, missingFundCount: expected === null ? null : Math.max(0, expected - count),
        status: expected === null ? 'directory-incomplete' : !count ? 'unavailable' : count < expected ? 'partial' : 'available' };
    });
    Object.assign(totalCoverage, { companyCount: companies.size, catalogAvailableFundCount: directory.funds.filter(fund => available.has(fund.code)).length,
      unmatchedCompanyFundCount: companies.get('unknown')?.fundCount || 0, status: funds.length ? 'partial' : 'awaiting-data' });
    const manifest = { schemaVersion: 1, ...observationDates(dates), sourceMode: 'actions', targetPeriodEnd: config.targetPeriodEnd || null,
      baselinePeriodEnd: config.baselinePeriodEnd || null, coverage: totalCoverage, companyCoverage, companyFiles, funds: [], errors: [] };
    // Rebuild receipts after global price reuse from already staged, bounded
    // pages, without keeping their holdings/manager/strategy arrays resident.
    const observations = new Map();
    mergeCheckpoint('receipts', 0);
    for (const file of companyFiles) {
      const envelope = readJson(path.join(outputRoot, path.basename(file.path)));
      for (const fund of envelope.funds) {
        const item = helpers.finalObservation(fund);
        for (const code of item.shareClassCodes) if (requested.has(code)) observations.set(code, { code, ...item });
      }
    }
    for (const old of receipt.funds) {
      if (old.attempted === false) observations.set(old.code, old);
      else if (observations.has(old.code) && old.attempted === true) observations.get(old.code).attempted = true;
    }
    receipt.funds = [...observations.values()].sort((a, b) => a.code.localeCompare(b.code));
    const changed = Boolean(!previousManifest || needsRepartition || ['acceptedFreshFundCount', 'acceptedPricePatchCount', 'acceptedInceptionPatchCount', 'acceptedStrategyClaimsPatchCount', 'migratedBootstrapFundCount', 'reusedPriceFundCount', 'conflictMarkedFundCount'].some(key => counts[key]));
    counts.errorCount = errorCount;
    if (changed) Object.assign(counts, { companyCount: companies.size, fundCount: funds.length });
    else delete counts.migratedBootstrapFundCount;
    // Content-addressed company filenames cannot overwrite an older manifest's
    // observations. Publish the small manifest only after all pages are ready.
    if (changed) {
      mergeCheckpoint('commit', 0);
      fs.mkdirSync(companyRoot, { recursive: true });
      for (const file of companyFiles) {
        const name = path.basename(file.path), destination = path.join(companyRoot, name), staged = path.join(outputRoot, name);
        if (fs.existsSync(destination)) {
          if (sha(fs.readFileSync(destination)) !== sha(fs.readFileSync(staged))) throw failure('MERGE_WRITE_FAILED');
        } else {
          // writeAtomic stages on the destination filesystem; the system temp
          // workspace may reside on a different mount.
          writeAtomic(destination, readJson(staged)); newFiles.push(destination);
        }
      }
    }
    if (beforeCommit) beforeCommit({ manifest, receipt, workspace });
    writeAtomic(path.join(parts, 'merge-receipt.json'), receipt);
    if (changed) writeAtomic(manifestFile, manifest);
    committed = true;
    if (changed) {
      const keep = new Set(companyFiles.map(file => path.basename(file.path)));
      for (const entry of fs.readdirSync(companyRoot, { withFileTypes: true })) {
        if (entry.isFile() && entry.name.endsWith('.json') && COMPANY_FILE.test(entry.name.slice(0, -5)) && !keep.has(entry.name)) {
          try { fs.unlinkSync(path.join(companyRoot, entry.name)); } catch { /* Unreferenced old pages may be cleaned on the next successful merge. */ }
        }
      }
    }
    mergeCheckpoint('done', funds.length);
    return { changed, manifest: changed ? manifest : previousManifest, receipt, counts, errors: [...errors] };
  } catch (error) {
    // Keep the failure-time free-space reading, before cleanup releases the
    // private spool. Post-cleanup space cannot diagnose a full scratch disk.
    captureMergeFailure(error);
    primaryFailure = error;
    throw error;
  } finally {
    if (!committed) for (const file of newFiles) { try { fs.unlinkSync(file); } catch { /* Preserve the original manifest on failure. */ } }
    try { fs.rmSync(workspace, { recursive: true, force: true }); }
    catch (cleanupError) { if (!primaryFailure) throw cleanupError; }
  }
}

module.exports = { mergeOnDisk, partFiles };
