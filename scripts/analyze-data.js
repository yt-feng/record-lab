#!/usr/bin/env node
'use strict';

// Aggregate retained observations in Actions. A source-proven portfolio is the
// additive unit; unresolved share-class identities remain visible, not summed.
const fs = require('node:fs');
const path = require('node:path');
const { createAnalysisSpool } = require('./analysis-spool');
const { compareHolding, estimatePosition, normalizeHolding, validDate } = require('../lib/fund-analysis');
const { publicUrl, isValidNotApplicable } = require('./build-data');
const { validCompletePrice, retainedCoverage } = require('../lib/price-coverage');
const { strategySectionCoverageFor, strategyAnalysisFor } = require('../lib/strategy-sections');
const { normalizeManagers, verifiedReportManagers, managerCoverageFor, completeManagerCoverage, overlaps: managerOverlaps } = require('../lib/report-managers');
const { chinaDate } = require('../web/source-dates');

const CODE = /^\d{6}$/;
const COMPANY_PATH = /^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/;
const PART_LIMIT = 8_000_000;
const INDEX_LIMIT = 25_000_000;
const CHANGES = { increase: 'increase', decrease: 'decrease', unchanged: 'unchanged',
  'entered-disclosure': 'enteredDisclosure', 'left-disclosure': 'leftDisclosure',
  'no-baseline': 'noBaseline', 'not-comparable': 'notComparable' };
const THEMES = [['行业/配置', /行业|配置|仓位/], ['估值/基本面', /估值|基本面|经营|盈利/],
  ['调仓/交易', /调整|增持|减持|增配|减配|买入|卖出/], ['组合管理', /组合|个股/], ['风险/防守', /风险|防守|现金|债券/]];
const text = (value, limit = 200) => typeof value === 'string' ? value.trim().slice(0, limit) : null;
const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const round = value => Number.isFinite(value) ? Number(value.toPrecision(13)) : null;
const stamp = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value)
  && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
const fail = code => { throw new Error(code); };

function datesFor(target, baseline) {
  if (!validDate(target)) fail('INVALID_ANALYSIS_PERIOD');
  const year = Number(target.slice(0, 4)), month = Number(target.slice(5, 7));
  const end = value => new Date(Date.UTC(year, value, 0)).toISOString().slice(0, 10);
  if (month % 3 || target !== end(month) || baseline !== end(month - 3)) fail('INVALID_ANALYSIS_PERIOD');
  return { target, baseline, start: `${year}-${String(month - 2).padStart(2, '0')}-01` };
}

function reportRef(report) {
  if (!report || !publicUrl(report.sourceUrl) || !validDate(report.periodEnd)
    || !validDate(report.publishedAt) || report.publishedAt < report.periodEnd) return null;
  return { sourceUrl: report.sourceUrl, periodEnd: report.periodEnd, publishedAt: report.publishedAt,
    kind: ['quarterly', 'semiannual', 'annual'].includes(report.kind) ? report.kind : 'unknown',
    coverage: ['top10', 'full', 'unknown'].includes(report.coverage) ? report.coverage : 'unknown',
    navYuan: finite(report.navYuan) && report.navYuan > 0 ? report.navYuan : null,
    navSourceUrl: publicUrl(report.navSourceUrl) ? report.navSourceUrl : report.sourceUrl,
    holdingsSourceUrl: publicUrl(report.holdingsSourceUrl) ? report.holdingsSourceUrl : report.sourceUrl,
    reportOnly: report.reportOnly === true };
}

