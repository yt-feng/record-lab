#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { buildSnapshot, catalogDetails, deduplicatePortfolios, publicUrl } = require('./build-data');
const { validDate } = require('../lib/fund-analysis');

const CODE = /^\d{6}$/;
const COMPANY = /^[A-Za-z0-9_-]{1,40}$/;
const timestamp = value => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
const label = (value, limit = 200) => typeof value === 'string' ? value.trim().slice(0, limit) : null;
const clone = value => JSON.parse(JSON.stringify(value));
const fixedErrors = new Set(['INVALID_CATALOG', 'INVALID_PART', 'INVALID_FUND', 'INVALID_COMPANY_SNAPSHOT',
  'CATALOG_IDENTITY_MISMATCH', 'INVALID_ARGUMENTS', 'MERGE_READ_FAILED', 'MERGE_WRITE_FAILED',
  'PORTFOLIO_IDENTITY_CONFLICT', 'TARGET_PERIOD_MISMATCH', 'MERGE_FAILED']);

class MergeError extends Error {
  constructor(code) { super(fixedErrors.has(code) ? code : 'MERGE_FAILED'); this.code = this.message; }
}

function validEnvelope(envelope, now, config) {
  return envelope && envelope.schemaVersion === 1 && envelope.sourceMode === 'actions'
    && Array.isArray(envelope.funds) && validDate(envelope.asOf)
    && envelope.asOf <= now.toISOString().slice(0, 10) && timestamp(envelope.retrievedAt)
    && Date.parse(envelope.retrievedAt) <= now.getTime() + 300000
    && (!config.targetPeriodEnd || !envelope.targetPeriodEnd || envelope.targetPeriodEnd === config.targetPeriodEnd);
}

function recoverPrice(row, supplied, retrievedAt) {
  if (!supplied || supplied.status !== 'observed' || supplied.priceBasis !== 'unadjusted'
    || !publicUrl(supplied.sourceUrl) || supplied.currency !== row.currency
    || supplied.rangeStart !== row.priceRange.rangeStart || supplied.rangeEnd !== row.priceRange.rangeEnd
    || !Number.isFinite(supplied.low) || supplied.low <= 0 || !Number.isFinite(supplied.high)
    || supplied.high < supplied.low || !Number.isInteger(supplied.observedCount) || supplied.observedCount < 1
    || !validDate(supplied.observedStart) || !validDate(supplied.observedEnd)
    || supplied.observedStart < supplied.rangeStart || supplied.observedEnd > supplied.rangeEnd
    || supplied.observedStart > supplied.observedEnd) return false;
  const fetchedAt = supplied.fetchedAt || retrievedAt;
  if (!timestamp(fetchedAt) || Date.parse(fetchedAt) > Date.parse(retrievedAt)) return false;
  row.priceRange = { ...row.priceRange, status: 'observed',
    label: '已观测区间价格（非持仓成本；完整性未核验）', low: supplied.low, high: supplied.high,
    priceBasis: 'unadjusted', observedStart: supplied.observedStart, observedEnd: supplied.observedEnd,
    observedCount: supplied.observedCount, sourceUrl: supplied.sourceUrl, fetchedAt,
    dataStatus: supplied.dataStatus === 'cached' ? 'cached' : 'fresh' };
  return true;
}

function sanitizeFund(item, { now, config, envelope }) {
  if (!item || !CODE.test(item.code) || !['ok', 'no-equities'].includes(item.status)
    || !['fresh', 'cached'].includes(item.dataStatus) || !validDate(item.asOf)
    || item.asOf > envelope.asOf || !timestamp(item.retrievedAt)
    || Date.parse(item.retrievedAt) > Date.parse(envelope.retrievedAt)
    || !item.selectedReport || !Array.isArray(item.rows)
    || (config.targetPeriodEnd && item.selectedReport.periodEnd !== config.targetPeriodEnd)) return null;
  const reportList = [...(Array.isArray(item.reports) ? item.reports : []), item.selectedReport, item.previousReport].filter(Boolean);
  const reports = [...new Map(reportList.map(report => [[report.periodEnd, report.kind, report.sourceUrl].join('|'), report])).values()];
  const cleanInput = {
    code: item.code, name: label(item.name), company: label(item.company), companyId: label(item.companyId, 40),
    managers: (Array.isArray(item.managers) ? item.managers : []).slice(0, 50)
      .map(manager => ({ name: label(manager && manager.name, 100), startDate: manager && manager.startDate,
        endDate: manager && manager.endDate })),
    metadataSourceUrl: item.metadataSourceUrl, managerSourceUrl: item.managerSourceUrl,
    managerScope: item.managerScope, managersAsOf: item.managersAsOf,
    shareClassCodes: (Array.isArray(item.shareClassCodes) ? item.shareClassCodes : []).filter(code => typeof code === 'string' && CODE.test(code)),
    sourcePortfolioCode: item.sourcePortfolioCode, portfolioSourceUrl: item.portfolioSourceUrl,
    reports, noEquitiesEvidence: item.noEquitiesEvidence,
  };
  let clean;
  try {
    clean = buildSnapshot({ asOf: item.asOf, retrievedAt: item.retrievedAt, funds: [cleanInput], barsByStock: {}, errors: [] },
      { now, config: { funds: [{ code: item.code }], targetPeriodEnd: config.targetPeriodEnd,
        baselinePeriodEnd: config.baselinePeriodEnd } }).funds[0];
  } catch { return null; }
  if (!clean || clean.selectedReport.periodEnd !== item.selectedReport.periodEnd || clean.status !== item.status) return null;
  const suppliedRows = new Map(item.rows.filter(row => row && typeof row.stockCode === 'string')
    .map(row => [`${row.market}:${row.stockCode}`, row]));
  for (const row of clean.rows) {
    recoverPrice(row, suppliedRows.get(`${row.market}:${row.stockCode}`)?.priceRange, clean.retrievedAt);
  }
  clean.summary.observedPriceCount = clean.rows.filter(row => row.priceRange.status === 'observed').length;
  clean.dataStatus = item.dataStatus;
  clean.cacheReason = item.dataStatus === 'cached' ? 'PREVIOUS_OBSERVATION' : null;
  if (clean.sourcePortfolioCode && publicUrl(clean.portfolioSourceUrl)) {
    const allowed = new Set(clean.shareClassCodes);
    clean.aliases = (Array.isArray(item.aliases) ? item.aliases : []).filter(alias => alias && allowed.has(alias.code))
      .map(alias => ({ code: alias.code, name: label(alias.name) }));
  }
  return clean;
}

function mapCompany(fund, directory) {
  const identity = directory.get(fund.code);
  const aliases = fund.sourcePortfolioCode && publicUrl(fund.portfolioSourceUrl)
    && fund.shareClassCodes.includes(fund.sourcePortfolioCode) && fund.shareClassCodes.includes(fund.code)
    ? fund.shareClassCodes : [fund.code];
  const knownCompanies = new Set(aliases.map(code => directory.get(code)?.companyId).filter(Boolean));
  if (knownCompanies.size > 1) return null;
  const hasUnmappedCode = aliases.some(code => !directory.get(code)?.companyId);
  const companyCode = identity && COMPANY.test(identity.companyId || '') && !hasUnmappedCode ? identity.companyId : 'unknown';
  fund.companyId = companyCode;
  fund.company = companyCode === 'unknown' ? '目录公司未匹配' : identity.company || fund.company;
  fund.companyMapping = companyCode === 'unknown' ? 'unmatched-catalog' : 'catalog';
  return companyCode;
}

function shouldReplace(current, candidate) {
  if (!current) return true;
  if (Date.parse(candidate.retrievedAt) <= Date.parse(current.retrievedAt)) return false;
  if (candidate.selectedReport.periodEnd < current.selectedReport.periodEnd) return false;
  if (candidate.selectedReport.periodEnd === current.selectedReport.periodEnd) {
    if (candidate.status !== current.status) return false;
    if (candidate.selectedReport.holdings.length < current.selectedReport.holdings.length) return false;
    if (current.selectedReport.coverage === 'full' && candidate.selectedReport.coverage !== 'full') return false;
  }
  return true;
}

