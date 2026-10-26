(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RecordLabStrategySections = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
'use strict';

// Complete source extraction and selected evidence are separate claims.
const CATEGORIES = ['market-review', 'portfolio-operations', 'outlook', 'unclassified'];
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value + 'T00:00:00Z')) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
const count = value => Number.isInteger(value) && value >= 0 && value <= 240000;
const page = value => Number.isInteger(value) && value > 0 && value <= 10000;
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const plain = (value, limit) => typeof value === 'string' && value.trim() && value.length <= limit ? value.trim() : null;
function source(value) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const url = new URL(value), host = url.hostname.toLowerCase();
    return ['https:', 'http:'].includes(url.protocol) && host.includes('.') && !url.username && !url.password && !url.hash
      && !host.endsWith('.local') && !host.endsWith('.localhost') && !host.startsWith('[')
      && !/^(?:0|10|127|169\.254|192\.168)\./.test(host) && !/^172\.(?:1[6-9]|2\d|3[01])\./.test(host)
      && ![...url.searchParams.keys()].some(key => /^(?:token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(key));
  } catch { return false; }
}
function interval(raw) {
  if (!date(raw?.periodEnd) || !['quarterly', 'semiannual', 'interim', 'annual'].includes(raw.kind)) return null;
  const suffix = raw.periodEnd.slice(5), month = Number(raw.periodEnd.slice(5, 7));
  if (!['03-31', '06-30', '09-30', '12-31'].includes(suffix)
    || ['semiannual', 'interim'].includes(raw.kind) && suffix !== '06-30' || raw.kind === 'annual' && suffix !== '12-31') return null;
  return `${raw.periodEnd.slice(0, 4)}-${raw.kind === 'quarterly' ? String(month - 2).padStart(2, '0') : '01'}-01`;
}
function heading(value) {
  if (!plain(value, 220)) return null;
  const match = value.match(/^\s*(?:#{1,6}\s*)?([1-9]\s*\.\s*\d+(?:\s*\.\s*\d+){0,2}|§\s*[1-9])\s*([^\n]+)$/);
  return match ? { section: match[1].replace(/[§\s]/g, ''), title: match[2].replace(/\s/g, '') } : null;
}
function boundary(start, end) {
  const a = start.split('.').map(Number), b = end.section.split('.').map(Number);
  const at = b.findIndex((number, index) => number !== a[index]);
  const forward = at >= 0 && (a[at] === undefined || b[at] > a[at]);
  return forward && (/业绩表现|投资业绩|基金净值表现|净值增长率/.test(end.title)
    || b.length <= a.length || a.some((number, index) => b[index] !== number));
}
function documentPeriod(evidence, raw) {
  if (!Array.isArray(evidence) || evidence.length !== 1) return false;
  const item = evidence[0], kind = raw.kind === 'interim' ? 'semiannual' : raw.kind;
  if (!item || item.periodEnd !== raw.periodEnd || item.kind !== kind || !page(item.page) || !plain(item.text, 100)) return false;
  const match = item.text.replace(/\s/g, '').match(/^(20\d{2})年(?:第?([1-4一二三四])季度|(中期|半年度)|(年度))报告$/);
  if (!match) return false;
  const quarter = match[2] ? Number(match[2]) || '一二三四'.indexOf(match[2]) + 1 : null;
  const end = match[1] + '-' + (quarter ? ['03-31', '06-30', '09-30', '12-31'][quarter - 1] : match[3] ? '06-30' : '12-31');
  return end === item.periodEnd && item.kind === (quarter ? 'quarterly' : match[3] ? 'semiannual' : 'annual');
}
function strategySectionCoverageFor(raw, periodEnd = null) {
  if (!raw || raw.schemaVersion !== 1 || raw.method !== 'bounded-strategy-sections-v1' || !source(raw.sourceUrl)
    || !hash(raw.sourceTextHash) || periodEnd && raw.periodEnd !== periodEnd || !interval(raw) || raw.periodStart !== interval(raw)
    || ['sectionCount', 'contentChars', 'sentenceCount'].some(key => !count(raw[key]))
    || !Array.isArray(raw.sections) || raw.sections.length > 4) return null;
  const sections = [], checks = [];
  for (const item of raw.sections) {
    if (!item || !['4.4', '4.4.1'].includes(item.section) || !hash(item.contentHash)
      || !count(item.contentChars) || !count(item.sentenceCount)) return null;
    const start = heading(item.heading), end = heading(item.endHeading);
    const pages = Array.isArray(item.pages) && item.pages.length > 0 && item.pages.length <= 16 && item.pages.every(page)
      && item.pages.every((value, index) => value === item.pages[0] + index)
      && item.startPage === item.pages[0] && item.endPage === item.pages.at(-1);
    const bounded = start?.section === item.section && /^(?:报告期内)?(?:本)?基金(?:的)?投资策略和运作分析$/.test(start.title)
      && end?.section === item.endSection && boundary(start.section, end);
    checks.push(Boolean(pages && bounded && item.sectionBounded === true && item.pageNumbersVerified === true
      && item.pageLimitReached === false && item.textLimitReached === false && item.contentChars > 0 && item.sentenceCount > 0));
    sections.push(Object.fromEntries(['section', 'heading', 'startPage', 'endPage', 'pages', 'endSection', 'endHeading', 'sectionBounded',
      'pageNumbersVerified', 'pageLimitReached', 'textLimitReached', 'contentChars', 'sentenceCount', 'contentHash'].map(key => [key, item[key] ?? null])));
  }
  const complete = Boolean(raw.complete === true && raw.sectionCount === sections.length && sections.length === 1 && checks.every(Boolean)
    && documentPeriod(raw.documentPeriodEvidence, raw) && date(raw.publishedAt) && raw.publishedAt >= raw.periodEnd
    && ['contentChars', 'sentenceCount'].every(key => raw[key] === sections.reduce((sum, item) => sum + item[key], 0)));
  return { schemaVersion: 1, method: raw.method, sourceUrl: raw.sourceUrl, publishedAt: date(raw.publishedAt) ? raw.publishedAt : null,
    periodStart: raw.periodStart, periodEnd: raw.periodEnd, kind: raw.kind, sourceTextHash: raw.sourceTextHash,
    documentPeriodEvidence: (Array.isArray(raw.documentPeriodEvidence) ? raw.documentPeriodEvidence : []).slice(0, 8).filter(item => item && typeof item === 'object')
      .map(item => ({ periodEnd: item.periodEnd, kind: item.kind, page: page(item.page) ? item.page : null, text: plain(item.text, 100) })),
    sections, sectionCount: sections.length, contentChars: raw.contentChars, sentenceCount: raw.sentenceCount, complete,
    status: complete ? 'complete' : sections.length ? 'partial' : 'missing',
    reasonCodes: [...new Set((Array.isArray(raw.reasonCodes) ? raw.reasonCodes : []).filter(value => typeof value === 'string' && /^[A-Z_]{1,60}$/.test(value)))].sort() };
}
function mentions(raw, text) {
  return (Array.isArray(raw) ? raw : []).slice(0, 20).filter(item => {
    if (!item || !plain(item.stockName, 100) || typeof item.stockCode !== 'string') return false;
    const code = item.stockCode, name = item.stockName.replace(/\s/g, '');
    if (!(item.market === 'CN' && /^\d{6}$/.test(code) || item.market === 'HK' && /^\d{5}$/.test(code)
      || item.market === 'US' && /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/.test(code))) return false;
    const escaped = code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return item.matchType === 'exact-name' && name.length >= 4 && !['银行行业', '保险行业', '科技行业', '金融行业', '人工智能', '中国经济'].includes(name) && text.includes(name)
      || item.matchType === 'explicit-code' && (item.market === 'US' ? new RegExp(`(?:股票代码|证券代码|ticker)\\s*[:：]?\\s*${escaped}(?![A-Z0-9.])`, 'i').test(text)
        : new RegExp(`(?<!\\d)${escaped}(?!\\d)`).test(text));
  }).map(item => ({ stockCode: item.stockCode, market: item.market, stockName: item.stockName, matchType: item.matchType,
    provesTrade: false, provesTradeReason: false }));
}
function strategyAnalysisFor(raw, rawCoverage) {
  const proof = strategySectionCoverageFor(rawCoverage);
  if (!proof || !raw || raw.schemaVersion !== 1 || raw.scope !== 'selected-evidence' || raw.method !== 'explicit-language-rules-v1'
    || raw.sentenceCount !== proof.sentenceCount || !count(raw.sentenceCount) || !raw.categorySentenceCounts
    || CATEGORIES.some(category => !count(raw.categorySentenceCounts[category]) || raw.categorySentenceCounts[category] > raw.sentenceCount)
    || !Array.isArray(raw.evidence) || raw.evidence.length > 8 || raw.selectedSentenceCount !== raw.evidence.length
    || raw.omittedSentenceCount !== raw.sentenceCount - raw.evidence.length) return null;
  const evidence = [], ids = new Set(), indices = new Set();
  const conflict = proof.documentPeriodEvidence.some(item => item.periodEnd !== proof.periodEnd || item.kind !== (proof.kind === 'interim' ? 'semiannual' : proof.kind));
  if (conflict && raw.evidence.length) return null;
  for (const item of raw.evidence) {
    const text = plain(item?.text, 160), categories = item?.categories;
    if (!text || !plain(item.id, 40) || ids.has(item.id) || !count(item.sentenceIndex) || item.sentenceIndex >= raw.sentenceCount || indices.has(item.sentenceIndex)
      || item.sourceUrl !== proof.sourceUrl || item.periodStart !== proof.periodStart || item.periodEnd !== proof.periodEnd
      || !Array.isArray(item.pages) || !item.pages.length || !item.pages.every(page) || item.page !== item.pages[0]
      || !item.pages.every((value, index) => value === item.pages[0] + index)
      || !proof.sections.some(section => section.section === item.section && item.pages.every(value => section.pages?.includes(value)))
      || !Array.isArray(categories) || !categories.length || categories.some(value => !CATEGORIES.includes(value))) return null;
    ids.add(item.id); indices.add(item.sentenceIndex);
    evidence.push({ id: item.id, text, page: item.page, pages: [...item.pages], section: item.section, sentenceIndex: item.sentenceIndex,
      categories: [...new Set(categories)], sourceUrl: proof.sourceUrl, periodStart: proof.periodStart, periodEnd: proof.periodEnd,
      quoteTruncated: item.quoteTruncated === true, isInference: false, securityMentions: mentions(item.securityMentions, text),
      languageSignals: { operation: /本基金|本产品|本组合|组合|我们/.test(text) && /配置|持有|增持|减持|增配|减配|买入|卖出|加仓|减仓|降低|增加|调整|布局|维持|仓位/.test(text),
        causalConnective: /因为|由于|基于|考虑到|因此|从而|以期|旨在|以博取|为了/.test(text),
        outlook: /展望|未来|下一阶段|后续|后市|将继续|继续将|预计|我们将|本基金将|计划|有望/.test(text) } });
  }
  return { schemaVersion: 1, scope: 'selected-evidence', method: raw.method, status: conflict ? 'wrong-period' : evidence.length ? proof.complete ? 'available' : 'partial-source' : 'missing',
    analysisCoverage: 'selected-evidence', semanticAnalysisComplete: false, sourceSectionComplete: proof.complete, sentenceCount: raw.sentenceCount,
    categorySentenceCounts: Object.fromEntries(CATEGORIES.map(category => [category, raw.categorySentenceCounts[category]])), categoryCountsOverlap: true,
    selectedSentenceCount: evidence.length, omittedSentenceCount: raw.sentenceCount - evidence.length, maxQuotes: 8, maxQuoteChars: 160, evidence,
    explainsIndividualStockTrades: false, securityMentionsProveTrades: false };
}
return { strategySectionCoverageFor, strategyAnalysisFor };
});