function strategyFor(fund, phase) {
  const report = fund.selectedReport || {}, narrative = fund.narrative || report.narrative;
  const sourceProof = strategySectionCoverageFor(report.strategySectionCoverage, phase.target);
  const strategyObserved = report.strategyEvidenceRetrievedAt || fund.retrievedAt;
  const observed = stamp(strategyObserved) && stamp(phase.generation || fund.retrievedAt)
    && Date.parse(strategyObserved) <= Date.parse(phase.generation || fund.retrievedAt) + 300000;
  const proof = sourceProof?.periodStart === phase.start && sourceProof.kind === 'quarterly'
    && observed && sourceProof.publishedAt <= chinaDate(strategyObserved) ? sourceProof : null;
  const analysis = proof ? strategyAnalysisFor(report.strategyAnalysis, proof) : null;
  const candidates = [...(Array.isArray(report.strategyExcerpts) ? report.strategyExcerpts : [])];
  if (narrative?.text) candidates.push(narrative);
  const unique = new Map();
  for (const item of candidates) {
    if (!item || item.periodStart !== phase.start || item.periodEnd !== phase.target
      || !publicUrl(item.sourceUrl) || !text(item.text)) continue;
    const excerpt = { text: text(item.text, 260), sourceUrl: item.sourceUrl,
      page: Number.isInteger(item.page) && item.page > 0 && item.page <= 10000 ? item.page : null,
      periodStart: phase.start, periodEnd: phase.target };
    unique.set(`${excerpt.sourceUrl}\0${excerpt.text}`, excerpt);
  }
  const all = analysis?.evidence.length ? analysis.evidence : [...unique.values()];
  const combined = all.map(item => item.text).join(' ');
  const limit = analysis?.evidence.length ? 8 : 2;
  return { scope: 'report-level-selected-evidence', status: all.length ? proof?.complete ? 'available' : 'partial-source' : 'missing',
    sectionCoverage: proof, sectionStatus: proof?.complete ? 'complete' : all.length || proof ? 'partial' : sourceProof ? 'wrong-period' : 'missing',
    evidenceRetrievedAt: observed ? strategyObserved : null,
    analysis, analysisCoverage: analysis?.analysisCoverage || 'legacy-excerpts', semanticAnalysisComplete: false,
    themes: THEMES.filter(([, pattern]) => pattern.test(combined)).map(([label]) => label),
    excerpts: all.slice(0, limit), availableExcerptCount: all.length, excerptLimit: limit,
    explainsIndividualStockTrades: false };
}

function managersFor(fund, phase) {
  const managers = normalizeManagers(fund.managers);
  const verified = verifiedReportManagers(fund.reportManagers, phase.start, phase.target);
  let reportManagers = verified.length ? verified : normalizeManagers(fund.managerAttribution === 'tenure-overlap'
    ? fund.reportManagers : fund.managers).filter(item => managerOverlaps(item, phase.start, phase.target));
  const attribution = verified.length ? 'report' : reportManagers.length ? 'tenure-overlap' : 'current-profile';
  const managerTableCoverage = managerCoverageFor(fund.managerTableCoverage, null, phase.target);
  const managerComplete = completeManagerCoverage(fund, phase.target);
  return { managers, reportManagers, managerAttribution: attribution,
    managerTableCoverage, managerCoverageStatus: managerComplete ? 'complete' : verified.length || managerTableCoverage ? 'partial' : 'missing',
    managerEvidenceStatus: managerComplete ? 'complete' : verified.length ? 'partial' : 'missing',
    managerSourceUrl: verified.length ? verified[0].evidence.sourceUrl : publicUrl(fund.managerSourceUrl) ? fund.managerSourceUrl : null,
    managerEvidenceRetrievedAt: stamp(fund.managerEvidenceRetrievedAt) ? fund.managerEvidenceRetrievedAt : null,
    managersAsOf: validDate(fund.managersAsOf) ? fund.managersAsOf : null };
}

function descriptor(fund, file, index, phase) {
  if (!fund || !CODE.test(fund.code) || !stamp(fund.retrievedAt)) fail('INVALID_ANALYSIS_RECORD');
  if (!fund.selectedReport && isValidNotApplicable(fund.notApplicableEvidence,
    { asOf: fund.asOf, code: fund.code, periodEnd: phase.target })) return null;
  const report = reportRef(fund.selectedReport);
  if (!report || report.periodEnd !== phase.target) fail('ANALYSIS_PERIOD_MISMATCH');
  if (report.publishedAt > chinaDate(fund.retrievedAt)) fail('ANALYSIS_FUTURE_REPORT');
  if (fund.selectedReport.fundCode && fund.selectedReport.fundCode !== fund.code) fail('ANALYSIS_IDENTITY_MISMATCH');
  const aliases = [...new Set((fund.shareClassCodes || []).filter(code => typeof code === 'string' && CODE.test(code)))];
  const proven = CODE.test(fund.sourcePortfolioCode || '') && publicUrl(fund.portfolioSourceUrl)
    && aliases.includes(fund.code) && aliases.includes(fund.sourcePortfolioCode);
  return { code: fund.code, file, index, companyId: text(fund.companyId, 64) || 'unknown',
    identity: proven ? fund.sourcePortfolioCode : null, aliases: proven ? aliases : [fund.code],
    identitySourceUrl: proven ? fund.portfolioSourceUrl : null, report,
    holdingCount: (fund.selectedReport.holdings || []).length, retrievedAt: fund.retrievedAt };
}