function retainPrices(candidate, old) {
  if (!old || old.selectedReport.periodEnd !== candidate.selectedReport.periodEnd) return;
  const rows = new Map(old.rows.map(row => [`${row.market}:${row.stockCode}`, row]));
  for (const row of candidate.rows) {
    if (row.priceRange.status !== 'observed') {
      const prior = rows.get(`${row.market}:${row.stockCode}`)?.priceRange;
      if (prior && recoverPrice(row, prior, candidate.retrievedAt)) row.priceRange.dataStatus = 'cached';
    }
  }
  candidate.summary.observedPriceCount = candidate.rows.filter(row => row.priceRange.status === 'observed').length;
}

function coverageFor(funds, directory, config, expectedCount = directory.fundCount) {
  return {
    targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
    fundCount: funds.length, companyCount: new Set(funds.map(fund => fund.companyId)).size,
    scope: '按公开目录逐家处理，展示已取得的有效报告；已披露数据与目录规模分别计数',
    requestedFundCount: expectedCount, catalogFundCount: directory.fundCount, catalogCompanyCount: directory.companyCount,
    holdingCount: funds.reduce((sum, fund) => sum + fund.rows.filter(row => row.current).length, 0),
    priceAvailable: funds.reduce((sum, fund) => sum + fund.rows.filter(row => row.current && row.priceRange.status === 'observed').length, 0),
    narrativeCount: funds.filter(fund => fund.narrative?.text).length,
    noEquitiesFundCount: funds.filter(fund => fund.status === 'no-equities').length,
    catalogStatus: directory.known ? 'available' : 'unavailable',
    limitations: ['目录总数不代表已完成采集。', '只有报告明确确认未持有股票才记为无股票持仓。',
      '区间最高最低价不是实际持仓成本。', '同组合仅凭公开主代码和份额证据去重，份额金额不叠加。'],
  };
}

function observationDates(funds) {
  return { asOf: funds.length ? funds.map(fund => fund.asOf).sort().at(-1) : null,
    retrievedAt: funds.length ? [...funds].sort((a, b) => Date.parse(b.retrievedAt) - Date.parse(a.retrievedAt))[0].retrievedAt : null };
}

