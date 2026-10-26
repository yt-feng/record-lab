'use strict';

// This lane binds one new PDF observation to an already verified section.
// It carries no holdings, manager or price updates and proves no full semantics.
const crypto = require('node:crypto');
const { strategySectionCoverageFor } = require('./strategy-sections');
const { strategyClaimsFor, METHOD, canonical } = require('./strategy-claims');
const { normalizePortfolioIdentity } = require('./portfolio-identity');
const { chinaDate } = require('../web/source-dates');
const SOURCE_FIELDS = ['sourceUrl', 'publishedAt', 'kind', 'periodStart', 'periodEnd', 'sourceTextHash', 'section', 'sectionContentHash', 'pages'];
const TASK_FIELDS = ['schemaVersion', 'code', 'targetPeriodEnd', 'method', 'source', 'analysisDigest', 'taskDigest', 'retainedStrategyObservedAt'];
const RECEIPT_FIELDS = ['schemaVersion', 'code', 'targetPeriodEnd', 'taskDigest', 'analysisDigest', 'sourceUrl', 'attemptedAt', 'outcome', 'errorCode'];
const REPORT_FIELDS = ['fundCode', 'periodEnd', 'publishedAt', 'kind', 'sourceUrl', 'strategySectionCoverage', 'strategyClaims',
  'strategyEvidencePool', 'strategyEvidenceBudget', 'strategyEvidenceRetrievedAt', 'strategyClaimsIdentityEvidence',
  'strategyAnalysis', 'strategyExcerpts', 'strategyThemes', 'narrative'];
const ERROR_CODES = new Set(['STRATEGY_CLAIMS_SOURCE_CHANGED', 'STRATEGY_CLAIMS_IDENTITY_MISMATCH', 'STRATEGY_CLAIMS_INCOMPLETE',
  'STRATEGY_CLAIMS_FAILED', 'STRATEGY_CLAIMS_INTERRUPTED', 'INVALID_URL', 'UNEXPECTED_REQUEST', 'HTTP_REJECTED', 'HTTP_ERROR', 'TLS_FAILED', 'TIMEOUT',
  'NETWORK_ERROR', 'EMPTY_RESPONSE', 'RESPONSE_TOO_LARGE', 'INVALID_PDF', 'PDF_PARSE_FAILED', 'PDF_TEXT_EMPTY']);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const clone = value => JSON.parse(JSON.stringify(value));