function selectRecords(records) {
  const proof = new Map(), companies = new Map(), conflicts = new Set();
  for (const record of records.filter(item => item.identity)) {
    if (!companies.has(record.identity)) companies.set(record.identity, new Set());
    companies.get(record.identity).add(record.companyId);
    for (const code of record.aliases) {
      if (!proof.has(code)) proof.set(code, new Set());
      proof.get(code).add(record.identity);
    }
  }
  for (const identities of proof.values()) if (identities.size > 1) for (const id of identities) conflicts.add(id);
  for (const [id, owners] of companies) if (owners.size !== 1) conflicts.add(id);
  const groups = new Map();
  for (const record of records) {
    const identities = proof.get(record.code), candidate = identities?.size === 1 ? [...identities][0] : null;
    const identity = candidate && !conflicts.has(candidate) && companies.get(candidate)?.has(record.companyId) ? candidate : null;
    const id = `${identity ? 'portfolio' : 'fund'}:${identity || record.code}`;
    if (!groups.has(id)) groups.set(id, { id, identity, members: [] });
    groups.get(id).members.push(record);
  }
  const result = [];
  for (const group of groups.values()) {
    group.members.sort((a, b) => b.report.publishedAt.localeCompare(a.report.publishedAt)
      || b.holdingCount - a.holdingCount || Number(b.code === group.identity) - Number(a.code === group.identity)
      || b.retrievedAt.localeCompare(a.retrievedAt) || a.code.localeCompare(b.code));
    const source = group.members.find(item => item.identity === group.identity);
    result.push({ ...group.members[0], id: group.id, aggregationEligible: Boolean(group.identity),
      identity: group.identity, identitySourceUrl: source?.identitySourceUrl || null,
      aliases: group.identity ? [...new Set(group.members.flatMap(item => item.aliases))].sort() : [group.members[0].code] });
  }
  return { selected: result, conflicts: conflicts.size, duplicatesRemoved: records.length - result.length };
}

function holdingFor(value, row) {
  if (!value) return null;
  const holding = normalizeHolding(value);
  if (!holding || holding.market !== row.market || holding.stockCode !== row.stockCode
    || holding.currency !== row.currency) return null;
  return holding;
}
const holdingFacts = holding => holding ? { shares: holding.shares, weightPct: holding.weightPct,
  marketValueYuan: holding.marketValueYuan } : null;

function priceFor(value, row, phase) {
  if (!value || value.status !== 'observed' || value.priceBasis !== 'unadjusted'
    || value.currency !== row.currency || value.rangeStart !== phase.start || value.rangeEnd !== phase.target
    || !finite(value.low) || value.low <= 0 || !finite(value.high) || value.high < value.low
    || !validDate(value.observedStart) || !validDate(value.observedEnd)
    || value.observedStart < phase.start || value.observedEnd > phase.target || value.observedStart > value.observedEnd
    || !Number.isInteger(value.observedCount) || value.observedCount < 1 || !publicUrl(value.sourceUrl)) return null;
  return value;
}

function emptyStock(row, phase) {
  return { key: `${row.market}:${row.stockCode}`, stockCode: row.stockCode, stockName: text(row.stockName, 100) || row.stockCode,
    market: row.market, currency: row.currency, currentPortfolioCount: 0, previousPortfolioCount: 0,
    aggregationEligiblePortfolioCount: 0, identityUnresolvedCount: 0,
    disclosedValueYuan: null, navWeightEstimateYuan: null, knownValueYuan: null,
    valueCoverage: { unit: 'CNY', scope: 'source-verified-portfolios-only', disclosed: 0, estimated: 0, known: 0, missing: 0 },
    changes: Object.fromEntries(Object.values(CHANGES).map(key => [key, 0])),
    comparableSharesDelta: null, reportManagerCount: 0, currentProfileManagerCount: 0,
    priceRange: { status: 'missing', low: null, high: null, currency: row.currency, rangeStart: phase.start,
      rangeEnd: phase.target, observedCount: 0, completeness: 'partial', isCost: false,
      actualCostKnown: false, sourceUrls: [], sourceCount: 0, sourceUrlsTruncated: false, conflictingObservations: false },
    details: [], _contributions: [], _reportManagers: new Set(), _currentManagers: new Set(), _tenureManagers: new Set(), _partialManagers: new Set(),
    _priceSources: new Set(), _prices: new Set(), _completePrice: null, _completePriceConflict: false };
}

function appendValue(stock, field, value, coverage) {
  if (value === null) return;
  stock[field] = (stock[field] ?? 0) + value;
  stock.valueCoverage[coverage]++;
}

