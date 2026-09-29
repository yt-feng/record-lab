#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { analyzeFund, normalizeReport, validDate } = require('../lib/fund-analysis');

const BUILD_CODES = new Set([
  'INVALID_INPUT', 'INVALID_AS_OF', 'FUTURE_AS_OF', 'INVALID_RETRIEVED_AT', 'INVALID_CONFIG',
  'INVALID_PREVIOUS_SNAPSHOT', 'INVALID_FUND_CODE', 'DUPLICATE_FUND_CODE', 'OUT_OF_SCOPE_FUND',
  'INVALID_REPORT', 'INVALID_REPORT_SOURCE', 'REPORT_IDENTITY_MISMATCH', 'FUTURE_REPORT',
  'EMPTY_HOLDINGS', 'NO_VALID_REPORTS', 'FUND_NOT_COLLECTED', 'OLDER_REPORT',
  'HOLDING_COVERAGE_REGRESSION', 'REPORT_COVERAGE_REGRESSION', 'INVALID_PRICE_SOURCE',
  'PRICE_IDENTITY_MISMATCH', 'NO_VALID_FRESH_FUNDS', 'COLLECTION_FAILED', 'BUILD_FAILED',
  'INVALID_ARGUMENTS', 'INPUT_READ_FAILED', 'OUTPUT_WRITE_FAILED',
  'NO_REPORTS', 'INVALID_FORMAT', 'TRUNCATED_RESPONSE', 'NO_HOLDINGS', 'INVALID_URL',
  'HTTP_REJECTED', 'RESPONSE_TOO_LARGE', 'EMPTY_RESPONSE', 'TLS_FAILED', 'INVALID_PDF',
  'PDF_PARSE_FAILED', 'UNSUPPORTED_MARKET', 'INVALID_PRICE_FORMAT', 'NO_PRICE_DATA',
  'PARSE_FAILED', 'NAV_NOT_DISCLOSED_OR_UNRESOLVED', 'NARRATIVE_UNRESOLVED', 'NO_DATED_HOLDINGS',
  'INVALID_CATALOG', 'NOT_IN_CURRENT_BATCH', 'PORTFOLIO_IDENTITY_CONFLICT',
  'INVALID_NO_EQUITIES_EVIDENCE', 'CONTRADICTORY_NO_EQUITIES',
  'TARGET_REPORT_MISSING',
]);
const STAGES = new Set(['metadata', 'announcements', 'holdings', 'report', 'nav', 'narrative',
  'join', 'prices', 'build', 'collection']);
const fundCode = value => typeof value === 'string' && /^\d{6}$/.test(value);
const clone = value => JSON.parse(JSON.stringify(value));

class SafeBuildError extends Error {
  constructor(code) {
    const safeCode = BUILD_CODES.has(code) ? code : 'BUILD_FAILED';
    super(safeCode);
    this.name = 'SafeBuildError';
    this.code = safeCode;
  }
}

function safeError(error, stage = 'build', code = null) {
  const errorCode = typeof error === 'string' ? error : error && (error.code || error.errorCode);
  return { code: fundCode(code) ? code : null, stage: STAGES.has(stage) ? stage : 'build',
    errorCode: BUILD_CODES.has(errorCode) ? errorCode : 'BUILD_FAILED' };
}

function publicUrl(value) {
  if (typeof value !== 'string' || value.length > 4096) return false;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.hash
      || !host.includes('.') || host === 'localhost' || host.endsWith('.localhost')
      || host.endsWith('.local') || host.startsWith('[')
      || /^(?:0|10|127)\./.test(host) || /^192\.168\./.test(host)
      || /^169\.254\./.test(host) || /^172\.(?:1[6-9]|2\d|3[01])\./.test(host)) return false;
    for (const key of url.searchParams.keys()) {
      if (/^(?:token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(key)) return false;
    }
    return true;
  } catch { return false; }
}

function timestamp(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
}

function reportKey(report) {
  return [report.periodEnd, report.kind, report.sourceUrl].join('|');
}