const same = (a, b) => canonical(a) === canonical(b);
const exactKeys = (value, names) => object(value) && same(Object.keys(value).sort(), [...names].sort());
const code = value => typeof value === 'string' && /^\d{6}$/.test(value) && value !== '000000';
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const digest = value => crypto.createHash('sha256').update(canonical(value)).digest('hex');
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
function instant(value) {
  const match = typeof value === 'string' && /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match || !date(match[1]) || +match[2] > 23 || +match[3] > 59 || +match[4] > 59
    || match[6] !== 'Z' && (+match[6].slice(1, 3) > 23 || +match[6].slice(4) > 59)) return null;
  const ms = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}${match[6]}`);
  return Number.isFinite(ms) ? BigInt(ms) * 1000n + BigInt((match[5] || '').padEnd(6, '0')) : null;
}
function current(value = new Date()) {
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? BigInt(value.getTime()) * 1000n : null;
  return typeof value === 'number' && Number.isSafeInteger(value) ? BigInt(value) * 1000n : instant(value);
}
function publicSource(value) {
  if (typeof value !== 'string' || Array.from(value).length > 2048 || /[\ud800-\udfff\u0000-\u0020\u007f\\#]/u.test(value)
    || !/^https:\/\/[A-Za-z0-9.-]+(?::443)?(?:\/|$)/.test(value)) return false;
  try {
    const url = new URL(value), host = url.hostname;
    return url.protocol === 'https:' && !url.username && !url.password && !url.hash && !url.port
      && /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z][a-z0-9-]*$/.test(host)
      && !/\.(?:local|localhost|internal)$/.test(host)
      && ![...url.searchParams.keys()].some(key => /token|secret|password|signature|authorization|credential|key/i.test(key));
  } catch { return false; }
}
function normalizeTask(raw, phase = {}, fundCode = raw?.code, now = new Date()) {
  const clock = current(now), at = instant(raw?.retainedStrategyObservedAt);
  if (!exactKeys(raw, TASK_FIELDS) || raw.schemaVersion !== 1 || !code(fundCode) || raw.code !== fundCode
    || raw.method !== METHOD || !date(raw.targetPeriodEnd) || phase.targetPeriodEnd && raw.targetPeriodEnd !== phase.targetPeriodEnd
    || !/-(?:03-31|06-30|09-30|12-31)$/.test(raw.targetPeriodEnd) || clock === null || at === null || at > clock + 300000000n
    || !exactKeys(raw.source, SOURCE_FIELDS)) return null;
  const source = raw.source, start = `${raw.targetPeriodEnd.slice(0, 4)}-${String(Number(raw.targetPeriodEnd.slice(5, 7)) - 2).padStart(2, '0')}-01`;
  if (!publicSource(source.sourceUrl) || !date(source.publishedAt) || source.publishedAt < raw.targetPeriodEnd
    || source.publishedAt > chinaDate(raw.retainedStrategyObservedAt) || source.kind !== 'quarterly'
    || source.periodEnd !== raw.targetPeriodEnd || source.periodStart !== start || !hash(source.sourceTextHash)
    || !hash(source.sectionContentHash) || !['4.4', '4.4.1'].includes(source.section)
    || !Array.isArray(source.pages) || !source.pages.length || source.pages.length > 16
    || !source.pages.every((page, index) => Number.isInteger(page) && page >= 1 && page <= 10000 && page === source.pages[0] + index)
    || raw.analysisDigest !== digest({ ...source, method: METHOD })
    || raw.taskDigest !== digest({ code: fundCode, targetPeriodEnd: raw.targetPeriodEnd, analysisDigest: raw.analysisDigest })) return null;
  return clone(raw);
}
function taskFor(fund, phase = {}, now = new Date(), { code: requestedCode = fund?.code, eligibleOnly = true } = {}) {
  const report = fund?.selectedReport;
  if (!code(requestedCode) || !report || phase.targetPeriodEnd && report.periodEnd !== phase.targetPeriodEnd) return null;
  const proof = strategySectionCoverageFor(report.strategySectionCoverage, report.periodEnd);
  if (!proof?.complete || proof.kind !== 'quarterly' || proof.sections.length !== 1
    || eligibleOnly && strategyClaimsFor(report, { code: requestedCode, now, asOf: fund.asOf })) return null;
  if (requestedCode !== fund.code) {
    const identity = report.strategyClaimsIdentityEvidence || report.portfolioIdentityEvidence;
    if (!normalizePortfolioIdentity(identity, { ...proof, fundCode: requestedCode }, now)) return null;
  }
  const section = proof.sections[0], source = { ...Object.fromEntries(SOURCE_FIELDS.slice(0, 6).map(key => [key, proof[key]])),
    section: section.section, sectionContentHash: section.contentHash, pages: [...section.pages] };
  const analysisDigest = digest({ ...source, method: METHOD });
  return normalizeTask({ schemaVersion: 1, code: requestedCode, targetPeriodEnd: proof.periodEnd, method: METHOD, source,
    analysisDigest, taskDigest: digest({ code: requestedCode, targetPeriodEnd: proof.periodEnd, analysisDigest }),
    retainedStrategyObservedAt: report.strategyEvidenceRetrievedAt }, phase, requestedCode, now);
}
function normalizeReceipt(raw, rawTask, phase, retrievedAt, now = new Date(), plannedAt) {
  const task = normalizeTask(rawTask, phase, rawTask?.code, now), clock = current(now);
  const planned = instant(plannedAt), attempted = instant(raw?.attemptedAt), observed = instant(retrievedAt);
  if (!task || !exactKeys(raw, RECEIPT_FIELDS) || raw.schemaVersion !== 1
    || ['code', 'targetPeriodEnd', 'taskDigest', 'analysisDigest'].some(key => raw[key] !== task[key])
    || raw.sourceUrl !== task.source.sourceUrl || planned === null || attempted === null || observed === null || clock === null
    || planned > attempted || attempted > observed || observed > clock + 300000000n
    || !['started', 'parsed', 'failed', 'unknown'].includes(raw.outcome)) return null;
  if (['started', 'parsed'].includes(raw.outcome) ? raw.errorCode !== null
    : raw.outcome === 'unknown' ? raw.errorCode !== 'STRATEGY_CLAIMS_INTERRUPTED' : !ERROR_CODES.has(raw.errorCode)) return null;
  return clone(raw);
}
function normalizePatch(raw, rawTask, phase, retrievedAt, now = new Date(), plannedAt, rawReceipt = raw?.receipt) {
  const task = normalizeTask(rawTask, phase, rawTask?.code, now);
  const receipt = task && normalizeReceipt(rawReceipt, task, phase, retrievedAt, now, plannedAt);
  if (!task || !receipt || receipt.outcome !== 'parsed' || !exactKeys(raw, ['schemaVersion', 'code', 'binding', 'receipt', 'report'])
    || raw.schemaVersion !== 1 || raw.code !== task.code || !same(raw.binding, task) || !same(raw.receipt, receipt)
    || !object(raw.report) || Object.keys(raw.report).some(key => !REPORT_FIELDS.includes(key))
    || Buffer.byteLength(JSON.stringify(raw.report)) > 100000) return null;
  const value = raw.report, observed = instant(value.strategyEvidenceRetrievedAt);
  if (value.fundCode !== task.code || ['sourceUrl', 'periodEnd', 'publishedAt', 'kind'].some(key => value[key] !== task.source[key])
    || observed === null || observed < instant(receipt.attemptedAt) || observed < instant(task.retainedStrategyObservedAt)
    || observed > instant(retrievedAt)) return null;
  const claims = strategyClaimsFor(value, { code: task.code, now, asOf: chinaDate(retrievedAt) });
  if (!claims || claims.source.digest !== task.analysisDigest
    || !same(Object.fromEntries(SOURCE_FIELDS.map(key => [key, claims.source[key]])), task.source)) return null;
  // Normalize only whitelisted strategy metadata; there is no route for a
  // patch to replace a position, NAV, manager record, or observation envelope.
  const cleaned = require('./fund-analysis').normalizeReport(value, { code: task.code, now, asOf: chinaDate(retrievedAt) });
  if (!strategyClaimsFor(cleaned, { code: task.code, now, asOf: chinaDate(retrievedAt) })) return null;
  const report = Object.fromEntries(REPORT_FIELDS.filter(key => Object.hasOwn(cleaned, key)).map(key => [key, cleaned[key]]));
  return { schemaVersion: 1, code: task.code, binding: task, receipt, report };
}

module.exports = { normalizeTask, taskFor, normalizeReceipt, normalizePatch, SOURCE_FIELDS, REPORT_FIELDS, ERROR_CODES, instant, canonical };
