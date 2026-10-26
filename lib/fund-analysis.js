'use strict';

const { normalizeLifecycleEvidence } = require('../web/lifecycle');
const { inspectPriceCoverage } = require('./price-coverage');
const { normalizeManagers, verifiedReportManagers, overlaps: managerOverlaps, managerCoverageFor } = require('./report-managers');
const { strategySectionCoverageFor, strategyAnalysisFor } = require('./strategy-sections');
const { normalizePortfolioIdentity } = require('./portfolio-identity');
const { strategyClaimsFor } = require('./strategy-claims');

// Pure, source-bounded calculations. Monetary positions are in CNY yuan;
// market price ranges keep their native quoted currency and are never costs.
const REPORT_KINDS = new Set(['quarterly', 'semiannual', 'annual']);
const COVERAGES = new Set(['top10', 'full', 'unknown']);
const MARKET_CURRENCY = { CN: 'CNY', HK: 'HKD', US: 'USD' };
const PARSE_METHODS = new Set(['mineru', 'pypdf', 'pypdf-fallback']);
const STRATEGY_THEMES = new Set(['行业/配置', '估值/基本面', '调仓/交易', '组合管理', '风险/防守']);

function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function numberOrNull(value, { positive = false, maximum = Infinity } = {}) {
  return typeof value === 'number' && Number.isFinite(value)
    && (positive ? value > 0 : value >= 0) && value <= maximum ? value : null;
}

function textOrNull(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function integerOrNull(value, { minimum = 0, maximum = Infinity } = {}) {
  return Number.isInteger(value) && value >= minimum && value <= maximum ? value : null;
}

function timestampOrNull(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value)) ? value : null;
}

function round(value) {
  return Number.isFinite(value) ? Number(value.toPrecision(13)) : null;
}

function nextDay(date) {
  const parsed = new Date(`${date}T00:00:00.000Z`);
  parsed.setUTCDate(parsed.getUTCDate() + 1);
  return parsed.toISOString().slice(0, 10);
}

function quarterIndex(date) {
  return Number(date.slice(0, 4)) * 4 + Math.floor((Number(date.slice(5, 7)) - 1) / 3);
}

function quarterStart(date) {
  const month = Math.floor((Number(date.slice(5, 7)) - 1) / 3) * 3 + 1;
  return `${date.slice(0, 4)}-${String(month).padStart(2, '0')}-01`;
}

function holdingKey(holding) {
  return `${holding.market || 'unknown'}:${holding.stockCode}`;
}

function normalizeHolding(holding) {
  if (!holding || !textOrNull(holding.stockCode)) return null;
  const market = Object.hasOwn(MARKET_CURRENCY, holding.market) ? holding.market : null;
  return {
    stockCode: holding.stockCode.trim(),
    stockName: textOrNull(holding.stockName),
    market,
    currency: textOrNull(holding.currency) || MARKET_CURRENCY[market] || null,
    shares: numberOrNull(holding.shares),
    weightPct: numberOrNull(holding.weightPct, { maximum: 100 }),
    marketValueYuan: numberOrNull(holding.marketValueYuan),
    ...(holding.sourceRank !== undefined ? {
      sourceRank: integerOrNull(holding.sourceRank, { minimum: 1, maximum: 100000 }),
      sourcePage: integerOrNull(holding.sourcePage, { minimum: 1, maximum: 10000 }),
      sourceEndPage: integerOrNull(holding.sourceEndPage, { minimum: 1, maximum: 10000 }),
      sourceRankCell: Array.isArray(holding.sourceRankCell) && holding.sourceRankCell.length === 5
        ? holding.sourceRankCell.map((value, index) => index === 0
          ? integerOrNull(value, { minimum: 1, maximum: 10000 }) : numberOrNull(value, { maximum: 10000 })) : null,
      sourceStockCode: textOrNull(holding.sourceStockCode)?.slice(0, 20) || null,
      sourceExchange: textOrNull(holding.sourceExchange)?.slice(0, 80) || null,
      sourceCountry: textOrNull(holding.sourceCountry)?.slice(0, 40) || null,
      marketValueCurrency: holding.marketValueCurrency === 'CNY' ? 'CNY' : null,
    } : {}),
  };
}

function validHoldingIdentity(holding) {
  return Boolean(holding && holding.currency === MARKET_CURRENCY[holding.market]
    && (holding.market === 'CN' ? /^\d{6}$/.test(holding.stockCode)
      : holding.market === 'HK' ? /^\d{5}$/.test(holding.stockCode)
        : holding.market === 'US' && /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/.test(holding.stockCode)));
}