function mergeSnapshots({ snapshots = [], previousCompanies = {}, previousManifest = null, catalog,
  config = {}, now = new Date() } = {}) {
  const currentTime = now instanceof Date ? now : new Date(now);
  if (!Number.isFinite(currentTime.getTime())) throw new MergeError('MERGE_FAILED');
  let directory;
  try { directory = catalogDetails(catalog); } catch { throw new MergeError('INVALID_CATALOG'); }
  if (!directory.known) throw new MergeError('INVALID_CATALOG');
  const directoryMap = new Map(directory.funds.map(fund => [fund.code, fund]));
  const byCode = new Map();
  const errors = [];
  const addError = code => errors.push(fixedErrors.has(code) ? code : 'INVALID_FUND');
  let migratedBootstrapFundCount = 0;
  const hasBootstrap = previousManifest && Array.isArray(previousManifest.funds) && previousManifest.funds.length
    && (!Array.isArray(previousManifest.companyFiles) || !previousManifest.companyFiles.length)
    && validEnvelope(previousManifest, currentTime, config);
  if (hasBootstrap) {
    for (const item of previousManifest.funds) {
      const fund = sanitizeFund(item, { now: currentTime, config, envelope: previousManifest });
      if (!fund) { addError('INVALID_FUND'); continue; }
      if (!mapCompany(fund, directoryMap)) { addError('CATALOG_IDENTITY_MISMATCH'); continue; }
      if (!byCode.has(fund.code)) {
        byCode.set(fund.code, fund);
        migratedBootstrapFundCount += 1;
      }
    }
  }
  for (const [companyCode, envelope] of Object.entries(previousCompanies)) {
    if (!COMPANY.test(companyCode) || !validEnvelope(envelope, currentTime, config)) { addError('INVALID_COMPANY_SNAPSHOT'); continue; }
    for (const item of envelope.funds) {
      const fund = sanitizeFund(item, { now: currentTime, config, envelope });
      if (!fund) { addError('INVALID_FUND'); continue; }
      const actual = mapCompany(fund, directoryMap);
      if (!actual) { addError('CATALOG_IDENTITY_MISMATCH'); continue; }
      if (!byCode.has(fund.code) || Date.parse(fund.retrievedAt) > Date.parse(byCode.get(fund.code).retrievedAt)) byCode.set(fund.code, fund);
    }
  }
  let acceptedFreshFundCount = 0;
  for (const envelope of snapshots) {
    if (!validEnvelope(envelope, currentTime, config)) { addError('INVALID_PART'); continue; }
    for (const item of envelope.funds) {
      if (item?.dataStatus !== 'fresh') continue;
      const fund = sanitizeFund(item, { now: currentTime, config, envelope });
      if (!fund) { addError('INVALID_FUND'); continue; }
      if (!mapCompany(fund, directoryMap)) { addError('CATALOG_IDENTITY_MISMATCH'); continue; }
      const old = byCode.get(fund.code);
      if (!shouldReplace(old, fund)) continue;
      retainPrices(fund, old);
      byCode.set(fund.code, fund);
      acceptedFreshFundCount += 1;
    }
  }
  // No new observation means no fabricated new timestamp or overwritten old files.
  if (!acceptedFreshFundCount && !migratedBootstrapFundCount && previousManifest) {
    return { changed: false, manifest: previousManifest, companySnapshots: {},
      counts: { acceptedFreshFundCount: 0, errorCount: errors.length }, errors: [...new Set(errors)] };
  }
  const funds = acceptedFreshFundCount || migratedBootstrapFundCount ? deduplicatePortfolios([...byCode.values()], addError) : [];
  const buckets = new Map();
  for (const fund of funds) {
    const companyCode = mapCompany(fund, directoryMap);
    if (!companyCode) { addError('CATALOG_IDENTITY_MISMATCH'); continue; }
    if (!buckets.has(companyCode)) buckets.set(companyCode, []);
    buckets.get(companyCode).push(fund);
  }
  const companySnapshots = {};
  const companyFiles = [];
  const companyCoverage = [];
  for (const [companyCode, companyFunds] of [...buckets].sort(([a], [b]) => a.localeCompare(b))) {
    companyFunds.sort((a, b) => a.code.localeCompare(b.code));
    const info = directory.companies.find(company => company.id === companyCode);
    const companyName = info?.name || companyFunds[0].company || '目录公司未匹配';
    const expected = directory.funds.filter(fund => fund.companyId === companyCode).length;
    const dates = observationDates(companyFunds);
    companySnapshots[companyCode] = { schemaVersion: 1, ...dates, sourceMode: 'actions',
      targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
      company: { code: companyCode, name: companyName },
      coverage: coverageFor(companyFunds, directory, config, expected), funds: companyFunds, errors: [] };
    companyFiles.push({ companyCode, companyName, path: `./data/companies/${companyCode}.json`,
      fundCount: companyFunds.length, holdingCount: companySnapshots[companyCode].coverage.holdingCount,
      managers: [...new Set(companyFunds.flatMap(fund => (fund.reportManagers?.length ? fund.reportManagers : fund.managers)
        .map(manager => manager.name)))].sort() });
  }
  const availableCodes = new Set(funds.flatMap(fund => fund.sourcePortfolioCode && publicUrl(fund.portfolioSourceUrl)
    ? fund.shareClassCodes : [fund.code]));
  for (const company of directory.companies) {
    const records = directory.funds.filter(fund => fund.companyId === company.id);
    const available = records.filter(fund => availableCodes.has(fund.code)).length;
    const expected = company.declaredFundCount === null && ['pending', 'failed'].includes(company.directoryStatus)
      ? null : Math.max(company.declaredFundCount || 0, records.length);
    companyCoverage.push({ id: company.id, companyCode: company.id, name: company.name,
      catalogFundCount: expected, availableFundCount: available, directoryStatus: company.directoryStatus,
      mappedFundCount: records.length,
      availablePortfolioCount: buckets.get(company.id)?.length || 0,
      missingFundCount: expected === null ? null : Math.max(0, expected - available),
      status: expected === null ? 'directory-incomplete' : !available ? 'unavailable' : available < expected ? 'partial' : 'available' });
  }
  const dates = observationDates(funds);
  const coverage = coverageFor(funds, directory, config);
  coverage.catalogAvailableFundCount = directory.funds.filter(fund => availableCodes.has(fund.code)).length;
  coverage.unmatchedCompanyFundCount = buckets.get('unknown')?.length || 0;
  coverage.status = funds.length ? 'partial' : 'awaiting-data';
  const manifest = { schemaVersion: 1, ...dates, sourceMode: 'actions',
    targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
    coverage, companyCoverage, companyFiles, funds: [], errors: [] };
  return { changed: true, manifest, companySnapshots,
    counts: { acceptedFreshFundCount, migratedBootstrapFundCount, companyCount: companyFiles.length,
      fundCount: funds.length, errorCount: errors.length },
    errors: [...new Set(errors)] };
}

