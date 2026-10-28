#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { diagnose: diagnoseMerge } = require('../lib/merge-diagnostics');
const { buildSnapshot, catalogDetails, deduplicatePortfolios, publicUrl } = require('./build-data');
const { validDate } = require('../lib/fund-analysis');
const { verifiedReportManagers, normalizeManagers, evidenceFor, managerKey, managerCoverageFor, completeManagerCoverage } = require('../lib/report-managers');
const { strategySectionCoverageFor, strategyAnalysisFor } = require('../lib/strategy-sections');
const { retainStrategyClaims, strategyClaimsFor } = require('../lib/strategy-claims');
const strategyClaimPatches = require('../lib/strategy-claim-patches');
const { VERSION: PRICE_COVERAGE_VERSION, calendar, validCompletePrice, validUsableCompletePrice, validUsableCompleteQuarterPrice, retainedCoverage } = require('../lib/price-coverage');
const { normalizeConflict } = require('../lib/price-consistency');
const { bindingFor, validPatch, canReplacePrice, key: priceKey } = require('../lib/price-patches');
const { reuseCompletePrices } = require('../lib/reuse-prices');
const { normalizePortfolioIdentity, retainPortfolioIdentity } = require('../lib/portfolio-identity');
const { chinaDate } = require('../web/source-dates');

const CODE = /^\d{6}$/;
const COMPANY = /^[A-Za-z0-9_-]{1,40}$/;
const COMPANY_FILE = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_COMPANY_BYTES = 8_000_000;
const MAX_COMPANY_FUNDS = 100;
const timestamp = value => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
const label = (value, limit = 200) => typeof value === 'string' ? value.trim().slice(0, limit) : null;
const clone = value => JSON.parse(JSON.stringify(value));
const fixedErrors = new Set(['INVALID_CATALOG', 'INVALID_PART', 'INVALID_FUND', 'INVALID_COMPANY_SNAPSHOT',
  'CATALOG_IDENTITY_MISMATCH', 'INVALID_ARGUMENTS', 'MERGE_READ_FAILED', 'MERGE_WRITE_FAILED',
  'PORTFOLIO_IDENTITY_CONFLICT', 'TARGET_PERIOD_MISMATCH', 'COMPANY_FUND_TOO_LARGE', 'MERGE_COMPONENT_TOO_LARGE', 'MERGE_FAILED']);

class MergeError extends Error {
  constructor(code) { super(fixedErrors.has(code) ? code : 'MERGE_FAILED'); this.code = this.message; }
}

function validEnvelope(envelope, now, config) {
  return envelope && envelope.schemaVersion === 1 && envelope.sourceMode === 'actions'
    && Array.isArray(envelope.funds) && validDate(envelope.asOf)
    && envelope.asOf <= chinaDate(now) && timestamp(envelope.retrievedAt)
    && Date.parse(envelope.retrievedAt) <= now.getTime() + 300000
    && (!config.targetPeriodEnd || !envelope.targetPeriodEnd || envelope.targetPeriodEnd === config.targetPeriodEnd);
}

function recoverPrice(row, supplied, retrievedAt, now = new Date()) {
  if (!supplied || supplied.status !== 'observed' || supplied.priceBasis !== 'unadjusted'
    || !publicUrl(supplied.sourceUrl) || supplied.currency !== row.currency
    || supplied.rangeStart !== row.priceRange.rangeStart || supplied.rangeEnd !== row.priceRange.rangeEnd
    || !Number.isFinite(supplied.low) || supplied.low <= 0 || !Number.isFinite(supplied.high)
    || supplied.high < supplied.low || !Number.isInteger(supplied.observedCount) || supplied.observedCount < 1
    || !validDate(supplied.observedStart) || !validDate(supplied.observedEnd)
    || supplied.observedStart < supplied.rangeStart || supplied.observedEnd > supplied.rangeEnd
    || supplied.observedStart > supplied.observedEnd
    || Array.isArray(supplied.sourceUrls) && supplied.sourceUrls.some(url => !publicUrl(url))) return false;
  const fetchedAt = supplied.fetchedAt || retrievedAt;
  if (!timestamp(fetchedAt) || Date.parse(fetchedAt) > Date.parse(retrievedAt)) return false;
  const conflict = Object.hasOwn(supplied, 'globalPriceConflict') ? normalizeConflict(supplied.globalPriceConflict,
    { market: row.market, stockCode: row.stockCode, currency: row.currency, rangeStart: supplied.rangeStart,
      rangeEnd: supplied.rangeEnd, calendarId: calendar.id }, now) : null;
  if (Object.hasOwn(supplied, 'globalPriceConflict') && !conflict) return false;
  const coverage = retainedCoverage(supplied, row.market, row.stockCode, row.priceRange.rangeStart, row.priceRange.rangeEnd);
  row.priceRange = { ...row.priceRange, ...coverage, status: 'observed',
    label: coverage.completeness === 'complete' ? '完整交易日区间价格（非持仓成本）' : '已观测区间价格（非持仓成本；交易日覆盖未完整）', low: supplied.low, high: supplied.high,
    priceBasis: 'unadjusted', observedStart: supplied.observedStart, observedEnd: supplied.observedEnd,
    observedCount: supplied.observedCount, sourceUrl: supplied.sourceUrl, fetchedAt,
    sourceUrls: Array.isArray(supplied.sourceUrls) ? [...new Set(supplied.sourceUrls)].slice(0, 8) : [supplied.sourceUrl],
    dataStatus: supplied.dataStatus === 'cached' ? 'cached' : 'fresh' };
  if (conflict) row.priceRange.globalPriceConflict = conflict;
  return true;
}

function latestPriceTime(current, observed) {
  const observedTime = strategyClaimPatches.instant(observed);
  const currentTime = strategyClaimPatches.instant(current);
  if (observedTime === null) return current;
  return currentTime === null || observedTime > currentTime ? observed : current;
}

function sanitizeFund(item, { now, config, envelope }) {
  const exemption = item?.status === 'unavailable' && item?.notApplicableEvidence;
  if (!item || !CODE.test(item.code) || !['ok', 'no-equities', 'unavailable'].includes(item.status)
    || !['fresh', 'cached'].includes(item.dataStatus) || !validDate(item.asOf)
    || item.asOf > envelope.asOf || !timestamp(item.retrievedAt)
    || Date.parse(item.retrievedAt) > Date.parse(envelope.retrievedAt)
    || (!item.selectedReport && !exemption) || (item.status === 'unavailable' && !exemption && item.selectedReport.reportOnly !== true) || !Array.isArray(item.rows)
    || (config.targetPeriodEnd && item.selectedReport && item.selectedReport.periodEnd !== config.targetPeriodEnd)) return null;
  // Persisted snapshots already carry the canonical report history.  The
  // selected/previous pointers are often duplicate references; appending them
  // and letting the last value win can replace a complete report with a poorer
  // duplicate during a source-only normalization.  Preserve the first
  // persisted entry for each identity and only backfill a missing identity.
  const reports = [];
  const reportIndexes = new Map();
  const appendReport = report => {
    if (!report) return;
    const key = [report.periodEnd, report.kind, report.sourceUrl].join('|');
    if (reportIndexes.has(key)) return;
    reportIndexes.set(key, reports.length); reports.push(report);
  };
  for (const report of Array.isArray(item.reports) ? item.reports : []) appendReport(report);
  // The previous pointer is a fallback only; it must never overwrite the
  // persisted report history for an already-known identity.
  appendReport(item.previousReport);
  // The selected pointer may carry a newly accepted strategy patch, so it is
  // allowed to replace the canonical entry for its own identity.
  if (item.selectedReport) {
    const key = [item.selectedReport.periodEnd, item.selectedReport.kind, item.selectedReport.sourceUrl].join('|');
    if (reportIndexes.has(key)) reports[reportIndexes.get(key)] = item.selectedReport;
    else appendReport(item.selectedReport);
  }
  const cleanInput = {
    code: item.code, name: label(item.name), company: label(item.company), companyId: label(item.companyId, 40),
    managers: (Array.isArray(item.managers) ? item.managers : []).slice(0, 50)
      .map(manager => ({ name: label(manager && manager.name, 100), startDate: manager && manager.startDate,
        endDate: manager && manager.endDate })),
    metadataSourceUrl: item.metadataSourceUrl, managerSourceUrl: item.managerSourceUrl,
    inceptionDate: item.inceptionDate, inceptionSourceUrl: item.inceptionSourceUrl, inceptionEvidence: item.inceptionEvidence,
    inceptionProfileEvidence: item.inceptionProfileEvidence, inceptionConflict: item.inceptionConflict,
    managerScope: item.managerScope, managersAsOf: item.managersAsOf,
    shareClassCodes: (Array.isArray(item.shareClassCodes) ? item.shareClassCodes : []).filter(code => typeof code === 'string' && CODE.test(code)),
    sourcePortfolioCode: item.sourcePortfolioCode, portfolioSourceUrl: item.portfolioSourceUrl,
    reports, noEquitiesEvidence: item.noEquitiesEvidence, notApplicableEvidence: item.notApplicableEvidence,
  };
  let clean;
  try {
    clean = buildSnapshot({ asOf: item.asOf, retrievedAt: item.retrievedAt, funds: [cleanInput], barsByStock: {}, errors: [] },
      { now, config: { funds: [{ code: item.code }], targetPeriodEnd: config.targetPeriodEnd,
        baselinePeriodEnd: config.baselinePeriodEnd } }).funds[0];
  } catch { return null; }
  if (!clean || clean.selectedReport?.periodEnd !== item.selectedReport?.periodEnd || clean.status !== item.status) return null;
  const suppliedRows = new Map(item.rows.filter(row => row && typeof row.stockCode === 'string')
    .map(row => [`${row.market}:${row.stockCode}`, row]));
  const priceAt = timestamp(item.priceUpdatedAt) && Date.parse(item.priceUpdatedAt) <= now.getTime() + 300000
    && Date.parse(item.priceUpdatedAt) <= Date.parse(envelope.priceUpdatedAt || envelope.retrievedAt)
    ? item.priceUpdatedAt : clean.retrievedAt;
  let retainedPriceTime;
  for (const row of clean.rows) {
    const supplied = suppliedRows.get(`${row.market}:${row.stockCode}`)?.priceRange;
    if (recoverPrice(row, supplied, priceAt, now)) {
      retainedPriceTime = latestPriceTime(retainedPriceTime, row.priceRange.fetchedAt);
    }
    const conflict = normalizeConflict(supplied?.globalPriceConflict,
      { market: row.market, stockCode: row.stockCode, currency: row.currency, rangeStart: row.priceRange.rangeStart,
        rangeEnd: row.priceRange.rangeEnd, calendarId: calendar.id }, now);
    // The globally witnessed key may be present on a row with no local quote.
    if (conflict) row.priceRange.globalPriceConflict = conflict;
  }
  // A PDF refresh is not a price observation. Preserve an explicit spelling
  // only when it matches the latest quote actually retained, to microseconds.
  if (retainedPriceTime) clean.priceUpdatedAt = strategyClaimPatches.instant(item.priceUpdatedAt)
    === strategyClaimPatches.instant(retainedPriceTime) ? item.priceUpdatedAt : retainedPriceTime;
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
  if (!candidate.selectedReport) return !current.selectedReport;
  if (!current.selectedReport) return true;
  if (candidate.selectedReport.periodEnd < current.selectedReport.periodEnd) return false;
  if (candidate.selectedReport.periodEnd === current.selectedReport.periodEnd) {
    if (candidate.status !== current.status && current.status !== 'unavailable') return false;
    if (candidate.selectedReport.holdings.length < current.selectedReport.holdings.length) return false;
    if (current.selectedReport.coverage === 'full' && candidate.selectedReport.coverage !== 'full') return false;
  }
  return true;
}

function retainPrices(candidate, old, now) {
  if (!old?.selectedReport || !candidate.selectedReport || old.selectedReport.periodEnd !== candidate.selectedReport.periodEnd) return;
  const rows = new Map(old.rows.map(row => [`${row.market}:${row.stockCode}`, row]));
  let copiedPriceTime;
  for (const row of candidate.rows) {
    const prior = rows.get(`${row.market}:${row.stockCode}`)?.priceRange;
    const conflict = normalizeConflict(prior?.globalPriceConflict,
      { market: row.market, stockCode: row.stockCode, currency: row.currency, rangeStart: row.priceRange.rangeStart,
        rangeEnd: row.priceRange.rangeEnd, calendarId: calendar.id }, now);
    if (conflict) row.priceRange.globalPriceConflict = conflict;
    const complete = value => validCompletePrice(value, row.market, row.stockCode, row.priceRange.rangeStart, row.priceRange.rangeEnd);
    if (row.priceRange.status !== 'observed' || !complete(row.priceRange) && complete(prior)) {
      const bound = Date.parse(old?.priceUpdatedAt) > Date.parse(candidate.retrievedAt) ? old.priceUpdatedAt : candidate.retrievedAt;
      if (prior && recoverPrice(row, prior, bound, now)) {
        row.priceRange.dataStatus = 'cached';
        copiedPriceTime = latestPriceTime(copiedPriceTime, row.priceRange.fetchedAt);
      }
    }
  }
  if (copiedPriceTime) {
    // Preserve the original spelling of the matching clock. Do not inherit a
    // later clock that belongs only to an old security absent from this fund.
    if (strategyClaimPatches.instant(old.priceUpdatedAt) === strategyClaimPatches.instant(copiedPriceTime)) copiedPriceTime = old.priceUpdatedAt;
    candidate.priceUpdatedAt = latestPriceTime(candidate.priceUpdatedAt, copiedPriceTime);
  }
  candidate.summary.observedPriceCount = candidate.rows.filter(row => row.priceRange.status === 'observed').length;
}