function isValidNoEq(evidence, { asOf, code } = {}) {
  if (!evidence || evidence.confirmed !== true || !validDate(asOf) || !publicUrl(evidence.sourceUrl)
    || !validDate(evidence.periodEnd) || !validDate(evidence.publishedAt)
    || evidence.periodEnd > evidence.publishedAt || evidence.publishedAt > asOf
    || !Number.isInteger(evidence.page) || evidence.page <= 0
    || typeof evidence.text !== 'string' || (evidence.fundCode !== undefined && evidence.fundCode !== code)) return false;
  const excerpt = evidence.text.slice(0, 600).replace(/\s+/g, '');
  if (/并非未|不是未|不能确认|尚未确认|无法确认/.test(excerpt)) return false;
  return /(?:未持有|未投资于?)(?:股票)(?:资产|投资|组合)?(?:[。；，,.;：:]|$)/.test(excerpt);
}

function noEquitiesReport(evidence, code) {
  const kind = ['quarterly', 'semiannual', 'annual'].includes(evidence.kind) ? evidence.kind : 'unknown';
  const normalizedEvidence = { confirmed: true, sourceUrl: evidence.sourceUrl,
    periodEnd: evidence.periodEnd, publishedAt: evidence.publishedAt, page: evidence.page,
    text: evidence.text.slice(0, 600), kind,
    title: typeof evidence.title === 'string' ? evidence.title.slice(0, 200) : null };
  return { fundCode: code, periodEnd: evidence.periodEnd, publishedAt: evidence.publishedAt,
    kind, coverage: 'full', sourceUrl: evidence.sourceUrl,
    title: normalizedEvidence.title || '报告股票持仓披露', holdings: [], navYuan: null,
    noEquitiesConfirmed: true, noEquitiesEvidence: normalizedEvidence };
}

function cleanReport(raw, code, asOf, addError) {
  if (!raw || (raw.fundCode !== undefined && raw.fundCode !== null && raw.fundCode !== code)
    || (raw.sourceFundCode !== undefined && raw.sourceFundCode !== null && raw.sourceFundCode !== code)) {
    addError('REPORT_IDENTITY_MISMATCH', code); return null;
  }
  if (!publicUrl(raw.sourceUrl) || ['officialSourceUrl', 'navSourceUrl', 'holdingsSourceUrl']
    .some(key => raw[key] !== undefined && raw[key] !== null && !publicUrl(raw[key]))) {
    addError('INVALID_REPORT_SOURCE', code); return null;
  }
  const report = normalizeReport(raw);
  if (!report) { addError('INVALID_REPORT', code); return null; }
  if (report.periodEnd > asOf || report.publishedAt > asOf) { addError('FUTURE_REPORT', code); return null; }
  if (!report.holdings.length) {
    if (!report.noEquitiesConfirmed || !isValidNoEq(raw.noEquitiesEvidence, { asOf, code })
      || raw.noEquitiesEvidence.periodEnd !== report.periodEnd
      || raw.noEquitiesEvidence.publishedAt !== report.publishedAt
      || raw.noEquitiesEvidence.sourceUrl !== report.sourceUrl) {
      addError(report.noEquitiesConfirmed ? 'INVALID_NO_EQUITIES_EVIDENCE' : 'EMPTY_HOLDINGS', code); return null;
    }
  } else if (report.noEquitiesConfirmed) { addError('CONTRADICTORY_NO_EQUITIES', code); return null; }
  if (report.holdings.some(holding => !['CN', 'HK'].includes(holding.market)
    || !/^\d{5,6}$/.test(holding.stockCode)
    || holding.currency !== (holding.market === 'CN' ? 'CNY' : 'HKD'))) {
    addError('INVALID_REPORT', code); return null;
  }
  if (report.narrative.text && !publicUrl(report.narrative.sourceUrl)) {
    addError('INVALID_REPORT_SOURCE', code); return null;
  }
  report.fundCode = code;
  for (const key of ['officialSourceUrl', 'navSourceUrl', 'holdingsSourceUrl']) {
    if (raw[key]) report[key] = raw[key];
  }
  return report;
}

function cleanBars(rawBars, addError) {
  const result = {};
  for (const [key, value] of Object.entries(rawBars && typeof rawBars === 'object' ? rawBars : {})) {
    if (!/^(?:(?:CN|HK):)?\d{5,6}$/.test(key) || !value || !publicUrl(value.sourceUrl)) {
      addError('INVALID_PRICE_SOURCE', null, 'prices'); continue;
    }
    const [market, stock] = key.includes(':') ? key.split(':') : [null, key];
    if ((value.stockCode !== undefined && value.stockCode !== stock)
      || (market && value.market !== undefined && value.market !== market)) {
      addError('PRICE_IDENTITY_MISMATCH', null, 'prices'); continue;
    }
    result[key] = {
      currency: value.currency, priceBasis: value.priceBasis, sourceUrl: value.sourceUrl,
      rows: Array.isArray(value.rows) ? value.rows.map(row => row && ({ date: row.date, low: row.low,
        high: row.high, close: row.close })) : [],
    };
  }
  return result;
}

function cachedFund(previous, reason) {
  return { ...clone(previous), dataStatus: 'cached', cacheReason: reason };
}

function sameWindow(current, previous) {
  return current.rangeStart === previous.rangeStart && current.rangeEnd === previous.rangeEnd
    && current.currency === previous.currency;
}

function preservePrices(current, previous, retrievedAt) {
  const priorRows = new Map((previous && previous.rows || []).map(row => [`${row.market}:${row.stockCode}`, row]));
  for (const row of current.rows) {
    const price = row.priceRange;
    if (price.status === 'observed') {
      price.dataStatus = 'fresh'; price.fetchedAt = retrievedAt; continue;
    }
    const priorRow = priorRows.get(`${row.market}:${row.stockCode}`);
    const prior = priorRow && priorRow.priceRange;
    if (price.status === 'missing' && previous && previous.selectedReport
      && previous.selectedReport.periodEnd === current.selectedReport.periodEnd
      && prior && prior.status === 'observed' && prior.priceBasis === 'unadjusted'
      && (price.priceBasis === null || price.priceBasis === 'unadjusted') && sameWindow(price, prior)
      && Number.isFinite(prior.low) && prior.low > 0 && Number.isFinite(prior.high) && prior.high >= prior.low
      && publicUrl(prior.sourceUrl) && timestamp(prior.fetchedAt || previous.retrievedAt)) {
      row.priceRange = { ...clone(prior), dataStatus: 'cached',
        fetchedAt: prior.fetchedAt || previous.retrievedAt, cacheReason: 'NO_PRICE_DATA' };
    } else {
      price.dataStatus = 'unavailable'; price.fetchedAt = null;
    }
  }
  current.summary.observedPriceCount = current.rows.filter(row => row.priceRange.status === 'observed').length;
}

function previousFunds(previous, asOf, addError) {
  const result = new Map();
  if (!previous) return result;
  if (previous.schemaVersion !== 1 || !Array.isArray(previous.funds)) throw new SafeBuildError('INVALID_PREVIOUS_SNAPSHOT');
  for (const item of previous.funds) {
    if (!item || !fundCode(item.code) || !timestamp(item.retrievedAt) || !validDate(item.asOf)
      || item.asOf > asOf || !item.selectedReport || !Array.isArray(item.rows)) continue;
    const selected = cleanReport(item.selectedReport, item.code, asOf, addError);
    const prior = item.previousReport ? cleanReport(item.previousReport, item.code, asOf, addError) : null;
    if (!selected || (item.previousReport && !prior)) continue;
    if (!result.has(item.code)) result.set(item.code, item);
  }
  return result;
}

function catalogDetails(catalog) {
  if (!catalog) return { known: false, fundCount: null, companyCount: null, funds: [], companies: [] };
  if (typeof catalog !== 'object' || Array.isArray(catalog)) throw new SafeBuildError('INVALID_CATALOG');
  const companies = Array.isArray(catalog.companies) ? catalog.companies.map(company => ({
    id: typeof company.id === 'string' ? company.id : company.companyId || company.companyCode || company.code || null,
    name: company.name || company.companyName || company.company || null,
    declaredFundCount: Number.isInteger(company.fundCount) && company.fundCount >= 0 ? company.fundCount : null,
    funds: Array.isArray(company.funds) ? company.funds : [],
  })) : [];
  const records = Array.isArray(catalog.funds) ? catalog.funds : companies.flatMap(company => company.funds
    .map(fund => typeof fund === 'string' ? { code: fund, companyId: company.id, company: company.name }
      : { ...fund, companyId: fund.companyId || fund.companyCode || company.id,
        company: fund.company || fund.companyName || company.name }));
  const funds = [...new Map(records.filter(item => item && fundCode(item.code)).map(item => [item.code, {
    code: item.code, companyId: item.companyId || item.companyCode || null, company: item.company || item.companyName || null,
  }])).values()];
  const declaredFundCount = catalog.fundCount ?? catalog.totalFundCount ?? (catalog.coverage && catalog.coverage.fundCount);
  const declaredCompanyCount = catalog.companyCount ?? catalog.totalCompanyCount ?? (catalog.coverage && catalog.coverage.companyCount);
  if (!companies.length) {
    for (const item of funds) {
      const id = item.companyId || item.company;
      if (id && !companies.some(company => (company.id || company.name) === id)) {
        companies.push({ id: item.companyId, name: item.company, declaredFundCount: null, funds: [] });
      }
    }
  }
  return {
    known: true, funds, companies,
    fundCount: Number.isInteger(declaredFundCount) && declaredFundCount >= funds.length ? declaredFundCount : funds.length,
    companyCount: Number.isInteger(declaredCompanyCount) && declaredCompanyCount >= companies.length ? declaredCompanyCount : companies.length,
  };
}

function confirmedPortfolio(fund) {
  return fundCode(fund.sourcePortfolioCode) && publicUrl(fund.portfolioSourceUrl)
    && Array.isArray(fund.shareClassCodes) && fund.shareClassCodes.includes(fund.code)
    && fund.shareClassCodes.includes(fund.sourcePortfolioCode);
}

function deduplicatePortfolios(funds, addError) {
  const evidence = new Map();
  for (const fund of funds.filter(confirmedPortfolio)) {
    for (const code of fund.shareClassCodes.filter(fundCode)) {
      if (!evidence.has(code)) evidence.set(code, new Set());
      evidence.get(code).add(fund.sourcePortfolioCode);
    }
  }
  const conflicted = new Set();
  for (const [code, identities] of evidence) {
    if (identities.size > 1) {
      identities.forEach(identity => conflicted.add(identity));
      addError('PORTFOLIO_IDENTITY_CONFLICT', code);
    }
  }
  const groups = new Map();
  for (const fund of funds) {
    const identities = evidence.get(fund.code);
    const candidate = identities && identities.size === 1 ? [...identities][0] : null;
    const identity = candidate && !conflicted.has(candidate) ? candidate : null;
    const key = identity ? `portfolio:${identity}` : `fund:${fund.code}`;
    if (!groups.has(key)) groups.set(key, { identity, members: [] });
    groups.get(key).members.push(fund);
  }
  return [...groups.values()].map(({ identity, members }) => {
    if (!identity) return members[0];
    // Choose a representative; never add share-class NAV, positions, or weights.
    const ordered = [...members].sort((a, b) => Number(b.code === identity) - Number(a.code === identity)
      || b.selectedReport.periodEnd.localeCompare(a.selectedReport.periodEnd)
      || b.retrievedAt.localeCompare(a.retrievedAt));
    const winner = clone(ordered[0]);
    const proof = members.find(member => confirmedPortfolio(member) && member.sourcePortfolioCode === identity);
    const aliases = new Map();
    for (const member of members) {
      aliases.set(member.code, { code: member.code, name: member.name || null });
      for (const alias of Array.isArray(member.aliases) ? member.aliases : []) {
        if (alias && fundCode(alias.code)) aliases.set(alias.code, { code: alias.code, name: alias.name || null });
      }
      if (confirmedPortfolio(member)) {
        for (const code of member.shareClassCodes.filter(fundCode)) {
          if (!aliases.has(code)) aliases.set(code, { code, name: null });
        }
      }
    }
    winner.sourcePortfolioCode = identity;
    winner.portfolioSourceUrl = proof ? proof.portfolioSourceUrl : winner.portfolioSourceUrl;
    winner.shareClassCodes = [...aliases.keys()].sort();
    winner.aliases = [...aliases.values()].sort((a, b) => a.code.localeCompare(b.code));
    winner.portfolioDeduplicated = members.length > 1 || winner.portfolioDeduplicated === true;
    winner.portfolioRepresentativeCode = winner.code;
    winner.summary.aggregatesShareClasses = false;
    return winner;
  });
}