function boundedParts(kind, field, items, prefix, phase, limit, writePart, onPart) {
  let part = [], bytes = 200, index = 0;
  const flush = () => {
    if (!part.length) return;
    const file = `./analysis/${prefix}-${String(index++).padStart(4, '0')}.json`;
    const document = { schemaVersion: 1, kind, generation: phase.generation,
      targetPeriodEnd: phase.target, baselinePeriodEnd: phase.baseline, [field]: part };
    if (Buffer.byteLength(JSON.stringify(document)) > limit) fail('ANALYSIS_PART_TOO_LARGE');
    writePart(file, document); onPart(file, part); part = []; bytes = 200;
  };
  for (const item of items) {
    const size = Buffer.byteLength(JSON.stringify(item)) + 1;
    if (size + 200 > limit) fail('ANALYSIS_ITEM_TOO_LARGE');
    if (bytes + size > limit) flush();
    part.push(item); bytes += size;
  }
  flush();
}

function analyzeQuarter(manifest, loadCompany, { progress = null, writePart = () => {}, maxPartBytes = PART_LIMIT,
  now = new Date(), spool = null } = {}) {
  const awaitingData = manifest?.coverage?.status === 'awaiting-data';
  const emptyObservation = awaitingData && manifest.asOf === null && manifest.retrievedAt === null
    && manifest.priceUpdatedAt == null && manifest.coverage.fundCount === 0 && manifest.coverage.complete !== true
    && Array.isArray(manifest.funds) && manifest.funds.length === 0
    && Array.isArray(manifest.companyFiles) && manifest.companyFiles.length === 0
    && ['companyCount', 'holdingCount', 'priceAvailable', 'priceComplete', 'narrativeCount', 'noEquitiesFundCount',
      'notApplicableFundCount', 'establishedAfterPeriodFundCount', 'terminatedBeforePeriodFundCount', 'reportOnlyFundCount',
      'catalogAvailableFundCount', 'unmatchedCompanyFundCount'].every(key => manifest.coverage[key] === undefined || manifest.coverage[key] === 0)
    && (manifest.companyCoverage === undefined || Array.isArray(manifest.companyCoverage) && manifest.companyCoverage.every(company => company
      && ['availableFundCount', 'availablePortfolioCount'].every(key => company[key] === undefined || company[key] === 0)));
  if (!manifest || manifest.schemaVersion !== 1 || manifest.sourceMode !== 'actions'
    || !Array.isArray(manifest.companyFiles) || manifest.companyFiles.length > 10000
    || (awaitingData ? !emptyObservation : !stamp(manifest.retrievedAt) || !validDate(manifest.asOf))) fail('INVALID_ANALYSIS_MANIFEST');
  const phase = datesFor(manifest.targetPeriodEnd, manifest.baselinePeriodEnd);
  const currentTime = new Date(now);
  if (!Number.isFinite(currentTime.getTime()) || !awaitingData && (manifest.asOf > chinaDate(currentTime)
    || Date.parse(manifest.retrievedAt) > currentTime.getTime() + 300000 || manifest.asOf < phase.target)) fail('ANALYSIS_FUTURE_OBSERVATION');
  if (manifest.priceUpdatedAt != null && (!stamp(manifest.priceUpdatedAt)
    || Date.parse(manifest.priceUpdatedAt) > currentTime.getTime() + 300000)) fail('ANALYSIS_FUTURE_OBSERVATION');
  phase.generation = currentTime.toISOString();
  if (!Number.isInteger(maxPartBytes) || maxPartBytes < 2000 || maxPartBytes > PART_LIMIT) fail('INVALID_ANALYSIS_LIMIT');
  if (progress && (progress.phase?.targetPeriodEnd !== phase.target || progress.phase?.baselinePeriodEnd !== phase.baseline)) fail('ANALYSIS_PROGRESS_PERIOD_MISMATCH');
  const seenFiles = new Set(), records = [];
  let excludedNotApplicable = 0, inputRecords = 0;
  const readCompany = file => {
    if (!COMPANY_PATH.test(file.path)) fail('INVALID_ANALYSIS_COMPANY_PATH');
    const document = loadCompany(file.path);
    if (!document || document.schemaVersion !== 1 || document.sourceMode !== 'actions'
      || document.targetPeriodEnd !== phase.target || document.baselinePeriodEnd !== phase.baseline
      || !Array.isArray(document.funds) || document.funds.length > 1000) fail('ANALYSIS_COMPANY_PERIOD_MISMATCH');
    return document;
  };
  for (const file of manifest.companyFiles) {
    if (seenFiles.has(file.path)) fail('DUPLICATE_ANALYSIS_COMPANY_PATH');
    seenFiles.add(file.path);
    const document = readCompany(file);
    for (const [index, fund] of document.funds.entries()) {
      inputRecords++;
      if (Date.parse(fund.retrievedAt) > Date.parse(manifest.retrievedAt)) fail('ANALYSIS_OBSERVATION_MISMATCH');
      const record = descriptor(fund, file.path, index, phase);
      if (record) records.push(record); else excludedNotApplicable++;
    }
  }
  const { selected, conflicts, duplicatesRemoved } = selectRecords(records);
  const selectedByRef = new Map(selected.map(item => [`${item.file}\0${item.index}`, item]));
  const stocks = new Map(), portfolios = spool ? null : [], themes = new Map();
  const portfolioCounts = { analyzedRecords: 0, strategySectionCompleteRecords: 0, strategySectionIncompleteRecords: 0,
    strategySelectedEvidenceRecords: 0, reportManagerRecords: 0, reportManagerPartialRecords: 0,
    tenureOverlapManagerRecords: 0, managerEvidenceMissingRecords: 0, currentProfileOnlyManagerRecords: 0 };
  let invalidRows = 0, reportOnlyRecords = 0, noEquityRecords = 0, unknownIdentityRecords = 0,
    unknownIdentityHoldingRecords = 0, strategyRecords = 0, currentHoldingRecords = 0, priceRows = 0;
  for (const file of manifest.companyFiles) {
    const document = readCompany(file);
    for (const [index, fund] of document.funds.entries()) {
      const identity = selectedByRef.get(`${file.path}\0${index}`);
      if (!identity) continue;
      const managers = managersFor(fund, phase), strategy = strategyFor(fund, phase);
      const previous = reportRef(fund.previousReport);
      const previousReport = previous?.periodEnd === phase.baseline ? previous : null;
      const portfolio = { id: identity.id, code: fund.code, name: text(fund.name), company: text(fund.company),
        companyId: identity.companyId, shareClassCodes: identity.aliases, aggregationEligible: identity.aggregationEligible,
        identitySourceUrl: identity.identitySourceUrl, report: identity.report, previousReport,
        ...managers, strategy, retrievedAt: fund.retrievedAt };
      if (spool) spool.addPortfolio(portfolio); else portfolios.push(portfolio);
      portfolioCounts.analyzedRecords++;
      portfolioCounts.strategySectionCompleteRecords += Number(strategy.sectionStatus === 'complete');
      portfolioCounts.strategySectionIncompleteRecords += Number(strategy.sectionStatus !== 'complete');
      portfolioCounts.strategySelectedEvidenceRecords += Number(Boolean(strategy.analysis?.evidence.length));
      portfolioCounts.reportManagerRecords += Number(managers.managerCoverageStatus === 'complete');
      portfolioCounts.reportManagerPartialRecords += Number(managers.managerCoverageStatus === 'partial');
      portfolioCounts.tenureOverlapManagerRecords += Number(managers.managerAttribution === 'tenure-overlap');
      portfolioCounts.managerEvidenceMissingRecords += Number(managers.managerCoverageStatus !== 'complete');
      portfolioCounts.currentProfileOnlyManagerRecords += Number(!managers.reportManagers.length && managers.managers.length > 0);
      if (!identity.aggregationEligible) unknownIdentityRecords++;
      if (strategy.excerpts.length) strategyRecords++;
      for (const theme of strategy.themes) {
        if (!themes.has(theme)) themes.set(theme, { name: theme, portfolioIds: [], aggregationEligiblePortfolioCount: 0 });
        themes.get(theme).portfolioIds.push(identity.id);
        themes.get(theme).aggregationEligiblePortfolioCount += Number(identity.aggregationEligible);
      }
      if (identity.report.reportOnly) { reportOnlyRecords++; continue; }
      if (fund.status === 'no-equities') noEquityRecords++;
      const seenStocks = new Set(); let hasCurrent = false;
      for (const row of Array.isArray(fund.rows) ? fund.rows : []) {
        if (!row || !((row.market === 'CN' && CODE.test(row.stockCode) && row.currency === 'CNY')
          || (row.market === 'HK' && /^\d{5}$/.test(row.stockCode) && row.currency === 'HKD')
          || (row.market === 'US' && /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/.test(row.stockCode) && row.currency === 'USD'))) { invalidRows++; continue; }
        const key = `${row.market}:${row.stockCode}`;
        if (seenStocks.has(key)) { invalidRows++; continue; }
        seenStocks.add(key);
        const current = holdingFor(row.current, row), prior = previousReport ? holdingFor(row.previous, row) : null;
        if ((row.current && !current) || (previousReport && row.previous && !prior)) { invalidRows++; continue; }
        if (!current && !prior) { if (row.current) invalidRows++; continue; }
        if (!stocks.has(key)) stocks.set(key, emptyStock(row, phase));
        const stock = stocks.get(key), position = estimatePosition(current, identity.report);
        const change = compareHolding(current, prior, Boolean(previousReport));
        stock.changes[CHANGES[change.kind]]++;
        stock.previousPortfolioCount += Number(Boolean(prior));
        if (current) {
          hasCurrent = true; stock.currentPortfolioCount++;
          stock.identityUnresolvedCount += Number(!identity.aggregationEligible);
          for (const manager of managers.reportManagers) (managers.managerAttribution === 'report'
            ? managers.managerCoverageStatus === 'complete' ? stock._reportManagers : stock._partialManagers
            : stock._tenureManagers).add(`${identity.companyId}:${manager.name}`);
          if (!managers.reportManagers.length) for (const manager of managers.managers) stock._currentManagers.add(`${identity.companyId}:${manager.name}`);
          if (identity.aggregationEligible) {
            stock.aggregationEligiblePortfolioCount++;
            appendValue(stock, 'disclosedValueYuan', position.disclosedYuan, 'disclosed');
            appendValue(stock, 'navWeightEstimateYuan', position.estimatedYuan, 'estimated');
            appendValue(stock, 'knownValueYuan', position.valueYuan, 'known');
            stock.valueCoverage.missing += Number(position.valueYuan === null);
            if (change.sharesDelta !== null) stock.comparableSharesDelta = (stock.comparableSharesDelta ?? 0) + change.sharesDelta;
          }
        }
        const price = priceFor(row.priceRange, row, phase);
        if (price) {
          priceRows++; stock._priceSources.add(price.sourceUrl); stock._prices.add(`${price.low}:${price.high}`);
          for (const url of price.sourceUrls || []) if (publicUrl(url)) stock._priceSources.add(url);
          if (validCompletePrice(price, row.market, row.stockCode, phase.start, phase.target)) {
            if (!stock._completePrice) stock._completePrice = price;
            else if (stock._completePrice.low !== price.low || stock._completePrice.high !== price.high) stock._completePriceConflict = true;
          }
          Object.assign(stock.priceRange, { status: 'observed', low: stock.priceRange.low === null ? price.low : Math.min(stock.priceRange.low, price.low),
            high: stock.priceRange.high === null ? price.high : Math.max(stock.priceRange.high, price.high),
            observedCount: Math.max(stock.priceRange.observedCount, price.observedCount) });
        }
        const contribution = { portfolioId: identity.id, current: holdingFacts(current), previous: holdingFacts(prior),
          change: CHANGES[change.kind], sharesDelta: change.sharesDelta, weightDeltaPp: change.weightDeltaPp, provesTrade: false,
          disclosedValueYuan: position.disclosedYuan, navWeightEstimateYuan: position.estimatedYuan,
          aggregationEligible: identity.aggregationEligible };
        if (spool) spool.addContribution(key, contribution); else stock._contributions.push(contribution);
      }
      currentHoldingRecords += Number(hasCurrent);
      unknownIdentityHoldingRecords += Number(hasCurrent && !identity.aggregationEligible);
    }
  }
  if (portfolios) portfolios.sort((a, b) => a.id.localeCompare(b.id));
  const portfolioFiles = [];
  boundedParts('portfolio-evidence', 'portfolios', spool ? spool.portfolios() : portfolios, 'details-portfolios', phase, maxPartBytes, writePart,
    (file, items) => portfolioFiles.push({ path: file, ids: items.map(item => item.id) }));
  const ordered = [...stocks.values()].sort((a, b) => a.key.localeCompare(b.key));
  function* stockChunks() {
    for (const stock of ordered) {
      let chunk = [], size = 100;
      for (const contribution of spool ? spool.contributions(stock.key) : stock._contributions) {
        const itemSize = Buffer.byteLength(JSON.stringify(contribution)) + 1;
        if (chunk.length && size + itemSize > maxPartBytes - 400) { yield { key: stock.key, contributions: chunk }; chunk = []; size = 100; }
        chunk.push(contribution); size += itemSize;
      }
      if (chunk.length) yield { key: stock.key, contributions: chunk };
    }
  }
  boundedParts('stock-contributions', 'stocks', stockChunks(), 'details', phase, maxPartBytes, writePart,
    (file, items) => { for (const item of items) if (!stocks.get(item.key).details.includes(file)) stocks.get(item.key).details.push(file); });
  for (const stock of ordered) {
    stock.reportManagerCount = stock._reportManagers.size;
    stock.partialReportManagerCount = stock._partialManagers.size;
    stock.tenureOverlapManagerCount = stock._tenureManagers.size;
    stock.currentProfileManagerCount = stock._currentManagers.size;
    stock.priceRange.sourceUrls = [...stock._priceSources].sort().slice(0, 8);
    stock.priceRange.sourceCount = stock._priceSources.size;
    stock.priceRange.sourceUrlsTruncated = stock._priceSources.size > 8;
    stock.priceRange.conflictingObservations = stock._prices.size > 1;
    if (stock._completePrice) {
      const complete = stock._completePrice;
      // A narrower partial observation can agree with a complete interval. Two
      // complete intervals must agree exactly, including when one encloses the
      // other; the envelope alone cannot establish that consistency.
      const consistent = !stock._completePriceConflict
        && stock.priceRange.low === complete.low && stock.priceRange.high === complete.high;
      if (consistent) {
        Object.assign(stock.priceRange, retainedCoverage(complete, stock.market, stock.stockCode, phase.start, phase.target), {
          low: complete.low, high: complete.high, observedStart: complete.observedStart, observedEnd: complete.observedEnd,
          observedCount: complete.observedCount, priceBasis: 'unadjusted', sourceUrl: complete.sourceUrl, conflictingObservations: false });
      } else {
        stock.priceRange.conflictingObservations = true;
        stock.priceRange.completeness = 'partial';
      }
    }
    for (const field of ['disclosedValueYuan', 'navWeightEstimateYuan', 'knownValueYuan', 'comparableSharesDelta']) if (stock[field] !== null) stock[field] = round(stock[field]);
    for (const field of Object.keys(stock).filter(key => key.startsWith('_'))) delete stock[field];
  }
  const research = progress?.research;
  const coverage = { status: awaitingData ? 'awaiting-data' : 'partial', complete: false, inputRecords, ...portfolioCounts,
    currentHoldingRecords, noEquityRecords, reportOnlyRecords, excludedNotApplicable, duplicatesRemoved,
    stockCount: ordered.length, contributionCount: ordered.reduce((sum, stock) => sum + Object.values(stock.changes).reduce((a, b) => a + b, 0), 0),
    identityUnresolvedRecords: unknownIdentityRecords, identityUnresolvedHoldingRecords: unknownIdentityHoldingRecords,
    identityConflictGroups: conflicts, invalidRows, strategyRecords, observedPriceRows: priceRows,
    strategySemanticAnalysisComplete: false,
    completePriceStocks: ordered.filter(stock => stock.priceRange.completeness === 'complete').length,
    incompletePriceStocks: ordered.filter(stock => stock.priceRange.completeness !== 'complete').length,
    sourceCatalogShareCodes: manifest.coverage?.catalogFundCount ?? null,
    sourceProcessedShareCodes: progress?.processedFunds ?? null, sourceResearchCompleteShareCodes: research?.completeFunds ?? null,
    sourceResearchApplicableShareCodes: research?.applicableFunds ?? null,
    sourceCompletionStatus: progress?.completionStatus || 'unknown',
    // A complete current directory is not a certificate for products that
    // disappeared before discovery. No such historical certificate exists yet.
    historicalUniverseVerified: false,
    historicalUniverseStatus: ['current-directory-only', 'supplemented-unverified'].includes(progress?.quarterUniverse?.status)
      && progress.quarterUniverse.targetPeriodEnd === phase.target ? progress.quarterUniverse.status : 'unchecked',
    valueAggregationScope: 'source-verified-portfolios-only', holdingsScope: 'disclosed-holdings-only',
    recordCountScope: 'proven-portfolios-and-unresolved-fund-records', strategyScope: 'report-level-selected-evidence',
    changeCountsScope: 'record-level-disclosure-changes-not-trades',
    managerEvidenceScope: 'bounded-complete-report-manager-tables', managerCountsScope: 'report-period-managers-not-exclusive-period-end-owners', actualCostsKnown: false };
  if (!awaitingData && coverage.historicalUniverseVerified && progress?.completionStatus === 'complete' && Number.isInteger(research?.completeFunds)
    && Number.isInteger(research?.applicableFunds) && research.completeFunds === research.applicableFunds
    && !unknownIdentityHoldingRecords && !conflicts && !invalidRows && !reportOnlyRecords && !coverage.managerEvidenceMissingRecords
    && !coverage.incompletePriceStocks && !coverage.strategySectionIncompleteRecords
    && inputRecords === manifest.coverage?.fundCount) { coverage.status = 'complete'; coverage.complete = true; }
  const output = { schemaVersion: 1, analysisVersion: 6, kind: 'quarter-analysis', targetPeriodEnd: phase.target, baselinePeriodEnd: phase.baseline,
    asOf: manifest.asOf, retrievedAt: manifest.retrievedAt, priceUpdatedAt: manifest.priceUpdatedAt || null,
    generatedAt: phase.generation, generation: phase.generation, coverage,
    stocks: ordered, portfolioFiles,
    themes: [...themes.values()].map(item => ({ ...item, recordCount: item.portfolioIds.length,
      portfolioCount: item.aggregationEligiblePortfolioCount })).sort((a, b) => b.portfolioCount - a.portfolioCount || a.name.localeCompare(b.name)),
    topStockKeys: [...ordered].filter(item => item.knownValueYuan !== null)
      .sort((a, b) => b.knownValueYuan - a.knownValueYuan || a.key.localeCompare(b.key)).slice(0, 100).map(item => item.key) };
  if (Buffer.byteLength(JSON.stringify(output)) > INDEX_LIMIT) fail('ANALYSIS_INDEX_TOO_LARGE');
  return output;
}

function readJson(filename, limit = INDEX_LIMIT) {
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > limit) fail('INVALID_ANALYSIS_INPUT');
  return JSON.parse(fs.readFileSync(filename, 'utf8'));
}

function readAnalysisProgress(filename) {
  if (!fs.existsSync(filename)) return null;
  const value = readJson(filename, 96_000_000);
  return { phase: value.phase, research: value.research, processedFunds: value.processedFunds,
    completionStatus: value.completionStatus, quarterUniverse: value.quarterUniverse };
}

function generate(dataRoot) {
  const root = path.resolve(dataRoot), staging = path.join(root, `.analysis-stage-${process.pid}`);
  if (fs.existsSync(staging)) fail('ANALYSIS_STAGE_EXISTS');
  fs.mkdirSync(staging);
  let spool;
  try {
    const manifest = readJson(path.join(root, 'latest.json'));
    const progress = readAnalysisProgress(path.join(root, 'progress.json'));
    spool = createAnalysisSpool(path.join(staging, '.spool'));
    const output = analyzeQuarter(manifest, file => readJson(path.join(root, file.slice('./data/'.length))), {
      progress, spool, writePart: (file, value) => fs.writeFileSync(path.join(staging, path.basename(file)), `${JSON.stringify(value)}\n`) });
    spool.dispose();
    const destination = path.join(root, 'analysis');
    fs.mkdirSync(destination, { recursive: true });
    // New files become visible before the new index. A failed run never replaces
    // the previous index with an incomplete result.
    const names = new Set(fs.readdirSync(staging));
    for (const name of names) fs.renameSync(path.join(staging, name), path.join(destination, name));
    const temporary = path.join(root, `.analysis-index-${process.pid}.json`);
    fs.writeFileSync(temporary, `${JSON.stringify(output)}\n`);
    fs.renameSync(temporary, path.join(root, 'analysis.json'));
    for (const name of fs.readdirSync(destination)) if (/^details(?:-portfolios)?-\d{4}\.json$/.test(name) && !names.has(name)) fs.unlinkSync(path.join(destination, name));
    fs.rmdirSync(staging);
    return output;
  } catch (error) { fs.rmSync(staging, { recursive: true, force: true }); throw error; }
}