function validCellHoldings(evidence, holdings) {
  let previousRank = 0, previousCell = null, groups = 0, continued = 0;
  for (const holding of holdings) {
    const cell = holding.sourceRankCell;
    if (!cell || cell.some(value => value === null) || cell[0] !== holding.sourcePage || cell[1] >= cell[2] || cell[3] >= cell[4]
      || holding.sourcePage < evidence.startPage || holding.sourceEndPage < holding.sourcePage || holding.sourceEndPage > evidence.endPage
      || holding.marketValueCurrency !== 'CNY' || !holding.sourceRank) return false;
    const key = cell.join(':');
    if (holding.sourceRank === previousRank) {
      if (key !== previousCell) return false;
    } else {
      if (holding.sourceRank !== previousRank + 1 || key === previousCell) return false;
      groups++;
    }
    previousRank = holding.sourceRank; previousCell = key;
    continued += holding.sourceEndPage - holding.sourcePage;
    const exchange = holding.sourceExchange, country = holding.sourceCountry;
    if (holding.market === 'US') {
      if (country !== '美国' || !['纳斯达克证券交易所', '美国纽约证券交易所', '纽约证券交易所', '美国证券交易所'].includes(exchange)
        || !/^[A-Z][A-Z0-9]{0,9}(?:[./-][A-Z0-9]{1,3})?$/.test(holding.sourceStockCode || '')
        || holding.sourceStockCode.replace(/[/-]/g, '.') !== holding.stockCode) return false;
    } else if (holding.market === 'HK') {
      if (!['中国香港', '香港'].includes(country) || exchange !== '香港联合交易所') return false;
    } else if (!['中国', '中国内地', '中国大陆'].includes(country)
      || !['上海证券交易所', '深圳证券交易所', '北京证券交易所'].includes(exchange)) return false;
  }
  return evidence.marketValueCurrency === 'CNY' && groups === evidence.rankGroups
    && holdings.length - groups === evidence.mergedSecurityRows && continued === evidence.continuedRows;
}

function normalizeHoldingsPdfEvidence(raw, report, holdings, invalidCount, duplicateCount) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const section = value => typeof value === 'string' && /^\d{1,3}(?:\.\d{1,3}){1,2}$/.test(value) ? value : null;
  const evidence = {
    sourceUrl: textOrNull(raw.sourceUrl), periodEnd: validDate(raw.periodEnd) ? raw.periodEnd : null,
    publishedAt: validDate(raw.publishedAt) ? raw.publishedAt : null,
    kind: REPORT_KINDS.has(raw.kind) ? raw.kind : null,
    sourceTextHash: typeof raw.sourceTextHash === 'string' && /^[a-f0-9]{64}$/.test(raw.sourceTextHash) ? raw.sourceTextHash : null,
    parseMethod: PARSE_METHODS.has(raw.parseMethod) ? raw.parseMethod : null,
    coverage: ['full', 'top10'].includes(raw.coverage) ? raw.coverage : 'unknown',
    sectionNumber: section(raw.sectionNumber), nextSectionNumber: section(raw.nextSectionNumber),
    sectionBounded: raw.sectionBounded === true, contiguousRanks: raw.contiguousRanks === true,
    unitsVerified: raw.unitsVerified === true,
    documentTruncated: typeof raw.documentTruncated === 'boolean' ? raw.documentTruncated : null,
    pageNumbersVerified: raw.pageNumbersVerified === true,
    ...Object.fromEntries(['totalPages', 'processedPages', 'startPage', 'endPage'].map(key =>
      [key, integerOrNull(raw[key], { minimum: 1, maximum: 10000 })])),
    ...Object.fromEntries(['rowsSeen', 'parsedRows', 'excludedRows'].map(key =>
      [key, integerOrNull(raw[key], { minimum: 0, maximum: 100000 })])),
    ...(raw.rankMode !== undefined ? { rankMode: raw.rankMode === 'pdf-cells' ? 'pdf-cells' : null,
      marketValueCurrency: raw.marketValueCurrency === 'CNY' ? 'CNY' : null,
      ...Object.fromEntries(['rankGroups', 'mergedSecurityRows', 'continuedRows'].map(key =>
        [key, integerOrNull(raw[key], { minimum: 0, maximum: 100000 })])) } : {}),
  };
  const previous = evidence.sectionNumber?.split('.').map(Number) || [];
  const following = evidence.nextSectionNumber?.split('.').map(Number) || [];
  const unequal = following.findIndex((value, index) => value !== previous[index]);
  const forward = previous.length && following.length && unequal >= 0
    && (previous[unequal] === undefined || following[unequal] > previous[unequal]);
  const cellTable = evidence.rankMode === 'pdf-cells' && report.holdingsRetrieval === 'pdf-stock-table-cells';
  const validTable = cellTable ? validCellHoldings(evidence, holdings)
    : report.holdingsRetrieval === 'pdf-stock-table-text' && !evidence.rankMode && holdings.every(holding => holding.market !== 'US');
  evidence.complete = Boolean(raw.complete === true && validTable
    && evidence.sourceUrl && evidence.sourceUrl === report.sourceUrl && evidence.sourceUrl === report.holdingsSourceUrl
    && evidence.periodEnd === report.periodEnd && evidence.publishedAt === report.publishedAt && evidence.kind === report.kind
    && evidence.sourceTextHash && ['pypdf', 'pypdf-fallback'].includes(evidence.parseMethod)
    && evidence.coverage === report.coverage && evidence.coverage !== 'unknown'
    && evidence.sectionBounded && forward && evidence.contiguousRanks && evidence.unitsVerified
    && holdings.length > 0 && evidence.rowsSeen === holdings.length && evidence.parsedRows === holdings.length
    && evidence.excludedRows === 0 && !invalidCount && !duplicateCount
    && (evidence.coverage !== 'top10' || (cellTable ? evidence.rankGroups : holdings.length) <= 10)
    && holdings.every(holding => holding.shares !== null && holding.marketValueYuan !== null && holding.weightPct !== null
      && validHoldingIdentity(holding))
    && holdings.reduce((total, holding) => total + holding.weightPct, 0) <= 100.1
    && evidence.totalPages !== null && evidence.processedPages === evidence.totalPages
    && evidence.documentTruncated === false && evidence.pageNumbersVerified
    && evidence.startPage !== null && evidence.endPage !== null
    && evidence.startPage <= evidence.endPage && evidence.endPage <= evidence.totalPages);
  return evidence;
}

function normalizeReport(report, options = {}) {
  options = options && typeof options === 'object' ? options : {};
  if (!report || !validDate(report.periodEnd) || !validDate(report.publishedAt)
    || report.publishedAt < report.periodEnd || (!REPORT_KINDS.has(report.kind)
      && !(report.kind === 'unknown' && report.noEquitiesConfirmed === true))) return null;
  const holdings = [];
  const seen = new Set();
  let duplicateHoldingCount = 0;
  let invalidHoldingCount = 0;
  for (const raw of Array.isArray(report.holdings) ? report.holdings : []) {
    const holding = normalizeHolding(raw);
    if (!holding) { invalidHoldingCount += 1; continue; }
    const key = holdingKey(holding);
    if (seen.has(key)) { duplicateHoldingCount += 1; continue; }
    seen.add(key);
    holdings.push(holding);
  }
  let rawNarrative = report.narrative || {};
  let narrativeText = textOrNull(rawNarrative.text);
  const holdingsPdfEvidence = normalizeHoldingsPdfEvidence(report.holdingsPdfEvidence, report, holdings,
    invalidHoldingCount, duplicateHoldingCount);
  const pdfHoldingsUnproven = ['pdf-stock-table-text', 'pdf-stock-table-cells'].includes(report.holdingsRetrieval) && !holdingsPdfEvidence?.complete;
  const extraction = {};
  if (report.parseMethod !== undefined) {
    extraction.parseMethod = PARSE_METHODS.has(report.parseMethod) ? report.parseMethod : null;
  }
  if (report.parseErrorCode !== undefined) {
    extraction.parseErrorCode = typeof report.parseErrorCode === 'string'
      && /^[A-Z0-9_]{2,64}$/.test(report.parseErrorCode) ? report.parseErrorCode : null;
  }
  if (report.sourceTextHash !== undefined) {
    extraction.sourceTextHash = typeof report.sourceTextHash === 'string'
      && /^[a-f0-9]{64}$/i.test(report.sourceTextHash) ? report.sourceTextHash.toLowerCase() : null;
  }
  if (report.pageStats !== undefined) {
    const stats = report.pageStats || {};
    extraction.pageStats = {
      pageCount: integerOrNull(stats.pageCount, { minimum: 0, maximum: 10000 }),
      nonEmptyPages: integerOrNull(stats.nonEmptyPages, { minimum: 0, maximum: 10000 }),
      charCount: integerOrNull(stats.charCount, { minimum: 0, maximum: 100000000 }),
      markdownBytes: integerOrNull(stats.markdownBytes, { minimum: 0, maximum: 100000000 }),
      totalPages: integerOrNull(stats.totalPages, { minimum: 1, maximum: 10000 }),
      processedPages: integerOrNull(stats.processedPages, { minimum: 0, maximum: 10000 }),
      truncated: typeof stats.truncated === 'boolean' ? stats.truncated : null,
      pageNumbersVerified: typeof stats.pageNumbersVerified === 'boolean' ? stats.pageNumbersVerified : null,
      textStatus: ['usable', 'sparse', 'empty'].includes(stats.textStatus) ? stats.textStatus : null,
    };
  }
  if (report.strategyExcerpts !== undefined) {
    extraction.strategyExcerpts = (Array.isArray(report.strategyExcerpts) ? report.strategyExcerpts : [])
      .slice(0, 8).map(item => item && ({
        text: textOrNull(item.text)?.slice(0, 260) || null,
        sourceUrl: textOrNull(item.sourceUrl),
        page: integerOrNull(item.page, { minimum: 1, maximum: 10000 }),
        periodStart: validDate(item.periodStart) ? item.periodStart : null,
        periodEnd: validDate(item.periodEnd) ? item.periodEnd : null,
      })).filter(item => item && item.text && item.sourceUrl);
  }
  if (report.strategyThemes !== undefined) {
    extraction.strategyThemes = [...new Set((Array.isArray(report.strategyThemes) ? report.strategyThemes : [])
      .filter(theme => STRATEGY_THEMES.has(theme)))];
  }
  if (report.strategySectionCoverage !== undefined) {
    extraction.strategySectionCoverage = strategySectionCoverageFor(report.strategySectionCoverage, report.periodEnd);
    extraction.strategyAnalysis = strategyAnalysisFor(report.strategyAnalysis, extraction.strategySectionCoverage);
    extraction.strategyEvidenceRetrievedAt = timestampOrNull(report.strategyEvidenceRetrievedAt);
    if (report.strategyEvidencePool == null) {
      extraction.strategyEvidenceBudget = require('./strategy-evidence-pool').strategyEvidenceBudgetFor(report, options.now || new Date());
    }
  }
  if (extraction.strategyEvidenceBudget === undefined && report.strategyEvidencePool == null
    && (narrativeText || report.strategyExcerpts?.length)) {
    extraction.strategyEvidenceBudget = require('./strategy-evidence-pool').strategyEvidenceBudgetFor(report, options.now || new Date());
  }
  if (report.strategyEvidencePool !== undefined || report.strategyClaims !== undefined) {
    const { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor } = require('./strategy-evidence-pool');
    extraction.strategyEvidencePool = normalizeStrategyEvidencePool(report.strategyEvidencePool,
      extraction.strategySectionCoverage, {}, options.now || new Date());
    extraction.strategyEvidenceBudget = strategyEvidenceBudgetFor(report, options.now || new Date());
    const claims = strategyClaimsFor(report, { code: options.code || report.fundCode || report.sourceFundCode,
      now: options.now || new Date(), asOf: options.asOf || null });
    extraction.strategyClaims = claims;
    extraction.strategyClaimsStatus = claims ? 'scanned' : report.strategyClaims?.ruleScanComplete === false
      || report.strategyClaims == null && report.strategyClaimsStatus === 'unavailable' ? 'unavailable' : 'invalid';
    if (claims && report.strategyClaimsDerivedFromObservedAt != null) {
      extraction.strategyClaimsDerivedFromObservedAt = report.strategyClaimsDerivedFromObservedAt;
    }
    const source = claims?.source || extraction.strategyEvidencePool?.source;
    extraction.strategyClaimsIdentityEvidence = source ? normalizePortfolioIdentity(report.strategyClaimsIdentityEvidence,
      { ...source, fundCode: options.code || report.fundCode || report.sourceFundCode }, options.now || new Date()) : null;
    if (extraction.strategyEvidenceBudget?.status === 'verified') {
      // The verified pool already bounds Unicode code points. Preserve the
      // literal projection, including astral characters, without re-slicing it.
      extraction.strategyExcerpts = (report.strategyExcerpts || []).map(item => ({
        text: item.text, sourceUrl: item.sourceUrl, page: item.page,
        periodStart: item.periodStart, periodEnd: item.periodEnd, evidencePoolId: item.evidencePoolId,
      }));
      for (const quote of extraction.strategyAnalysis?.evidence || []) {
        const original = report.strategyAnalysis?.evidence?.find(item => item.id === quote.id && item.text === quote.text);
        if (original?.evidencePoolId) quote.evidencePoolId = original.evidencePoolId;
      }
    } else if (report.strategyEvidencePool != null || extraction.strategyEvidenceBudget?.status === 'partial') {
      // New analysis cannot publish extra unpooled text under a partial label.
      // Keep its bounded canonical pool and source proof for inspection; the
      // sticky rejection prevents a later normalization from claiming repair.
      rawNarrative = {}; narrativeText = null;
      extraction.strategyExcerpts = [];
      extraction.strategyAnalysis = null;
    }
  }
  return {
    periodEnd: report.periodEnd,
    publishedAt: report.publishedAt,
    kind: report.kind,
    portfolioIdentityEvidence: normalizePortfolioIdentity(report.portfolioIdentityEvidence, report),
    sourceLocatorEvidence: report.sourceLocatorEvidence == null ? null
      : require('./report-locators').normalizeSourceLocatorEvidence(report.sourceLocatorEvidence, report),
    coverage: !pdfHoldingsUnproven && COVERAGES.has(report.coverage) ? report.coverage : 'unknown',
    navYuan: numberOrNull(report.navYuan, { positive: true }),
    reportOnly: report.reportOnly === true,
    evidenceRetrievedAt: timestampOrNull(report.evidenceRetrievedAt),
    navRetrievedAt: timestampOrNull(report.navRetrievedAt),
    reportPdfSourceUrl: textOrNull(report.reportPdfSourceUrl),
    reportPdfPeriodEnd: validDate(report.reportPdfPeriodEnd) ? report.reportPdfPeriodEnd : null,
    reportPdfKind: REPORT_KINDS.has(report.reportPdfKind) ? report.reportPdfKind : null,
    sourceUrl: textOrNull(report.sourceUrl),
    fundCode: textOrNull(report.fundCode),
    sourceFundCode: textOrNull(report.sourceFundCode),
    holdingsSourceUrl: textOrNull(report.holdingsSourceUrl),
    holdingsRetrieval: textOrNull(report.holdingsRetrieval),
    holdingsRequestedLimit: integerOrNull(report.holdingsRequestedLimit, { minimum: 1, maximum: 10000 }),
    holdingsPdfEvidence,
    holdingsPossiblyTruncated: pdfHoldingsUnproven || report.holdingsPossiblyTruncated === true,
    holdingsIncomplete: report.holdingsIncomplete === true,
    holdingsExcludedRowCount: integerOrNull(report.holdingsExcludedRowCount, { minimum: 0, maximum: 100000 }),
    holdingsIncompleteReason: report.holdingsIncompleteReason === 'UNSUPPORTED_OR_INVALID_SECURITY_ROWS'
      ? 'UNSUPPORTED_OR_INVALID_SECURITY_ROWS' : null,
    navSourceUrl: textOrNull(report.navSourceUrl) || (numberOrNull(report.navYuan, { positive: true }) !== null
      ? textOrNull(report.sourceUrl) : null),
    managers: normalizeManagers(report.managers, report.periodEnd),
    managerTableCoverage: managerCoverageFor(report.managerTableCoverage, normalizeManagers(report.managers, report.periodEnd), report.periodEnd),
    managerSourceUrl: normalizeManagers(report.managers, report.periodEnd).find(item => item.evidence)?.evidence.sourceUrl || null,
    managerEvidenceRetrievedAt: timestampOrNull(report.managerEvidenceRetrievedAt),
    noEquitiesConfirmed: report.noEquitiesConfirmed === true,
    noEquitiesEvidence: report.noEquitiesConfirmed === true && report.noEquitiesEvidence ?
      (report.noEquitiesEvidence.evidenceType === 'exhaustive-nonstock-assets'
        ? require('../web/nonstock-assets').normalizeNonstockAssets(report.noEquitiesEvidence) : {
      confirmed: report.noEquitiesEvidence.confirmed === true,
      sourceUrl: textOrNull(report.noEquitiesEvidence.sourceUrl),
      periodEnd: report.noEquitiesEvidence.periodEnd,
      publishedAt: report.noEquitiesEvidence.publishedAt,
      page: numberOrNull(report.noEquitiesEvidence.page, { positive: true }),
      text: textOrNull(report.noEquitiesEvidence.text)?.slice(0, 600) || null,
      kind: textOrNull(report.noEquitiesEvidence.kind),
      ...(['zero-stock-balance-sheet', 'explicit-no-stock-detail'].includes(report.noEquitiesEvidence.evidenceType) ? { evidenceType: report.noEquitiesEvidence.evidenceType } : {}),
      ...(report.noEquitiesEvidence.evidenceType === 'empty-stock-detail-section' ? {
        evidenceType: report.noEquitiesEvidence.evidenceType,
        assetAllocationText: textOrNull(report.noEquitiesEvidence.assetAllocationText)?.slice(0, 600) || null,
      } : {}),
      title: textOrNull(report.noEquitiesEvidence.title),
    }) : null,
    title: textOrNull(report.title),
    holdings,
    duplicateHoldingCount,
    invalidHoldingCount,
    narrative: {
      text: narrativeText ? narrativeText.slice(0, 600) : null,
      truncated: Boolean(narrativeText && narrativeText.length > 600),
      sourceUrl: textOrNull(rawNarrative.sourceUrl) || textOrNull(report.sourceUrl),
      page: numberOrNull(rawNarrative.page, { positive: true }),
      periodStart: validDate(rawNarrative.periodStart) ? rawNarrative.periodStart : null,
      periodEnd: validDate(rawNarrative.periodEnd) ? rawNarrative.periodEnd : report.periodEnd,
      attribution: 'report',
      isInference: false,
      excerptSelection: textOrNull(rawNarrative.excerptSelection),
      ...(extraction.strategyEvidenceBudget?.status === 'verified' && rawNarrative.evidencePoolId
        ? { evidencePoolId: rawNarrative.evidencePoolId } : {}),
    },
    ...extraction,
  };
}

function selectReports(reports, { asOf, periodEnd, baselinePeriodEnd, now, code } = {}) {
  if (!validDate(asOf)) throw new TypeError('INVALID_AS_OF');
  if (periodEnd !== undefined && !validDate(periodEnd)) throw new TypeError('INVALID_PERIOD_END');
  if (baselinePeriodEnd !== undefined && !validDate(baselinePeriodEnd)) throw new TypeError('INVALID_BASELINE_PERIOD_END');
  const priority = { full: 2, top10: 1, unknown: 0 };
  const available = (Array.isArray(reports) ? reports : [])
    .map(report => normalizeReport(report, { asOf, now, code })).filter(Boolean)
    .filter(report => report.periodEnd <= asOf && report.publishedAt <= asOf)
    .sort((a, b) => b.periodEnd.localeCompare(a.periodEnd)
      || priority[b.coverage] - priority[a.coverage]
      || b.publishedAt.localeCompare(a.publishedAt));
  const selectedReport = available.find(report => !periodEnd || report.periodEnd === periodEnd) || null;
  const previousReport = selectedReport
    ? available.find(report => report.periodEnd < selectedReport.periodEnd
      && (!baselinePeriodEnd || report.periodEnd === baselinePeriodEnd)
      && quarterIndex(report.periodEnd) < quarterIndex(selectedReport.periodEnd)) || null
    : null;
  return { selectedReport, previousReport };
}