// Fill independently sourced gaps without replacing a stronger holdings table.
// A recovered narrative keeps its own source, period, extraction time and PDF hash.
function retainReportEvidence(target, source, now = new Date()) {
  if (!target?.selectedReport || !source?.selectedReport || target.selectedReport.periodEnd !== source.selectedReport.periodEnd) return false;
  let changed = false;
  const to = target.selectedReport, from = source.selectedReport;
  changed = require('../lib/historical-inception').retainInception(target, source) || changed;
  changed = retainPortfolioIdentity(to, from) || changed;
  // Keep a different PDF's identity only in that report's own history. A
  // quarterly strategy supplement cannot become an interim identity source.
  const identityReports = [...(source.reports || []), from, source.previousReport].filter(Boolean)
    .filter(report => normalizePortfolioIdentity(report.portfolioIdentityEvidence, report));
  const targetReports = [...(target.reports || []), to, target.previousReport].filter(Boolean);
  const identityKey = report => [report.periodEnd, report.kind, report.sourceUrl, report.publishedAt].join('|');
  for (const report of identityReports) {
    const matching = targetReports.filter(existing => identityKey(existing) === identityKey(report));
    if (!matching.length) changed = true;
    for (const existing of matching) changed = retainPortfolioIdentity(existing, report) || changed;
  }
  const month = Number(to.periodEnd.slice(5, 7));
  const quarterStart = `${to.periodEnd.slice(0, 4)}-${String(Math.floor((month - 1) / 3) * 3 + 1).padStart(2, '0')}-01`;
  const oldManagers = verifiedReportManagers(target.reportManagers?.length ? target.reportManagers : to.managers, quarterStart, to.periodEnd);
  const newManagers = verifiedReportManagers(source.reportManagers?.length ? source.reportManagers : from.managers, quarterStart, to.periodEnd);
  const oldComplete = completeManagerCoverage(target, to.periodEnd), newComplete = completeManagerCoverage(source, to.periodEnd);
  const preferComplete = newComplete && (!oldComplete || source.managerTableCoverage.kind === 'quarterly'
    && target.managerTableCoverage.kind !== 'quarterly');
  const partialUnion = !oldComplete && !newComplete && newManagers.length > 0;
  const united = partialUnion ? [...new Map([...oldManagers, ...newManagers].map(item => [managerKey(item), item])).values()] : newManagers;
  if (newManagers.length && (preferComplete || !oldManagers.length || partialUnion && united.length > oldManagers.length)) {
    const managerReport = [from, ...(source.reports || [])].find(report => report.periodEnd === to.periodEnd
      && report.managerTableCoverage?.sourceUrl === source.managerTableCoverage?.sourceUrl
      && report.managerTableCoverage?.sourceTextHash === source.managerTableCoverage?.sourceTextHash
      && managerCoverageFor(report.managerTableCoverage, normalizeManagers(report.managers, to.periodEnd), to.periodEnd)?.complete);
    const allRows = preferComplete ? normalizeManagers((managerReport || from).managers, to.periodEnd).filter(item => evidenceFor(item, to.periodEnd)) : united;
    to.managers = clone(allRows); to.managerSourceUrl = united[0].evidence.sourceUrl;
    to.managerTableCoverage = managerCoverageFor(source.managerTableCoverage || from.managerTableCoverage, allRows, to.periodEnd);
    target.reportManagers = clone(united); target.managerAttribution = 'report';
    target.managerSourceUrl = united[0].evidence.sourceUrl;
    target.managerTableCoverage = clone(to.managerTableCoverage);
    target.managerCoverageStatus = to.managerTableCoverage?.complete ? 'complete' : 'partial'; changed = true;
    to.managerEvidenceRetrievedAt = (managerReport || from).managerEvidenceRetrievedAt || source.managerEvidenceRetrievedAt || source.retrievedAt;
    target.managerEvidenceRetrievedAt = to.managerEvidenceRetrievedAt;
  }
  if (Date.parse(source.retrievedAt) > Date.parse(target.retrievedAt) && from.holdingsIncomplete === true) {
    to.holdingsIncomplete = true;
    to.holdingsExcludedRowCount = Math.max(to.holdingsExcludedRowCount || 0, from.holdingsExcludedRowCount || 0);
    to.holdingsIncompleteReason = from.holdingsIncompleteReason;
    changed = true;
  }
  const oldStrategy = strategySectionCoverageFor(to.strategySectionCoverage, to.periodEnd);
  const newStrategy = strategySectionCoverageFor(from.strategySectionCoverage, to.periodEnd);
  const completeStrategy = proof => proof?.complete === true && proof.kind === 'quarterly' && proof.periodStart === quarterStart;
  if (newStrategy && (completeStrategy(newStrategy) && !completeStrategy(oldStrategy)
      || !oldStrategy && newStrategy.periodStart === quarterStart)) {
    to.strategySectionCoverage = clone(newStrategy);
    to.strategyAnalysis = strategyAnalysisFor(from.strategyAnalysis, newStrategy);
    to.strategyEvidenceRetrievedAt = from.strategyEvidenceRetrievedAt || source.retrievedAt;
    for (const key of ['strategyExcerpts', 'strategyThemes', 'narrative']) if (from[key] !== undefined) to[key] = clone(from[key]);
    target.narrative = clone(to.narrative); changed = true;
  }
  if (retainStrategyClaims(to, from, { code: target.code, now, asOf: target.asOf })) {
    target.narrative = clone(to.narrative); changed = true;
  }
  if (!to.narrative?.text && from.narrative?.text) {
    to.narrative = clone(from.narrative); target.narrative = clone(from.narrative); changed = true;
    for (const key of ['parseMethod', 'parseErrorCode', 'sourceTextHash', 'pageStats', 'strategyExcerpts', 'strategyThemes',
      'reportPdfSourceUrl', 'reportPdfPeriodEnd', 'reportPdfKind']) if (from[key] !== undefined) to[key] = clone(from[key]);
    to.evidenceRetrievedAt = source.retrievedAt;
  } else if ((!to.pageStats?.textStatus || to.pageStats.truncated) && from.pageStats?.textStatus === 'usable' && !from.pageStats.truncated
    && to.sourceUrl === from.sourceUrl) {
    for (const key of ['parseMethod', 'parseErrorCode', 'sourceTextHash', 'pageStats', 'strategyExcerpts', 'strategyThemes',
      'reportPdfSourceUrl', 'reportPdfPeriodEnd', 'reportPdfKind']) if (from[key] !== undefined) to[key] = clone(from[key]);
    to.evidenceRetrievedAt = source.retrievedAt; changed = true;
  }
  if (!(to.navYuan > 0) && from.navYuan > 0 && publicUrl(from.navSourceUrl || from.sourceUrl)) {
    to.navYuan = from.navYuan; to.navSourceUrl = from.navSourceUrl || from.sourceUrl;
    to.navRetrievedAt = source.retrievedAt; changed = true;
  }
  if (!target.previousReport && source.previousReport) { target.previousReport = clone(source.previousReport); changed = true; }
  if (changed) {
    const key = r => [r.periodEnd, r.kind, r.sourceUrl].join('|');
    target.reports = [...new Map([...(target.reports || []), ...(source.reports || []), target.previousReport, to].filter(Boolean).map(r => [key(r), r])).values()];
    for (const report of target.reports) {
      for (const original of [...identityReports, ...targetReports]) retainPortfolioIdentity(report, original);
    }
  }
  return changed;
}

function coverageFor(funds, directory, config, expectedCount = directory.fundCount) {
  return {
    targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
    fundCount: funds.length, companyCount: new Set(funds.map(fund => fund.companyId)).size,
    scope: '按公开目录逐家处理，展示已取得的有效报告；已披露数据与目录规模分别计数',
    requestedFundCount: expectedCount, catalogFundCount: directory.fundCount, catalogCompanyCount: directory.companyCount,
    holdingCount: funds.reduce((sum, fund) => sum + fund.rows.filter(row => row.current).length, 0),
    priceAvailable: funds.reduce((sum, fund) => sum + fund.rows.filter(row => row.current && row.priceRange.status === 'observed').length, 0),
    priceComplete: funds.reduce((sum, fund) => sum + fund.rows.filter(row => row.current && validUsableCompleteQuarterPrice(row.priceRange,
      row.market, row.stockCode, config.targetPeriodEnd || fund.selectedReport?.periodEnd)).length, 0),
    narrativeCount: funds.filter(fund => fund.narrative?.text).length,
    noEquitiesFundCount: funds.filter(fund => fund.status === 'no-equities').length,
    notApplicableFundCount: funds.filter(fund => fund.notApplicableEvidence).length,
    establishedAfterPeriodFundCount: funds.filter(fund => fund.notApplicableEvidence?.reason === 'ESTABLISHED_AFTER_PERIOD').length,
    terminatedBeforePeriodFundCount: funds.filter(fund => fund.notApplicableEvidence?.reason === 'TERMINATED_BEFORE_PERIOD').length,
    reportOnlyFundCount: funds.filter(fund => fund.selectedReport?.reportOnly).length,
    catalogStatus: directory.known ? 'available' : 'unavailable',
    limitations: ['目录总数不代表已完成采集。', '股票为零仅依据报告未持股说明或经核验的期末股票账面金额，保留各证据的适用范围。',
      '区间最高最低价不是实际持仓成本。', '同组合仅凭公开主代码和份额证据去重，份额金额不叠加。'],
  };
}

function observationDates(funds) {
  return { asOf: funds.length ? funds.map(fund => fund.asOf).sort().at(-1) : null,
    priceUpdatedAt: funds.map(fund => fund.priceUpdatedAt).filter(timestamp).sort((a, b) => Date.parse(a) - Date.parse(b)).at(-1) || null,
    retrievedAt: funds.length ? [...funds].sort((a, b) => Date.parse(b.retrievedAt) - Date.parse(a.retrievedAt))[0].retrievedAt : null };
}

function splitCompanyFunds(funds, makeEnvelope, { maxBytes = MAX_COMPANY_BYTES, maxFunds = MAX_COMPANY_FUNDS } = {}) {
  const pages = [];
  let page = [];
  const overhead = Buffer.byteLength(JSON.stringify(makeEnvelope([]))) + 1024;
  let bytes = overhead;
  for (const fund of funds) {
    const size = Buffer.byteLength(JSON.stringify(fund)) + 1;
    if (page.length && (page.length >= maxFunds || bytes + size > maxBytes)) {
      pages.push(page);
      page = [];
      bytes = overhead;
    }
    page.push(fund);
    bytes += size;
    if (bytes > maxBytes) throw new MergeError('COMPANY_FUND_TOO_LARGE');
  }
  if (page.length) pages.push(page);
  if (pages.some(items => Buffer.byteLength(JSON.stringify(makeEnvelope(items))) + 1 > maxBytes)) throw new MergeError('COMPANY_FUND_TOO_LARGE');
  return pages;
}

function finalObservation(fund) {
  if (fund.notApplicableEvidence && !fund.selectedReport) return {
    representativeCode: fund.code, companyCode: fund.companyId, status: 'not-applicable',
    notApplicableEvidence: clone(fund.notApplicableEvidence), observedAt: fund.retrievedAt,
    shareClassCodes: [fund.code], sourcePortfolioCode: null, portfolioSourceUrl: null,
  };
  const stocks = new Map(fund.selectedReport.holdings.map(item => [`${item.market}:${item.stockCode}`,
    { stockCode: item.stockCode, market: item.market }]));
  const period = fund.selectedReport.periodEnd;
  const start = `${period.slice(0, 4)}-${String(Math.floor((Number(period.slice(5, 7)) - 1) / 3) * 3 + 1).padStart(2, '0')}-01`;
  const baseline = new Date(Date.parse(`${start}T00:00:00Z`) - 86400000).toISOString().slice(0, 10);
  if (fund.previousReport?.periodEnd === baseline && publicUrl(fund.previousReport.sourceUrl)) {
    for (const row of fund.rows) if (row.previous) stocks.set(`${row.market}:${row.stockCode}`, { stockCode: row.stockCode, market: row.market });
  }
  const observed = new Set(fund.rows.filter(row => stocks.has(`${row.market}:${row.stockCode}`) && row.priceRange.status === 'observed'
    && row.priceRange.rangeStart === start && row.priceRange.rangeEnd === period)
    .map(row => `${row.market}:${row.stockCode}`));
  const complete = new Set(fund.rows.filter(row => stocks.has(`${row.market}:${row.stockCode}`) && validUsableCompletePrice(row.priceRange, row.market, row.stockCode, start, period))
    .map(row => `${row.market}:${row.stockCode}`));
  const conflicts = new Set(fund.rows.filter(row => stocks.has(`${row.market}:${row.stockCode}`) && normalizeConflict(row.priceRange.globalPriceConflict,
    { market: row.market, stockCode: row.stockCode, currency: row.currency, rangeStart: start, rangeEnd: period, calendarId: calendar.id }))
    .map(row => `${row.market}:${row.stockCode}`));
  const conflictingStocks = [...conflicts].sort().map(key => stocks.get(key));
  const missingStocks = [...stocks.keys()].sort().filter(key => !complete.has(key) && !conflicts.has(key)).map(key => stocks.get(key));
  const reportEvidence = { sourceUrl: fund.selectedReport.sourceUrl, periodEnd: fund.selectedReport.periodEnd,
    publishedAt: fund.selectedReport.publishedAt, observedAt: fund.retrievedAt };
  if (fund.status === 'no-equities') reportEvidence.noEquities = clone(fund.noEquitiesEvidence);
  return { representativeCode: fund.code, companyCode: fund.companyId,
    status: fund.status === 'no-equities' ? 'no-equities' : fund.status === 'ok' ? 'complete' : 'failed', reportEvidence,
    priceCoverage: { coverageVersion: PRICE_COVERAGE_VERSION, calendarId: calendar.id,
      consistencyVersion: 1, holdingCount: stocks.size, priceAvailable: observed.size, priceComplete: complete.size,
      priceConflicted: conflicts.size, conflictingStocks, missingStocks,
      holdingsIdentity: crypto.createHash('sha256').update([...stocks.keys()].sort().join('|')).digest('hex') },
    sourcePortfolioCode: fund.sourcePortfolioCode, portfolioSourceUrl: fund.portfolioSourceUrl,
    shareClassCodes: fund.sourcePortfolioCode && publicUrl(fund.portfolioSourceUrl) ? fund.shareClassCodes : [fund.code] };
}

function mergeReceipt(funds, codes, config, now, attempts = new Map()) {
  const observations = new Map();
  for (const fund of funds) {
    const item = finalObservation(fund);
    for (const code of item.shareClassCodes) if (codes.has(code)) observations.set(code, { code, ...item });
  }
  for (const [code, attempt] of attempts) {
    if (attempt.attempted === false) observations.set(code, { code, ...attempt });
    else if (observations.has(code)) observations.get(code).attempted = true;
  }
  return { schemaVersion: 1, phase: { targetPeriodEnd: config.targetPeriodEnd, baselinePeriodEnd: config.baselinePeriodEnd },
    generatedAt: now.toISOString(), requestedCodes: [...codes].sort(), funds: [...observations.values()].sort((a, b) => a.code.localeCompare(b.code)) };
}

function portfolioIdentity(fund) {
  if (!fund || !CODE.test(fund.code || '') || !CODE.test(fund.sourcePortfolioCode || '')
    || !publicUrl(fund.portfolioSourceUrl) || !Array.isArray(fund.shareClassCodes)
    || !fund.shareClassCodes.includes(fund.code) || !fund.shareClassCodes.includes(fund.sourcePortfolioCode)) return null;
  return {
    representative: fund.sourcePortfolioCode,
    sourceUrl: fund.portfolioSourceUrl,
    aliases: new Set([fund.code, fund.sourcePortfolioCode, ...fund.shareClassCodes.filter(code => CODE.test(code || ''))]),
  };
}

function portfolioAliasSet(fund) {
  const aliases = new Set();
  for (const code of [fund?.code, fund?.sourcePortfolioCode, ...(Array.isArray(fund?.shareClassCodes) ? fund.shareClassCodes : [])]) {
    if (CODE.test(code || '')) aliases.add(code);
  }
  return aliases;
}

function preserveUnrequestedPortfolioIdentity(funds, previousByCode, requestedCodes) {
  const groups = new Map();
  const ambiguous = new Set();
  for (const old of previousByCode instanceof Map ? previousByCode.values() : []) {
    const identity = portfolioIdentity(old);
    if (!identity) continue;
    const prior = groups.get(identity.representative);
    if (!prior) groups.set(identity.representative, { ...identity, fund: old });
    else if (prior.sourceUrl !== identity.sourceUrl || JSON.stringify([...prior.aliases].sort())
      !== JSON.stringify([...identity.aliases].sort())) ambiguous.add(identity.representative);
  }
  const byAlias = new Map();
  for (const [representative, group] of groups) {
    if (ambiguous.has(representative)) continue;
    for (const code of group.aliases) {
      if (!byAlias.has(code)) byAlias.set(code, new Set());
      byAlias.get(code).add(representative);
    }
  }
  const requested = requestedCodes instanceof Set ? requestedCodes : new Set(requestedCodes || []);
  for (const fund of funds) {
    const current = portfolioIdentity(fund);
    const currentAliases = current?.aliases || portfolioAliasSet(fund);
    if (!currentAliases.size || [...currentAliases].some(code => requested.has(code))) continue;
    // An unrelated code may have had no confirmed portfolio identity at all
    // in the persisted snapshot.  A fresh requested observation can still
    // make the deduplicator attach a URL/alias group beside it.  With no
    // requested alias in the resulting group, preserve the exact old spelling
    // (including an absent identity) before the admission guard compares the
    // source hash.  Otherwise a source-only recovery would rewrite an
    // unrelated product merely because the new group was normalized globally.
    const exactPrevious = previousByCode instanceof Map ? previousByCode.get(fund.code) : null;
    if (exactPrevious && !portfolioIdentity(exactPrevious)) {
      if (Object.hasOwn(exactPrevious, 'sourcePortfolioCode')) fund.sourcePortfolioCode = exactPrevious.sourcePortfolioCode;
      else delete fund.sourcePortfolioCode;
      if (Object.hasOwn(exactPrevious, 'portfolioSourceUrl')) fund.portfolioSourceUrl = exactPrevious.portfolioSourceUrl;
      else delete fund.portfolioSourceUrl;
      if (Array.isArray(exactPrevious.shareClassCodes)) fund.shareClassCodes = [...exactPrevious.shareClassCodes];
      else delete fund.shareClassCodes;
      if (Array.isArray(exactPrevious.aliases)) fund.aliases = clone(exactPrevious.aliases);
      else delete fund.aliases;
      continue;
    }
    const matches = new Set();
    for (const code of currentAliases) for (const representative of byAlias.get(code) || []) matches.add(representative);
    if (matches.size !== 1) continue;
    const representative = [...matches][0];
    const group = groups.get(representative);
    if (!group || [...group.aliases].some(code => requested.has(code))) continue;
    // A source-only recovery must not rewrite an unrelated persisted portfolio
    // identity merely because a fresh group was deduplicated beside it.
    fund.sourcePortfolioCode = group.representative;
    fund.portfolioSourceUrl = group.sourceUrl;
    fund.shareClassCodes = [...group.aliases].sort();
    if (Array.isArray(group.fund.aliases)) fund.aliases = clone(group.fund.aliases);
  }
  return funds;
}

function mergeSnapshots({ snapshots = [], previousCompanies = {}, previousManifest = null, catalog,
  config = {}, now = new Date(), receiptCodes = [], pageLimits, reusePrices = true, onSourceFunds = null } = {}) {
  const currentTime = now instanceof Date ? now : new Date(now);
  if (!Number.isFinite(currentTime.getTime())) throw new MergeError('MERGE_FAILED');
  let directory;
  try { directory = catalogDetails(catalog); } catch { throw new MergeError('INVALID_CATALOG'); }
  if (!directory.known) throw new MergeError('INVALID_CATALOG');
  const directoryMap = new Map(directory.funds.map(fund => [fund.code, fund]));
  const byCode = new Map();
  const errors = [];
  const addError = code => errors.push(fixedErrors.has(code) ? code : 'INVALID_FUND');
  const requestedCodes = new Set(receiptCodes.filter(code => typeof code === 'string' && CODE.test(code)));
  const attempts = new Map();
  const priceResults = [];
  for (const snapshot of snapshots) {
    for (const patch of snapshot?.pricePatches || []) if (CODE.test(patch?.code || '')) requestedCodes.add(patch.code);
    for (const receipt of snapshot?.strategyClaimsReceipts || []) if (CODE.test(receipt?.code || '')) requestedCodes.add(receipt.code);
    for (const item of [...(snapshot?.batchResults || []), ...(snapshot?.funds || [])]) if (CODE.test(item?.code || '')) requestedCodes.add(item.code);
    if (!validEnvelope(snapshot, currentTime, config)) continue;
    const freshCodes = new Set(snapshot.funds.filter(item => item?.dataStatus === 'fresh').map(item => item.code));
    for (const item of snapshot.batchResults || []) {
      if (!CODE.test(item?.code || '') || !directoryMap.has(item.code)) continue;
      const deferred = item.status === 'deferred-budget' && item.attempted === false
        && item.errorCode === 'BATCH_DEADLINE_REACHED' && !freshCodes.has(item.code);
      if (deferred && attempts.get(item.code)?.attempted !== true) {
        attempts.set(item.code, { status: 'deferred-budget', attempted: false, errorCode: 'BATCH_DEADLINE_REACHED',
          companyCode: directoryMap.get(item.code).companyId || 'unknown' });
      } else if (!deferred) attempts.set(item.code, { attempted: true });
    }
    for (const code of freshCodes) attempts.set(code, { attempted: true });
  }
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
    if (!COMPANY_FILE.test(companyCode) || !validEnvelope(envelope, currentTime, config)) { addError('INVALID_COMPANY_SNAPSHOT'); continue; }
    for (const item of envelope.funds) {
      let fund = sanitizeFund(item, { now: currentTime, config, envelope });
      if (!fund) { addError('INVALID_FUND'); continue; }
      const actual = mapCompany(fund, directoryMap);
      if (!actual) { addError('CATALOG_IDENTITY_MISMATCH'); continue; }
      if (!byCode.has(fund.code) || Date.parse(fund.retrievedAt) > Date.parse(byCode.get(fund.code).retrievedAt)) byCode.set(fund.code, fund);
    }
  }
  const previousByCode = new Map([...byCode.entries()].map(([code, fund]) => [code, clone(fund)]));
  let acceptedFreshFundCount = 0;
  for (const envelope of snapshots) {
    if (!validEnvelope(envelope, currentTime, config)) { addError('INVALID_PART'); continue; }
    for (const item of envelope.funds) {
      if (item?.dataStatus !== 'fresh') continue;
      let fund = sanitizeFund(item, { now: currentTime, config, envelope });
      if (!fund) { addError('INVALID_FUND'); continue; }
      if (!mapCompany(fund, directoryMap)) { addError('CATALOG_IDENTITY_MISMATCH'); continue; }
      const old = byCode.get(fund.code);
      if (!shouldReplace(old, fund)) {
        if (old && retainReportEvidence(old, fund, currentTime)) {
          const rebuilt = sanitizeFund(old, { now: currentTime, config,
            envelope: { asOf: old.asOf, retrievedAt: old.retrievedAt, priceUpdatedAt: old.priceUpdatedAt } });
          if (rebuilt) { byCode.set(old.code, rebuilt); acceptedFreshFundCount += 1; }
        }
        continue;
      }
      if (retainReportEvidence(fund, old, currentTime)) {
        fund = sanitizeFund(fund, { now: currentTime, config, envelope });
        if (!fund) continue;
      }
      retainPrices(fund, old, currentTime);
      byCode.set(fund.code, fund);
      acceptedFreshFundCount += 1;
    }
  }
  // A failed PDF attempt can still expose a real founding-date disagreement.
  // Apply only that check to an existing exact-code/quarter observation.
  let acceptedInceptionPatchCount = 0;
  for (const envelope of snapshots) {
    if (!validEnvelope(envelope, currentTime, config)) continue;
    for (const raw of Array.isArray(envelope.inceptionPatches) ? envelope.inceptionPatches : []) {
      const patch = require('../lib/historical-inception').normalizeInceptionPatch(raw, config, currentTime, envelope.retrievedAt);
      const fund = patch && byCode.get(patch.code);
      if (!fund || fund.selectedReport?.periodEnd !== patch.targetPeriodEnd) continue;
      const changed = require('../lib/historical-inception').retainInception(fund, {
        code: patch.code, inceptionDate: patch.historicalEvidence.date, inceptionSourceUrl: patch.historicalEvidence.sourceUrl,
        inceptionEvidence: patch.historicalEvidence, inceptionProfileEvidence: patch.profileEvidence,
      }, currentTime);
      if (changed) acceptedInceptionPatchCount++;
    }
  }
  let acceptedPricePatchCount = 0;
  for (const envelope of snapshots) {
    if (!validEnvelope(envelope, currentTime, config)) continue;
    for (const patch of Array.isArray(envelope.pricePatches) ? envelope.pricePatches : []) {
      if (!CODE.test(patch?.code || '') || !directoryMap.has(patch.code)) continue;
      const fund = byCode.get(patch.code);
      const matches = validPatch(patch, config, currentTime) && fund
        && JSON.stringify(bindingFor(fund, config)) === JSON.stringify(patch.binding);
      if (!matches) {
        priceResults.push({ code: patch.code, binding: patch.binding, accepted: false,
          errorCode: patch.errorCode === 'PRICE_BUDGET_REACHED' ? 'PRICE_BUDGET_REACHED' : 'PRICE_BINDING_INVALID' });
        continue;
      }
      const rows = new Map(fund.rows.map(row => [priceKey(row), row]));
      let adopted = 0;
      for (const update of patch.prices) {
        const row = rows.get(priceKey(update));
        if (!row || !canReplacePrice(row.priceRange, update.priceRange, row, config)) continue;
        const before = JSON.stringify(row.priceRange);
        if (recoverPrice(row, update.priceRange, envelope.retrievedAt) && JSON.stringify(row.priceRange) !== before) {
          fund.priceUpdatedAt = [fund.priceUpdatedAt, row.priceRange.fetchedAt].filter(timestamp).sort((a, b) => Date.parse(a) - Date.parse(b)).at(-1);
          adopted++;
        }
      }
      fund.summary.observedPriceCount = fund.rows.filter(row => row.priceRange.status === 'observed').length;
      if (adopted) acceptedPricePatchCount++;
      priceResults.push({ code: patch.code, binding: patch.binding, accepted: true, errorCode: null });
    }
  }
  let acceptedStrategyClaimsPatchCount = 0;
  const strategyResults = [], strategyEvents = new Map();
  for (const envelope of snapshots) {
    if (!validEnvelope(envelope, currentTime, config)) continue;
    for (const rawReceipt of Array.isArray(envelope.strategyClaimsReceipts) ? envelope.strategyClaimsReceipts : []) {
      const tasks = (Array.isArray(envelope.strategyClaimsTasks) ? envelope.strategyClaimsTasks : []).filter(task => task?.code === rawReceipt?.code);
      if (tasks.length !== 1 || !directoryMap.has(rawReceipt?.code)) continue;
      const task = strategyClaimPatches.normalizeTask(tasks[0], config, rawReceipt.code, currentTime);
      const receipt = task && strategyClaimPatches.normalizeReceipt(rawReceipt, task, config, envelope.retrievedAt, currentTime, envelope.strategyClaimsPlannedAt);
      if (!receipt) continue;
      const eventKey = strategyClaimPatches.canonical([receipt.code, receipt.taskDigest, receipt.attemptedAt]);
      const patches = (Array.isArray(envelope.strategyClaimsPatches) ? envelope.strategyClaimsPatches : [])
        .filter(item => item?.code === receipt.code).map(item => strategyClaimPatches.normalizePatch(item, task, config,
          envelope.retrievedAt, currentTime, envelope.strategyClaimsPlannedAt, receipt)).filter(Boolean);
      const prior = strategyEvents.get(eventKey);
      if (prior) {
        if (strategyClaimPatches.canonical([prior.task, prior.receipt]) !== strategyClaimPatches.canonical([task, receipt])) prior.conflict = true;
        prior.patches.push(...patches);
      } else strategyEvents.set(eventKey, { task, receipt, patches, conflict: false });
    }
  }
  const sourceFunds = [...byCode.values()];
  if (onSourceFunds) onSourceFunds(sourceFunds);
  const funds = deduplicatePortfolios(sourceFunds, addError);
  preserveUnrequestedPortfolioIdentity(funds, previousByCode, requestedCodes);
  // Patch the final representative so a valid alias request cannot disappear
  // when its share class is subsequently collapsed into the same portfolio.
  for (const event of strategyEvents.values()) {
    const { task, receipt } = event;
    const result = { ...receipt, accepted: false, scanned: false, mergeErrorCode: null };
    strategyResults.push(result);
    if (event.conflict) { result.mergeErrorCode = 'STRATEGY_CLAIMS_RECEIPT_CONFLICT'; continue; }
    const exact = funds.find(fund => fund.code === task.code);
    const possible = exact ? [exact] : funds.filter(fund =>
      strategyClaimPatches.taskFor(fund, config, currentTime, { code: task.code, eligibleOnly: false }));
    const fund = possible.length === 1 ? possible[0] : null;
    const retained = fund && strategyClaimPatches.taskFor(fund, config, currentTime, { code: task.code, eligibleOnly: false });
    if (!retained || strategyClaimPatches.canonical(retained) !== strategyClaimPatches.canonical(task)) {
      result.mergeErrorCode = 'STRATEGY_CLAIMS_BINDING_CHANGED'; continue;
    }
    const existing = strategyClaimsFor(fund.selectedReport, { code: task.code, now: currentTime, asOf: fund.asOf });
    result.scanned = existing?.source.digest === task.analysisDigest;
    if (receipt.outcome !== 'parsed') continue;
    const patches = [...new Map(event.patches.map(patch => [strategyClaimPatches.canonical(patch), patch])).values()];
    if (patches.length !== 1) { result.mergeErrorCode = 'STRATEGY_CLAIMS_PATCH_REJECTED'; continue; }
    const candidate = clone(fund);
    const changed = retainStrategyClaims(candidate.selectedReport, patches[0].report, { code: task.code, now: currentTime, asOf: candidate.asOf });
    if (!changed) { result.accepted = result.scanned; continue; }
    candidate.narrative = clone(candidate.selectedReport.narrative);
    const rebuilt = sanitizeFund(candidate, { now: currentTime, config,
      envelope: { asOf: fund.asOf, retrievedAt: fund.retrievedAt, priceUpdatedAt: fund.priceUpdatedAt } });
    const admitted = rebuilt && strategyClaimsFor(rebuilt.selectedReport, { code: task.code, now: currentTime, asOf: rebuilt.asOf });
    if (!admitted || admitted.source.digest !== task.analysisDigest) {
      result.mergeErrorCode = 'STRATEGY_CLAIMS_PATCH_REJECTED'; continue;
    }
    funds[funds.indexOf(fund)] = rebuilt; result.accepted = true; result.scanned = true;
    acceptedStrategyClaimsPatchCount++;
  }
  const { mutatedFunds: reusedFunds, ...reuseCounts } = reusePrices ? reuseCompletePrices(funds,
    { phase: config, now: currentTime, recoverPrice, sourceFunds }) : {
      mutatedFunds: [], reusedPriceRowCount: 0, reusedPriceFundCount: 0, eligiblePriceKeyCount: 0,
      conflictingPriceKeyCount: 0, partialConflictRowCount: 0, conflictMarkedRowCount: 0, conflictMarkedFundCount: 0, conflictingPriceRowCount: 0 };
  const receipt = mergeReceipt(funds, requestedCodes, config, currentTime, attempts);
  receipt.pricePatches = priceResults;
  receipt.strategyClaims = strategyResults;
  // No new observation means no fabricated new timestamp or overwritten old files.
  const needsRepartition = Object.values(previousCompanies).some(item => item.funds?.length > (pageLimits?.maxFunds || MAX_COMPANY_FUNDS)
    || Buffer.byteLength(JSON.stringify(item)) + 1 > (pageLimits?.maxBytes || MAX_COMPANY_BYTES));
  if (!acceptedFreshFundCount && !acceptedPricePatchCount && !acceptedInceptionPatchCount && !acceptedStrategyClaimsPatchCount && !reusedFunds.length && !migratedBootstrapFundCount && !needsRepartition && previousManifest) {
    return { changed: false, manifest: previousManifest, companySnapshots: {},
      receipt, counts: { acceptedFreshFundCount: 0, acceptedPricePatchCount: 0, acceptedInceptionPatchCount: 0, acceptedStrategyClaimsPatchCount: 0, ...reuseCounts,
        errorCount: errors.length }, errors: [...new Set(errors)] };
  }
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
    const makeEnvelope = page => ({ schemaVersion: 1, ...observationDates(page), sourceMode: 'actions',
      targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
      company: { code: companyCode, name: companyName },
      coverage: coverageFor(page, directory, config, expected), funds: page, errors: [] });
    const pages = splitCompanyFunds(companyFunds, makeEnvelope, pageLimits);
    const companyHoldingCount = coverageFor(companyFunds, directory, config, expected).holdingCount;
    for (const [index, page] of pages.entries()) {
      const key = pages.length === 1 ? companyCode : `${companyCode}-part${index + 1}`;
      companySnapshots[key] = makeEnvelope(page);
      companyFiles.push({ companyCode, companyName, path: `./data/companies/${key}.json`,
        partIndex: index + 1, partCount: pages.length, companyFundCount: companyFunds.length, companyHoldingCount,
        fundCount: page.length, holdingCount: companySnapshots[key].coverage.holdingCount,
        managers: [...new Set(page.flatMap(fund => (fund.reportManagers?.length ? fund.reportManagers : fund.managers)
          .map(manager => manager.name)))].sort() });
    }
  }
  const availableCodes = new Set(funds.filter(fund => ['ok', 'no-equities'].includes(fund.status)).flatMap(fund => fund.sourcePortfolioCode && publicUrl(fund.portfolioSourceUrl)
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
  return { changed: true, manifest, companySnapshots, receipt,
    counts: { acceptedFreshFundCount, acceptedPricePatchCount, acceptedInceptionPatchCount, acceptedStrategyClaimsPatchCount, ...reuseCounts, migratedBootstrapFundCount, companyCount: buckets.size,
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

function statusCodes(root) {
  const codes = new Set();
  if (!fs.existsSync(root)) return [];
  const visit = (directory, depth) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue;
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory() && depth < 2) visit(filename, depth + 1);
      else if (entry.isFile() && entry.name === 'status.json') {
        try { for (const item of readJson(filename).funds || []) if (CODE.test(item?.code || '')) codes.add(item.code); } catch { /* A missing part has no observation. */ }
      }
    }
  };
  visit(root, 0);
  return [...codes];
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

function mergeFiles(options) {
  return require('./merge-storage').mergeOnDisk({ ...options,
    reuseAcrossComponents: options.reuseAcrossComponents !== false,
    receiptCodes: options.receiptCodes || statusCodes(options.parts), engine: mergeSnapshots,
    helpers: { readJson, validEnvelope, coverageFor, observationDates, recoverPrice, writeAtomic, finalObservation,
      publicUrl, catalogDetails, maxCompanyBytes: MAX_COMPANY_BYTES, maxCompanyFunds: MAX_COMPANY_FUNDS } });
}

function main(argv = process.argv.slice(2)) {
  try {
    const args = { parts: '.cache/parts', data: 'web/data', catalog: 'web/data/catalog.json', config: 'config/universe.json', reuseMarketPrices: 'true' };
    for (let index = 0; index < argv.length; index += 2) {
      const key = argv[index].slice(2);
      if (!argv[index].startsWith('--') || !Object.hasOwn(args, key) || !argv[index + 1]
        || key === 'reuseMarketPrices' && !['true', 'false'].includes(argv[index + 1])) throw new MergeError('INVALID_ARGUMENTS');
      args[key] = argv[index + 1];
    }
    const catalog = readJson(args.catalog);
    const config = fs.existsSync(args.config) ? readJson(args.config) : {};
    const manifestFile = path.join(args.data, 'latest.json');
    const previousManifest = fs.existsSync(manifestFile) ? readJson(manifestFile) : null;
    const result = mergeFiles({ ...args, reuseAcrossComponents: args.reuseMarketPrices !== 'false', catalog, config, previousManifest });
    process.stdout.write(`${JSON.stringify({ status: result.changed ? 'updated' : 'unchanged', ...result.counts,
      errorCodes: result.errors })}\n`);
    return 0;
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ status: 'failed', errorCode: fixedErrors.has(error?.code) ? error.code : 'MERGE_FAILED', diagnostic: diagnoseMerge(error) })}\n`);
    return 1;
  }
}

if (require.main === module) process.exitCode = main();

module.exports = { MergeError, main, mergeFiles, mergeSnapshots, preserveUnrequestedPortfolioIdentity,
  sanitizeFund, splitCompanyFunds };