function readJson(filename) {
  try {
    const stat = fs.lstatSync(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 25_000_000) throw new Error('bounded-read');
    return JSON.parse(fs.readFileSync(filename, 'utf8'));
  } catch { throw new MergeError('MERGE_READ_FAILED'); }
}

function partSnapshots(root) {
  const snapshots = [];
  if (!fs.existsSync(root)) return snapshots;
  const visit = (directory, depth) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue;
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory() && depth < 2) visit(filename, depth + 1);
      else if (entry.isFile() && (entry.name === 'snapshot.json' || entry.name === 'part.json' || /^part[^/]*\.json$/.test(entry.name))) {
        try { snapshots.push(readJson(filename)); } catch { snapshots.push(null); }
      }
    }
  };
  visit(root, 0);
  return snapshots;
}

function writeAtomic(filename, value) {
  const temporary = `${filename}.tmp-${process.pid}`;
  try {
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { flag: 'wx' });
    fs.renameSync(temporary, filename);
  } catch {
    try { fs.unlinkSync(temporary); } catch { /* Fixed diagnostics only. */ }
    throw new MergeError('MERGE_WRITE_FAILED');
  }
}

function main(argv = process.argv.slice(2)) {
  try {
    const args = { parts: '.cache/parts', data: 'web/data', catalog: 'web/data/catalog.json', config: 'config/universe.json' };
    for (let index = 0; index < argv.length; index += 2) {
      const key = argv[index].slice(2);
      if (!argv[index].startsWith('--') || !Object.hasOwn(args, key) || !argv[index + 1]) throw new MergeError('INVALID_ARGUMENTS');
      args[key] = argv[index + 1];
    }
    const catalog = readJson(args.catalog);
    const config = fs.existsSync(args.config) ? readJson(args.config) : {};
    const manifestFile = path.join(args.data, 'latest.json');
    const previousManifest = fs.existsSync(manifestFile) ? readJson(manifestFile) : null;
    const companyRoot = path.join(args.data, 'companies');
    const previousCompanies = {};
    if (fs.existsSync(companyRoot)) {
      for (const entry of fs.readdirSync(companyRoot, { withFileTypes: true })) {
        if (entry.isFile() && entry.name.endsWith('.json') && COMPANY.test(entry.name.slice(0, -5))) {
          previousCompanies[entry.name.slice(0, -5)] = readJson(path.join(companyRoot, entry.name));
        }
      }
    }
    const result = mergeSnapshots({ snapshots: partSnapshots(args.parts), previousCompanies, previousManifest, catalog, config });
    if (result.changed) {
      for (const [companyCode, envelope] of Object.entries(result.companySnapshots)) {
        writeAtomic(path.join(companyRoot, `${companyCode}.json`), envelope);
      }
      writeAtomic(manifestFile, result.manifest);
    }
    process.stdout.write(`${JSON.stringify({ status: result.changed ? 'updated' : 'unchanged', ...result.counts,
      errorCodes: result.errors })}\n`);
    return 0;
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ status: 'failed', errorCode: fixedErrors.has(error?.code) ? error.code : 'MERGE_FAILED' })}\n`);
    return 1;
  }
}

if (require.main === module) process.exitCode = main();

module.exports = { MergeError, main, mergeSnapshots, sanitizeFund };