function makeInterval(selectedReport, previousReport) {
  if (!selectedReport) return null;
  const quarterSpan = previousReport
    ? quarterIndex(selectedReport.periodEnd) - quarterIndex(previousReport.periodEnd) : 1;
  const adjacent = previousReport ? quarterSpan === 1 : null;
  return {
    start: previousReport ? nextDay(previousReport.periodEnd) : quarterStart(selectedReport.periodEnd),
    end: selectedReport.periodEnd,
    adjacent,
    quarterSpan,
    label: !previousReport ? '本报告季度（基期缺失）'
      : adjacent ? '相邻季度披露间隔' : `跨 ${quarterSpan} 个季度的披露间隔（非单季）`,
  };
}

function compareHolding(current, previous, hasPreviousReport) {
  const result = {
    kind: 'not-comparable', label: '持股变化未知', sharesDelta: null,
    sharesPctChange: null, weightDeltaPp: null, reason: null, provesTrade: false,
  };
  if (!hasPreviousReport) {
    return { ...result, kind: 'no-baseline', label: '基期缺失', reason: '无可用的上期报告，无法比较。' };
  }
  if (!previous) {
    return { ...result, kind: 'entered-disclosure', label: '进入披露名单',
      reason: '上期未披露此证券；进入披露名单不能证明本期新买入。' };
  }
  if (!current) {
    return { ...result, kind: 'left-disclosure', label: '退出披露名单',
      reason: '本期未披露此证券；退出披露名单不能证明清仓，未披露值不按零处理。' };
  }
  if (current.weightPct !== null && previous.weightPct !== null) {
    result.weightDeltaPp = round(current.weightPct - previous.weightPct);
  }
  if (holdingKey(current) !== holdingKey(previous) || current.currency !== previous.currency
    || !current.market || current.shares === null || previous.shares === null) {
    result.reason = '缺少可比较的持股数量或证券口径不一致；权重变化也可能来自价格、申赎及规模变化。';
    return result;
  }
  result.sharesDelta = round(current.shares - previous.shares);
  result.sharesPctChange = previous.shares > 0
    ? round(result.sharesDelta / previous.shares * 100) : null;
  result.kind = result.sharesDelta > 0 ? 'increase' : result.sharesDelta < 0 ? 'decrease' : 'unchanged';
  result.label = result.sharesDelta > 0 ? '持股增加' : result.sharesDelta < 0 ? '持股减少' : '持股数量不变';
  result.reason = '披露股数变化包含交易及送转股等公司行为的影响，不直接认定为买卖；权重差为百分点。';
  return result;
}

function estimatePosition(holding, report) {
  const disclosedYuan = holding ? numberOrNull(holding.marketValueYuan) : null;
  const nav = report ? numberOrNull(report.navYuan, { positive: true }) : null;
  const weight = holding ? numberOrNull(holding.weightPct, { maximum: 100 }) : null;
  const estimatedYuan = nav !== null && weight !== null ? round(nav * weight / 100) : null;
  const discrepancyYuan = disclosedYuan !== null && estimatedYuan !== null
    ? round(estimatedYuan - disclosedYuan) : null;
  return {
    disclosedYuan,
    estimatedYuan,
    estimateNote: weight === 0 && disclosedYuan > 0 ? '披露占比四舍五入为零，请采用披露市值' : null,
    valueYuan: disclosedYuan !== null ? disclosedYuan : estimatedYuan,
    method: disclosedYuan !== null ? 'disclosed' : estimatedYuan !== null ? 'nav-weight' : 'unavailable',
    discrepancyYuan,
    discrepancyPct: discrepancyYuan !== null && disclosedYuan > 0
      ? round(discrepancyYuan / disclosedYuan * 100) : null,
    currency: 'CNY',
    periodEnd: report ? report.periodEnd : null,
  };
}

function priceRange(holding, series, interval) {
  const result = {
    status: 'missing', label: '缺少区间行情', low: null, high: null,
    currency: holding ? holding.currency : null,
    priceBasis: series ? textOrNull(series.priceBasis) : null,
    rangeStart: interval ? interval.start : null,
    rangeEnd: interval ? interval.end : null,
    observedStart: null, observedEnd: null, observedCount: 0,
    rejectedRowCount: 0, completeness: 'not-verified',
    isCost: false, actualCostKnown: false,
    costExplanation: '区间最低价与最高价仅作行情参考；建仓日期、成交价格和历史持仓成本未知。',
    sourceUrl: series ? textOrNull(series.sourceUrl) : null,
    sourceUrls: Array.isArray(series?.sourceUrls) ? series.sourceUrls.slice(0, 8) : [],
    market: holding?.market || null, stockCode: holding?.stockCode || null,
  };
  if (!interval || !validDate(interval.start) || !validDate(interval.end) || interval.start > interval.end) {
    return { ...result, status: 'unavailable-interval', label: '缺少可用价格区间' };
  }
  if (!series) return result;
  const expectedCurrency = holding && MARKET_CURRENCY[holding.market];
  if (!expectedCurrency || holding.currency !== expectedCurrency || series.currency !== expectedCurrency) {
    return { ...result, status: 'unsupported-currency', label: '行情币种或市场口径不匹配' };
  }
  if (series.priceBasis !== 'unadjusted') {
    return { ...result, status: 'invalid-price-basis', label: '缺少不复权行情' };
  }
  const daily = new Map();
  for (const row of Array.isArray(series.rows) ? series.rows : []) {
    if (!row || !validDate(row.date)) { result.rejectedRowCount += 1; continue; }
    if (row.date < interval.start || row.date > interval.end) continue;
    const low = numberOrNull(row.low, { positive: true });
    const high = numberOrNull(row.high, { positive: true });
    if (low === null || high === null || low > high) { result.rejectedRowCount += 1; continue; }
    if (daily.has(row.date)) { result.rejectedRowCount += 1; continue; }
    daily.set(row.date, { date: row.date, low, high });
  }
  const observations = [...daily.values()].sort((a, b) => a.date.localeCompare(b.date));
  const coverage = inspectPriceCoverage(holding, series, interval, observations, result.rejectedRowCount);
  if (!observations.length) return { ...result, ...coverage };
  return {
    ...result, ...coverage, status: 'observed', label: coverage.completeness === 'complete'
      ? '完整交易日区间价格（非持仓成本）' : '已观测区间价格（非持仓成本；交易日覆盖未完整）',
    low: Math.min(...observations.map(row => row.low)),
    high: Math.max(...observations.map(row => row.high)),
    observedStart: observations[0].date,
    observedEnd: observations[observations.length - 1].date,
    observedCount: observations.length,
  };
}

function analyzeFund(fund, options = {}) {
  if (!fund || !textOrNull(fund.code)) throw new TypeError('INVALID_FUND_CODE');
  const { selectedReport, previousReport } = selectReports(fund.reports, { ...options, code: fund.code });
  const interval = makeInterval(selectedReport, previousReport);
  const currentMap = new Map((selectedReport ? selectedReport.holdings : []).map(h => [holdingKey(h), h]));
  const previousMap = new Map((previousReport ? previousReport.holdings : []).map(h => [holdingKey(h), h]));
  const keys = new Set(selectedReport?.reportOnly ? [] : [...currentMap.keys(), ...previousMap.keys()]);
  const bars = options.barsByStock || {};
  const rows = [...keys].map(key => {
    const current = currentMap.get(key) || null;
    const previous = previousMap.get(key) || null;
    const identity = current || previous;
    return {
      stockCode: identity.stockCode, stockName: identity.stockName,
      market: identity.market, currency: identity.currency,
      current, previous,
      change: compareHolding(current, previous, Boolean(previousReport)),
      position: estimatePosition(current, selectedReport),
      priceRange: priceRange(identity, bars[key] || bars[identity.stockCode], interval),
    };
  });
  const count = kind => rows.filter(row => row.change.kind === kind).length;
  const valued = rows.filter(row => row.current && row.position.valueYuan !== null);
  const warnings = [];
  if (!selectedReport) warnings.push('没有在查询日期前已披露的合格报告。');
  else if (!previousReport) warnings.push('基期缺失，不能判断持股变化。');
  if (interval && interval.adjacent === false) warnings.push('比较区间跨越多个季度，不能归为单季变化。');
  if ([selectedReport, previousReport].some(report => report && report.coverage !== 'full')) {
    warnings.push('披露名单不完整，未出现的证券不能按零持仓处理。');
  }
  if ([selectedReport, previousReport].some(report => report && report.duplicateHoldingCount)) {
    warnings.push('重复证券披露行仅保留首条，未合计。');
  }
  const knownPositionYuan = valued.length ? round(valued.reduce((sum, row) => sum + row.position.valueYuan, 0)) : null;
  const managers = normalizeManagers(fund.managers);
  const reportStart = selectedReport ? quarterStart(selectedReport.periodEnd) : null;
  const managerReports = selectedReport ? (fund.reports || []).map(report => normalizeReport(report, { ...options, code: fund.code })).filter(report => report
    && report.periodEnd === selectedReport.periodEnd && report.publishedAt <= options.asOf
    && (!report.fundCode || report.fundCode === fund.code))
    .sort((a, b) => Number(b.managerTableCoverage?.complete === true) - Number(a.managerTableCoverage?.complete === true)
      || Number(b.kind === 'quarterly') - Number(a.kind === 'quarterly') || b.publishedAt.localeCompare(a.publishedAt)) : [];
  const managerReport = managerReports.find(report => verifiedReportManagers(report.managers, reportStart, selectedReport.periodEnd).length);
  const strategyReport = selectedReport ? [selectedReport, ...managerReports].filter(report => {
    const proof = report.strategySectionCoverage;
    return proof?.kind === 'quarterly' && proof.periodStart === reportStart
      || report.narrative?.periodStart === reportStart && report.narrative?.periodEnd === selectedReport.periodEnd
      || report.strategyExcerpts?.some(item => item.periodStart === reportStart && item.periodEnd === selectedReport.periodEnd);
  }).sort((a, b) => Number(b.strategySectionCoverage?.complete === true) - Number(a.strategySectionCoverage?.complete === true)
    || (b.strategySectionCoverage?.publishedAt || b.publishedAt).localeCompare(a.strategySectionCoverage?.publishedAt || a.publishedAt)
    || Number(Boolean(b.strategyClaims)) - Number(Boolean(a.strategyClaims)))[0] : null;
  if (strategyReport && strategyReport !== selectedReport) {
    for (const key of ['strategySectionCoverage', 'strategyAnalysis', 'strategyEvidenceRetrievedAt', 'strategyExcerpts', 'strategyThemes', 'narrative',
      'strategyClaims', 'strategyClaimsStatus', 'strategyClaimsIdentityEvidence', 'strategyClaimsDerivedFromObservedAt', 'strategyEvidencePool', 'strategyEvidenceBudget']) {
      if (strategyReport[key] !== undefined) selectedReport[key] = strategyReport[key];
    }
  }
  const explicitManagers = managerReport ? verifiedReportManagers(managerReport.managers, reportStart, selectedReport.periodEnd) : [];
  const reportManagers = explicitManagers.length ? explicitManagers : managers.filter(manager => selectedReport
    && managerOverlaps(manager, reportStart, selectedReport.periodEnd));
  const managerAttribution = explicitManagers.length ? 'report' : reportManagers.length ? 'tenure-overlap' : 'current-profile';
  if (managers.length && managerAttribution === 'current-profile') {
    warnings.push('经理名单来自当前概况，任期未核验，不能默认归属于历史报告。');
  }
  return {
    code: fund.code.trim(), name: textOrNull(fund.name), company: textOrNull(fund.company),
    companyId: textOrNull(fund.companyId),
    sourcePortfolioCode: textOrNull(fund.sourcePortfolioCode),
    portfolioSourceUrl: textOrNull(fund.portfolioSourceUrl),
    managers, reportManagers, managerAttribution,
    managerTableCoverage: managerReport?.managerTableCoverage || null,
    managerCoverageStatus: explicitManagers.length ? managerReport?.managerTableCoverage?.complete ? 'complete' : 'partial' : 'missing',
    managersAsOf: validDate(fund.managersAsOf) ? fund.managersAsOf : null,
    managerScope: textOrNull(fund.managerScope) || 'current-profile',
    managerSourceUrl: explicitManagers.length ? explicitManagers[0].evidence.sourceUrl
      : textOrNull(fund.managerSourceUrl) || textOrNull(fund.metadataSourceUrl),
    managerEvidenceRetrievedAt: managerReport?.managerEvidenceRetrievedAt || null,
    metadataSourceUrl: textOrNull(fund.metadataSourceUrl),
    ...require('./historical-inception').inceptionState(fund),
    shareClassCodes: [...new Set((Array.isArray(fund.shareClassCodes) ? fund.shareClassCodes : []).filter(textOrNull))],
    asOf: options.asOf, status: selectedReport && !selectedReport.reportOnly ? 'ok' : 'unavailable',
    selectedReport, previousReport, interval, rows,
    narrative: selectedReport ? selectedReport.narrative : null,
    summary: {
      currentHoldingCount: currentMap.size, previousHoldingCount: previousMap.size,
      increasedCount: count('increase'), decreasedCount: count('decrease'),
      enteredDisclosureCount: count('entered-disclosure'), leftDisclosureCount: count('left-disclosure'),
      knownPositionYuan, knownPositionCount: valued.length,
      observedPriceCount: rows.filter(row => row.priceRange.status === 'observed').length,
      positionCoverage: 'disclosed-holdings-only',
      aggregatesShareClasses: false,
    },
    warnings,
  };
}

module.exports = {
  analyzeFund, compareHolding, estimatePosition, makeInterval, normalizeHolding,
  normalizeReport, normalizeLifecycleEvidence, numberOrNull, priceRange, selectReports, validDate, validHoldingIdentity,
};
