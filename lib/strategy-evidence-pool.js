'use strict';

// Quote-pool admission proves bounded literal projections, not semantic truth.
const crypto = require('node:crypto');
const { strategySectionCoverageFor } = require('./strategy-sections');
const METHOD = 'shared-strategy-evidence-pool-v1';
const ACCOUNTING = 'unique-source-quote-pool-not-serialized-bytes';
const SOURCE_FIELDS = ['sourceUrl', 'publishedAt', 'periodStart', 'periodEnd', 'kind', 'sourceTextHash', 'retrievedAt'];
const LIMITS = { maxQuotes: 8, maxQuoteChars: 160, maxTotalQuoteChars: 1280 };
const POOL_FIELDS = ['schemaVersion', 'method', 'status', 'source', 'quotes', 'digest', 'usedQuotes', 'usedQuoteChars',
  'accounting', 'semanticAnalysisComplete', ...Object.keys(LIMITS)];
const QUOTE_FIELDS = ['id', 'text', 'textHash', 'page', 'pages', 'section'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const sameKeys = (value, keys) => object(value) && Object.keys(value).sort().join('|') === [...keys].sort().join('|');
const clone = value => JSON.parse(JSON.stringify(value));
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
  : object(value) ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}' : JSON.stringify(value);
const equal = (a, b) => canonical(a) === canonical(b);
const chars = value => typeof value === 'string' ? Array.from(value).length : -1;
function instant(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match || Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4]) > 59
    || !Number.isFinite(Date.parse(match[1])) || new Date(match[1]).toISOString().slice(0, 10) !== match[1]
    || match[6] !== 'Z' && (Number(match[6].slice(1, 3)) > 23 || Number(match[6].slice(4)) > 59)) return null;
  const ms = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}${match[6]}`);
  return Number.isFinite(ms) ? BigInt(ms) * 1000n + BigInt((match[5] || '').padEnd(6, '0')) : null;
}
function current(now) {
  if (now === undefined) return BigInt(Date.now()) * 1000n;
  if (now instanceof Date) now = now.getTime();
  return typeof now === 'number' && Number.isSafeInteger(now) ? BigInt(now) * 1000n : instant(now);
}
const quoteId = (sourceHash, textHash, pages) => 'q' + hash(sourceHash + '|' + textHash + '|' + pages.join(','));
const poolDigest = (source, quotes) => hash(canonical({ schemaVersion: 1,
  source: Object.fromEntries(Object.entries(source).filter(([key]) => key !== 'retrievedAt')), quotes }));
function normalizeStrategyEvidencePool(raw, coverage, expectedSource = {}, now) {
  try {
    const proof = strategySectionCoverageFor(coverage);
    if (!proof?.complete || proof.kind !== 'quarterly' || !sameKeys(raw, POOL_FIELDS)
      || raw.schemaVersion !== 1 || raw.method !== METHOD || raw.status !== 'verified'
      || raw.accounting !== ACCOUNTING || raw.semanticAnalysisComplete !== false
      || Object.entries(LIMITS).some(([key, value]) => raw[key] !== value)) return null;
    const source = raw.source;
    if (!sameKeys(source, SOURCE_FIELDS) || SOURCE_FIELDS.some(key => key !== 'retrievedAt' && source[key] !== proof[key])
      || Object.entries(expectedSource || {}).some(([key, value]) => SOURCE_FIELDS.includes(key) && source[key] !== value)) return null;
    const observed = instant(source.retrievedAt), clock = current(now);
    if (observed === null || clock === null || observed > clock + 300000000n
      || source.publishedAt > new Date(Number(observed / 1000n) + 28800000).toISOString().slice(0, 10)) return null;
    if (!Array.isArray(raw.quotes) || raw.quotes.length > 8) return null;
    const seen = new Set(), section = proof.sections[0];
    for (const quote of raw.quotes) {
      if (!sameKeys(quote, QUOTE_FIELDS) || typeof quote.text !== 'string' || !quote.text.trim()
        || chars(quote.text) > 160 || /[\ud800-\udfff\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(quote.text)
        || !Array.isArray(quote.pages) || !quote.pages.length || quote.pages.length > 16
        || quote.pages.some((page, i) => !Number.isInteger(page) || !section.pages.includes(page) || page !== quote.pages[0] + i)
        || quote.page !== quote.pages[0] || quote.section !== section.section || quote.textHash !== hash(quote.text)
        || quote.id !== quoteId(source.sourceTextHash, quote.textHash, quote.pages) || seen.has(quote.id)) return null;
      seen.add(quote.id);
    }
    const count = raw.quotes.reduce((sum, quote) => sum + chars(quote.text), 0);
    if (raw.usedQuotes !== raw.quotes.length || raw.usedQuoteChars !== count || count > 1280
      || raw.digest !== poolDigest(source, raw.quotes)) return null;
    return clone(raw);
  } catch { return null; }
}
function budget(status, reasonCodes, pool = null, projection = false) {
  return { schemaVersion: 1, status, sourceBound: Boolean(pool), poolVerified: Boolean(pool), projectionVerified: projection,
    usedQuotes: pool?.usedQuotes ?? null, usedQuoteChars: pool?.usedQuoteChars ?? null, ...LIMITS, accounting: ACCOUNTING,
    semanticAnalysisComplete: false, reasonCodes };
}
function strategyEvidenceBudgetFor(report, now) {
  const priorPartial = object(report?.strategyEvidenceBudget) && report.strategyEvidenceBudget.status === 'partial';
  if (!object(report) || report.strategyEvidencePool == null) return priorPartial
    ? budget('partial', ['PRIOR_QUOTE_BUDGET_REJECTED']) : budget('legacy-unbudgeted', ['EVIDENCE_POOL_MIGRATION_REQUIRED']);
  const original = report.strategyEvidenceRetrievedAt;
  let observed = original;
  if (Object.hasOwn(report, 'strategyClaimsDerivedFromObservedAt')) {
    observed = report.strategyEvidencePool?.source?.retrievedAt;
    const oldTime = instant(original), newTime = instant(observed);
    if (report.strategyClaimsDerivedFromObservedAt !== original || oldTime === null || newTime === null || oldTime > newTime)
      return budget('partial', ['STRATEGY_SOURCE_OBSERVATION_MISMATCH']);
  }
  const pool = normalizeStrategyEvidencePool(report.strategyEvidencePool, report.strategySectionCoverage,
    { retrievedAt: observed }, now);
  if (!pool) return budget('partial', ['INVALID_EVIDENCE_POOL']);
  if (priorPartial) return budget('partial', ['PRIOR_QUOTE_BUDGET_REJECTED'], pool);
  const byId = new Map(pool.quotes.map(quote => [quote.id, quote])), source = pool.source, values = [];
  if (report.narrative != null) {
    if (!object(report.narrative)) return budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool);
    if (report.narrative.text != null) values.push(report.narrative);
  }
  for (const [container, key] of [[report, 'strategyExcerpts'], [report.strategyAnalysis, 'evidence'], [report.strategyClaims, 'evidence']]) {
    if (container == null) continue;
    if (!object(container) || Object.hasOwn(container, key) && !Array.isArray(container[key])
      || (container[key] || []).length > 8) return budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool);
    values.push(...(container[key] || []));
  }
  if (report.strategyClaims && (!object(report.strategyClaims.source)
    || Object.entries(source).some(([key, value]) => report.strategyClaims.source[key] !== value)))
    return budget('partial', ['STRATEGY_CLAIMS_SOURCE_MISMATCH'], pool);
  for (const item of values) {
    const quote = object(item) && byId.get(item.evidencePoolId);
    if (!quote || item.text !== quote.text || item.sourceUrl !== source.sourceUrl || item.page !== quote.page
      || ['periodStart', 'periodEnd'].some(key => item[key] !== source[key])
      || Object.hasOwn(item, 'pages') && !equal(item.pages, quote.pages)
      || Object.hasOwn(item, 'textHash') && item.textHash !== quote.textHash)
      return budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool);
  }
  return budget('verified', [], pool, true);
}

module.exports = { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor };
