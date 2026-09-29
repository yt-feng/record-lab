'use strict';

// Pure, source-bounded calculations. Monetary positions are in CNY yuan;
// market price ranges keep their native quoted currency and are never costs.
const REPORT_KINDS = new Set(['quarterly', 'semiannual', 'annual']);
const COVERAGES = new Set(['top10', 'full', 'unknown']);
const MARKET_CURRENCY = { CN: 'CNY', HK: 'HKD' };

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

function normalizeManagers(managers) {
  return (Array.isArray(managers) ? managers : []).filter(manager => manager && textOrNull(manager.name))
    .map(manager => ({ name: manager.name.trim(), startDate: validDate(manager.startDate) ? manager.startDate : null,
      endDate: validDate(manager.endDate) ? manager.endDate : null }));
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
  };
}

function normalizeReport(report) {
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
  const rawNarrative = report.narrative || {};
  const narrativeText = textOrNull(rawNarrative.text);
  return {
    periodEnd: report.periodEnd,
    publishedAt: report.publishedAt,
    kind: report.kind,
    coverage: COVERAGES.has(report.coverage) ? report.coverage : 'unknown',
    navYuan: numberOrNull(report.navYuan, { positive: true }),
    sourceUrl: textOrNull(report.sourceUrl),
    fundCode: textOrNull(report.fundCode),
    sourceFundCode: textOrNull(report.sourceFundCode),
    holdingsSourceUrl: textOrNull(report.holdingsSourceUrl),
    holdingsRetrieval: textOrNull(report.holdingsRetrieval),
    navSourceUrl: textOrNull(report.navSourceUrl) || (numberOrNull(report.navYuan, { positive: true }) !== null
      ? textOrNull(report.sourceUrl) : null),
    managers: normalizeManagers(report.managers),
    noEquitiesConfirmed: report.noEquitiesConfirmed === true,
    noEquitiesEvidence: report.noEquitiesConfirmed === true && report.noEquitiesEvidence ? {
      confirmed: report.noEquitiesEvidence.confirmed === true,
      sourceUrl: textOrNull(report.noEquitiesEvidence.sourceUrl),
      periodEnd: report.noEquitiesEvidence.periodEnd,
      publishedAt: report.noEquitiesEvidence.publishedAt,
      page: numberOrNull(report.noEquitiesEvidence.page, { positive: true }),
      text: textOrNull(report.noEquitiesEvidence.text)?.slice(0, 600) || null,
      kind: textOrNull(report.noEquitiesEvidence.kind),
      title: textOrNull(report.noEquitiesEvidence.title),
    } : null,
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
    },
  };
}

function selectReports(reports, { asOf, periodEnd, baselinePeriodEnd } = {}) {
  if (!validDate(asOf)) throw new TypeError('INVALID_AS_OF');
  if (periodEnd !== undefined && !validDate(periodEnd)) throw new TypeError('INVALID_PERIOD_END');
  if (baselinePeriodEnd !== undefined && !validDate(baselinePeriodEnd)) throw new TypeError('INVALID_BASELINE_PERIOD_END');
  const priority = { full: 2, top10: 1, unknown: 0 };
  const available = (Array.isArray(reports) ? reports : [])
    .map(normalizeReport).filter(Boolean)
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
  if (!observations.length) return result;
  return {
    ...result, status: 'observed', label: '已观测区间价格（非持仓成本；完整性未核验）',
    low: Math.min(...observations.map(row => row.low)),
    high: Math.max(...observations.map(row => row.high)),
    observedStart: observations[0].date,
    observedEnd: observations[observations.length - 1].date,
    observedCount: observations.length,
  };
}

function analyzeFund(fund, options = {}) {
  if (!fund || !textOrNull(fund.code)) throw new TypeError('INVALID_FUND_CODE');
  const { selectedReport, previousReport } = selectReports(fund.reports, options);
  const interval = makeInterval(selectedReport, previousReport);
  const currentMap = new Map((selectedReport ? selectedReport.holdings : []).map(h => [holdingKey(h), h]));
  const previousMap = new Map((previousReport ? previousReport.holdings : []).map(h => [holdingKey(h), h]));
  const keys = new Set([...currentMap.keys(), ...previousMap.keys()]);
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
  const reportStart = selectedReport ? (selectedReport.kind === 'quarterly'
    ? quarterStart(selectedReport.periodEnd) : `${selectedReport.periodEnd.slice(0, 4)}-01-01`) : null;
  const explicitManagers = selectedReport ? selectedReport.managers : [];
  const reportManagers = explicitManagers.length ? explicitManagers : managers.filter(manager => selectedReport
    && manager.startDate && manager.startDate <= selectedReport.periodEnd
    && (!manager.endDate || manager.endDate >= reportStart));
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
    managersAsOf: validDate(fund.managersAsOf) ? fund.managersAsOf : null,
    managerScope: textOrNull(fund.managerScope) || 'current-profile',
    managerSourceUrl: explicitManagers.length ? selectedReport.sourceUrl
      : textOrNull(fund.managerSourceUrl) || textOrNull(fund.metadataSourceUrl),
    metadataSourceUrl: textOrNull(fund.metadataSourceUrl),
    shareClassCodes: [...new Set((Array.isArray(fund.shareClassCodes) ? fund.shareClassCodes : []).filter(textOrNull))],
    asOf: options.asOf, status: selectedReport ? 'ok' : 'unavailable',
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
  normalizeReport, numberOrNull, priceRange, selectReports, validDate,
};