function companyCoverageFor(catalog, funds) {
  return catalog.companies.map(company => {
    const companyFunds = catalog.funds.filter(fund => company.id && fund.companyId
      ? company.id === fund.companyId : company.name && company.name === fund.company);
    const catalogCodes = new Set(companyFunds.map(fund => fund.code));
    const availableCodes = new Set();
    let availablePortfolioCount = 0;
    for (const fund of funds) {
      const codes = confirmedPortfolio(fund) ? fund.shareClassCodes : [fund.code];
      const matches = codes.filter(code => catalogCodes.has(code));
      if (matches.length || (!catalogCodes.size && (company.id && fund.companyId === company.id
        || company.name && fund.company === company.name))) {
        availablePortfolioCount += 1;
        (matches.length ? matches : [fund.code]).forEach(code => availableCodes.add(code));
      }
    }
    const expected = company.declaredFundCount !== null ? Math.max(company.declaredFundCount, catalogCodes.size) : catalogCodes.size;
    const missing = Math.max(0, expected - availableCodes.size);
    return { id: company.id, name: company.name, catalogFundCount: expected,
      availableFundCount: availableCodes.size, availablePortfolioCount, missingFundCount: missing,
      status: !availableCodes.size ? 'unavailable' : missing ? 'partial' : 'available' };
  });
}

function buildSnapshot(raw, { previous = null, config = {}, now = new Date(), preserveOthers = false, catalog = null } = {}) {
  const nowValue = now instanceof Date ? now : new Date(now);
  if (!Number.isFinite(nowValue.getTime()) || !raw || typeof raw !== 'object' || !Array.isArray(raw.funds)) {
    throw new SafeBuildError('INVALID_INPUT');
  }
  if (!validDate(raw.asOf)) throw new SafeBuildError('INVALID_AS_OF');
  if (raw.asOf > nowValue.toISOString().slice(0, 10)) throw new SafeBuildError('FUTURE_AS_OF');
  if (!timestamp(raw.retrievedAt) || Date.parse(raw.retrievedAt) > nowValue.getTime() + 300000) {
    throw new SafeBuildError('INVALID_RETRIEVED_AT');
  }
  const configured = config.funds === undefined ? raw.funds : config.funds;
  if (!Array.isArray(configured) || !configured.length || configured.some(item => !item || !fundCode(item.code))) {
    throw new SafeBuildError('INVALID_CONFIG');
  }
  const requested = [...new Set(configured.map(item => item.code))];
  if ((config.targetPeriodEnd !== undefined && !validDate(config.targetPeriodEnd))
    || (config.baselinePeriodEnd !== undefined && !validDate(config.baselinePeriodEnd))) throw new SafeBuildError('INVALID_CONFIG');
  const requestedSet = new Set(requested);
  const directory = catalogDetails(catalog || config.catalog);
  const directoryFunds = new Map(directory.funds.map(fund => [fund.code, fund]));
  const errors = [];
  const addError = (errorCode, code = null, stage = 'build') => errors.push(safeError(errorCode, stage, code));
  for (const issue of Array.isArray(raw.errors) ? raw.errors : []) {
    errors.push(safeError(issue, issue && issue.stage, issue && issue.code));
  }
  const cached = previousFunds(previous, raw.asOf, addError);
  if (config.targetPeriodEnd) {
    for (const [code, item] of cached) {
      if (item.selectedReport.periodEnd !== config.targetPeriodEnd) cached.delete(code);
    }
  }
  const barsByStock = cleanBars(raw.barsByStock, addError);
  const analysisOptions = { asOf: raw.asOf, barsByStock,
    periodEnd: config.targetPeriodEnd, baselinePeriodEnd: config.baselinePeriodEnd };
  const groups = new Map();
  for (const item of raw.funds) {
    if (!item || !fundCode(item.code)) { addError('INVALID_FUND_CODE'); continue; }
    if (!requestedSet.has(item.code)) { addError('OUT_OF_SCOPE_FUND', item.code); continue; }
    if (groups.has(item.code)) { addError('DUPLICATE_FUND_CODE', item.code); groups.get(item.code).push(item); }
    else groups.set(item.code, [item]);
  }
  const funds = [];
  const batchResults = [];
  let freshFundCount = 0;
  let batchNoEquitiesFundCount = 0;
  let failedFundCount = 0;
  for (const code of requested) {
    const items = groups.get(code) || [];
    const old = cached.get(code);
    const rawReports = items.flatMap(item => Array.isArray(item.reports) ? item.reports : []);
    const reports = new Map();
    for (const item of rawReports) {
      const normalized = cleanReport(item, code, raw.asOf, addError);
      if (!normalized) continue;
      const key = reportKey(normalized);
      if (!reports.has(key) || reports.get(key).holdings.length < normalized.holdings.length) reports.set(key, normalized);
    }
    for (const item of items) {
      if (item.noEquitiesEvidence === undefined || item.noEquitiesEvidence === null) continue;
      if (!isValidNoEq(item.noEquitiesEvidence, { asOf: raw.asOf, code })) {
        addError('INVALID_NO_EQUITIES_EVIDENCE', code); continue;
      }
      const normalized = cleanReport(noEquitiesReport(item.noEquitiesEvidence, code), code, raw.asOf, addError);
      if (normalized) reports.set(reportKey(normalized), normalized);
    }
    if (!reports.size) {
      const reason = items.length ? 'NO_VALID_REPORTS' : 'FUND_NOT_COLLECTED';
      addError(reason, code); failedFundCount += 1;
      batchResults.push({ code, status: 'failed', errorCode: reason });
      if (old) funds.push(cachedFund(old, reason));
      continue;
    }
    const item = items[0];
    const catalogFund = directoryFunds.get(code);
    const oldPortfolio = old && confirmedPortfolio(old) && !fundCode(item.sourcePortfolioCode) ? old : null;
    const metadata = {
      code, name: item.name, company: item.company, managers: item.managers,
      companyId: item.companyId || item.companyCode || (catalogFund && catalogFund.companyId) || null,
      sourcePortfolioCode: fundCode(item.sourcePortfolioCode) ? item.sourcePortfolioCode : oldPortfolio && oldPortfolio.sourcePortfolioCode,
      portfolioSourceUrl: publicUrl(item.portfolioSourceUrl) ? item.portfolioSourceUrl : oldPortfolio && oldPortfolio.portfolioSourceUrl,
      metadataSourceUrl: publicUrl(item.metadataSourceUrl) ? item.metadataSourceUrl : null,
      managerSourceUrl: publicUrl(item.managerSourceUrl) ? item.managerSourceUrl : null,
      managersAsOf: validDate(item.managersAsOf) && item.managersAsOf <= raw.asOf ? item.managersAsOf : null,
      managerScope: item.managerScope === 'current-profile' ? item.managerScope : null,
      shareClassCodes: oldPortfolio ? oldPortfolio.shareClassCodes
        : (Array.isArray(item.shareClassCodes) ? item.shareClassCodes : []).filter(fundCode),
    };
    const current = analyzeFund({ ...metadata,
      reports: [...reports.values()] }, analysisOptions);
    if (!current.selectedReport) {
      const reason = 'TARGET_REPORT_MISSING';
      addError(reason, code); failedFundCount += 1;
      batchResults.push({ code, status: 'failed', errorCode: reason });
      if (old) funds.push(cachedFund(old, reason));
      continue;
    }
    let reason = null;
    if (old && current.selectedReport.periodEnd < old.selectedReport.periodEnd) reason = 'OLDER_REPORT';
    if (old && current.selectedReport.periodEnd === old.selectedReport.periodEnd) {
      if (current.selectedReport.noEquitiesConfirmed !== old.selectedReport.noEquitiesConfirmed
        && (current.selectedReport.noEquitiesConfirmed || old.selectedReport.noEquitiesConfirmed)) reason = 'CONTRADICTORY_NO_EQUITIES';
      else if (current.selectedReport.holdings.length < old.selectedReport.holdings.length) reason = 'HOLDING_COVERAGE_REGRESSION';
      else if (old.selectedReport.coverage === 'full' && current.selectedReport.coverage !== 'full') reason = 'REPORT_COVERAGE_REGRESSION';
    }
    if (reason) {
      addError(reason, code); failedFundCount += 1; funds.push(cachedFund(old, reason));
      batchResults.push({ code, status: 'failed', errorCode: reason }); continue;
    }
    // Preserve source records across runs; selected data must first pass fresh-data
    // gates above, so history can never disguise an entirely failed collection.
    const history = new Map();
    for (const historical of old ? (old.reports || [old.selectedReport, old.previousReport]).filter(Boolean) : []) {
      const normalized = cleanReport(historical, code, raw.asOf, addError);
      if (normalized) history.set(reportKey(normalized), normalized);
    }
    for (const [key, normalized] of reports) history.set(key, normalized);
    const finalFund = analyzeFund({ ...metadata,
      reports: [...history.values()] }, analysisOptions);
    finalFund.reports = [...history.values()].sort((a, b) => b.periodEnd.localeCompare(a.periodEnd)
      || b.publishedAt.localeCompare(a.publishedAt));
    finalFund.retrievedAt = raw.retrievedAt;
    finalFund.dataStatus = 'fresh';
    finalFund.cacheReason = null;
    if (finalFund.selectedReport.noEquitiesConfirmed) {
      finalFund.status = 'no-equities';
      finalFund.noEquitiesEvidence = finalFund.selectedReport.noEquitiesEvidence;
      finalFund.rows = [];
      finalFund.summary.currentHoldingCount = 0;
      finalFund.summary.knownPositionCount = 0;
      finalFund.summary.knownPositionYuan = 0;
      for (const key of ['increasedCount', 'decreasedCount', 'enteredDisclosureCount', 'leftDisclosureCount']) {
        finalFund.summary[key] = 0;
      }
      batchNoEquitiesFundCount += 1;
    }
    preservePrices(finalFund, old, raw.retrievedAt);
    funds.push(finalFund); freshFundCount += 1;
    batchResults.push({ code, status: finalFund.status === 'no-equities' ? 'no-equities' : 'complete', errorCode: null });
  }
  if (!freshFundCount) throw new SafeBuildError('NO_VALID_FRESH_FUNDS');
  let retainedFundCount = 0;
  if (preserveOthers) {
    for (const [code, old] of cached) {
      if (!requestedSet.has(code)) {
        funds.push(cachedFund(old, 'NOT_IN_CURRENT_BATCH'));
        retainedFundCount += 1;
      }
    }
  }
  for (const fund of funds) {
    const entry = directoryFunds.get(fund.code);
    if (entry && !fund.companyId) fund.companyId = entry.companyId;
  }
  const publishedFunds = deduplicatePortfolios(funds, addError);
  const companyCoverage = companyCoverageFor(directory, publishedFunds);
  const availableFundCodes = new Set(publishedFunds.flatMap(fund => confirmedPortfolio(fund) ? fund.shareClassCodes : [fund.code]));
  const catalogAvailableFundCount = directory.known
    ? directory.funds.filter(fund => availableFundCodes.has(fund.code)).length : null;
  const limitations = [
    '目录规模与已处理数据分别统计；公司和基金覆盖以当前有效披露数据为准。',
    '按最近已披露报告比较，披露名单进出不等于买入或清仓，股数变化也可能包含公司行为。',
    '区间行情仅反映已获取交易日，完整性未核验，不能视为实际持仓成本。',
    '持仓金额仅汇总已披露证券；仅凭公开主代码及份额证据去重同组合，份额金额不叠加。',
  ];
  if (failedFundCount) limitations.push('部分基金沿用上次有效数据或暂缺，原始观测日期保留。');
  const uniqueErrors = [...new Map(errors.map(issue => [JSON.stringify(issue), issue])).values()];
  return {
    schemaVersion: 1, asOf: raw.asOf, retrievedAt: raw.retrievedAt, sourceMode: 'actions',
    targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
    coverage: {
      targetPeriodEnd: config.targetPeriodEnd || null, baselinePeriodEnd: config.baselinePeriodEnd || null,
      fundCount: publishedFunds.length, companyCount: new Set(publishedFunds.map(item => item.companyId || item.company).filter(Boolean)).size,
      scope: typeof config.scope === 'string' ? config.scope : '按公开基金目录逐家处理，展示已取得的有效报告', limitations,
      holdingCount: publishedFunds.reduce((sum, item) => sum + item.rows.filter(row => row.current).length, 0),
      priceAvailable: publishedFunds.reduce((sum, item) => sum + item.rows.filter(row => row.current && row.priceRange.status === 'observed').length, 0),
      narrativeCount: publishedFunds.filter(item => item.narrative && item.narrative.text).length,
      requestedFundCount: directory.known ? directory.fundCount : preserveOthers ? null : requested.length,
      catalogFundCount: directory.fundCount, catalogCompanyCount: directory.companyCount,
      catalogAvailableFundCount, catalogStatus: directory.known ? 'available' : 'unavailable',
      batchRequestedFundCount: requested.length, batchFreshFundCount: freshFundCount,
      noEquitiesFundCount: publishedFunds.filter(fund => fund.status === 'no-equities').length,
      batchNoEquitiesFundCount,
      failedFundCount, retainedFundCount,
      shareClassDeduplicatedCount: funds.length - publishedFunds.length,
    },
    companyCoverage,
    batchResults,
    funds: publishedFunds, errors: uniqueErrors,
  };
}