function main(argv = process.argv.slice(2)) {
  try {
    if (process.env.GITHUB_ACTIONS !== 'true') fail('ANALYSIS_REQUIRES_ACTIONS');
    if (![2, 4].includes(argv.length) || argv[0] !== '--data'
      || (argv.length === 4 && (argv[2] !== '--output' || path.resolve(argv[3]) !== path.resolve(argv[1], 'analysis.json')))) fail('INVALID_ANALYSIS_ARGUMENTS');
    const result = generate(argv[1]);
    process.stdout.write(`${JSON.stringify({ status: result.coverage.status, records: result.coverage.analyzedRecords,
      stocks: result.coverage.stockCount, strategyRecords: result.coverage.strategyRecords })}\n`);
    return 0;
  } catch (error) {
    const code = /^(?:INVALID_ANALYSIS_[A-Z_]+|DUPLICATE_ANALYSIS_COMPANY_PATH|ANALYSIS_[A-Z_]+)$/.test(error?.message || '')
      ? error.message : 'ANALYSIS_FAILED';
    process.stdout.write(`${JSON.stringify({ status: 'failed', errorCode: code })}\n`); return 1;
  }
}

if (require.main === module) process.exitCode = main();
module.exports = { analyzeQuarter, generate, main, datesFor, strategyFor, selectRecords };