function argumentsFor(argv) {
  const result = { input: 'raw/input.json', output: 'web/data/latest.json', config: 'config/universe.json', catalog: null, mergeAll: false };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--merge-all') { result.mergeAll = true; continue; }
    const key = argv[index].slice(2);
    if (!argv[index].startsWith('--') || !['input', 'output', 'config', 'catalog'].includes(key) || !argv[index + 1]) {
      throw new SafeBuildError('INVALID_ARGUMENTS');
    }
    result[key] = argv[index + 1]; index += 1;
  }
  return result;
}

function readJson(filename, errorCode) {
  try { return JSON.parse(fs.readFileSync(filename, 'utf8')); }
  catch { throw new SafeBuildError(errorCode); }
}

function main(argv = process.argv.slice(2)) {
  let temporary;
  try {
    const args = argumentsFor(argv);
    const raw = readJson(args.input, 'INPUT_READ_FAILED');
    const config = readJson(args.config, 'INVALID_CONFIG');
    const catalog = args.catalog ? readJson(args.catalog, 'INVALID_CATALOG') : null;
    const previous = fs.existsSync(args.output) ? readJson(args.output, 'INVALID_PREVIOUS_SNAPSHOT') : null;
    const result = buildSnapshot(raw, { previous, config, catalog, preserveOthers: args.mergeAll });
    temporary = `${args.output}.tmp-${process.pid}`;
    try {
      fs.mkdirSync(path.dirname(args.output), { recursive: true });
      fs.writeFileSync(temporary, `${JSON.stringify(result, null, 2)}\n`, { flag: 'wx' });
      fs.renameSync(temporary, args.output);
    } catch { throw new SafeBuildError('OUTPUT_WRITE_FAILED'); }
    process.stdout.write(`${JSON.stringify({ status: 'ok', fundCount: result.coverage.fundCount,
      failedFundCount: result.coverage.failedFundCount, holdingCount: result.coverage.holdingCount,
      errorCount: result.errors.length })}\n`);
    return 0;
  } catch (error) {
    if (temporary) { try { fs.unlinkSync(temporary); } catch { /* No raw filesystem diagnostics. */ } }
    process.stdout.write(`${JSON.stringify({ status: 'failed', errorCode: safeError(error).errorCode })}\n`);
    return 1;
  }
}

if (require.main === module) process.exitCode = main();

module.exports = { buildSnapshot, isValidNoEq, main, publicUrl, safeError, SafeBuildError };
