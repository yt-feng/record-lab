'use strict';

// A locator is only an announcement candidate. This receipt proves an attempt
// against its frozen identifiers; it never proves that a PDF or report passed.
const crypto = require('node:crypto');
const ENDPOINT = 'https://www.cninfo.com.cn/new/hisAnnouncement/query';
const PERIODS = { '2026Q2': '2026-06-30', '2026Q3': '2026-09-30' };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const code = value => typeof value === 'string' && /^\d{6}$/.test(value) && value !== '000000';
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const text = (value, limit) => typeof value === 'string' && value.length > 0 && [...value].length <= limit
  && !/[<>\u0000-\u001f\u007f]/.test(value)
  && !/[\ud800-\udfff]/u.test(value);

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (object(value)) return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
const digest = value => crypto.createHash('sha256').update(canonical(value)).digest('hex');
function candidateId(value) {
  return digest({ announcementId: value.announcementId, codes: [...value.codes].sort(), title: value.title,
    publishedAt: value.publishedAt, sourceUrl: value.sourceUrl, targetPeriod: value.targetPeriod,
    correctionLabel: value.correctionLabel });
}
const locatorDigest = (fundCode, periodEnd, ids) => digest({ code: fundCode, targetPeriodEnd: periodEnd, candidateIds: [...ids].sort() });

function instant(value) {
  if (typeof value !== 'string' || value.length > 40) return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match || !date(match[1]) || Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4]) > 59) return null;
  if (match[6] !== 'Z' && (Number(match[6].slice(1, 3)) > 23 || Number(match[6].slice(4)) > 59)) return null;
  const ms = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}${match[6]}`);
  return Number.isFinite(ms) ? BigInt(ms) * 1000n + BigInt((match[5] || '').padEnd(6, '0')) : null;
}
const currentInstant = value => value instanceof Date && Number.isFinite(value.getTime()) ? BigInt(value.getTime()) * 1000n : instant(value);
const chinaDate = stamp => new Date(Number(stamp / 1000n) + 8 * 3600000).toISOString().slice(0, 10);

function normalizeCandidate(value, fundCode, periodEnd, now) {
  const current = currentInstant(now), observed = instant(value?.retrievedAt);
  if (!object(value) || !code(fundCode) || !date(periodEnd) || current === null || observed === null || observed > current + 300000000n
    || typeof value.announcementId !== 'string' || !/^\d{1,20}$/.test(value.announcementId)
    || !Array.isArray(value.codes) || !value.codes.length || value.codes.length > 100 || value.codes.some(item => !code(item))
    || new Set(value.codes).size !== value.codes.length || !value.codes.includes(fundCode)
    || PERIODS[value.targetPeriod] !== periodEnd || !text(value.title, 300) || value.pdfVerified !== false
    || typeof value.correctionLabel !== 'boolean' || value.correctionLabel !== /更正|修订/.test(value.title)
    || value.label != null && !text(value.label, 200) || value.discoverySourceUrl !== ENDPOINT
    || !date(value.publishedAt) || value.publishedAt < periodEnd || value.publishedAt > chinaDate(observed)) return null;
  const compactTitle = value.title.replace(/\s/g, ''), quarter = value.targetPeriod === '2026Q2' ? '[2二]' : '[3三]';
  if (!new RegExp(`2026年(?:度)?第?${quarter}季度报告`).test(compactTitle) || !/基金|ETF|LOF|FOF/i.test(compactTitle)
    || /提示|公告|招募|合同|摘要/.test(compactTitle)) return null;
  const source = typeof value.sourceUrl === 'string'
    && /^https:\/\/static\.cninfo\.com\.cn\/finalpage\/(2026-\d{2}-\d{2})\/(\d{1,20})\.(?:PDF|pdf)$/.exec(value.sourceUrl);
  if (!source || source[1] !== value.publishedAt || source[2] !== value.announcementId) return null;
  const page = value.sourcePage;
  if (!object(page) || !date(page.start) || !date(page.end) || !page.start.startsWith('2026-') || !page.end.startsWith('2026-')
    || page.start > value.publishedAt || page.end < value.publishedAt || page.end > chinaDate(observed)
    || !Number.isInteger(page.page) || page.page < 1 || page.page > 32 || !hash(page.contentHash) || typeof page.splitProbe !== 'boolean') return null;
  const normalized = { announcementId: value.announcementId, codes: [...value.codes].sort(), title: value.title, label: value.label ?? null,
    publishedAt: value.publishedAt, sourceUrl: value.sourceUrl.replace(/\.[Pp][Dd][Ff]$/, '.PDF'), targetPeriod: value.targetPeriod,
    correctionLabel: value.correctionLabel, pdfVerified: false, retrievedAt: value.retrievedAt,
    sourcePage: { start: page.start, end: page.end, page: page.page, contentHash: page.contentHash, splitProbe: page.splitProbe },
    discoverySourceUrl: ENDPOINT };
  const identity = candidateId(normalized);
  if (value.candidateId !== identity) return null;
  return { ...normalized, candidateId: identity };
}

function normalizeLocator(value, fundCode, periodEnd, now) {
  if (!object(value) || value.schemaVersion !== 1 || value.code !== fundCode || !code(fundCode)
    || value.targetPeriodEnd !== periodEnd || !Array.isArray(value.candidates) || !value.candidates.length || value.candidates.length > 2) return null;
  const candidates = value.candidates.map(item => normalizeCandidate(item, fundCode, periodEnd, now));
  if (candidates.some(item => !item) || new Set(candidates.map(item => item.candidateId)).size !== candidates.length) return null;
  candidates.sort((a, b) => {
    for (const key of ['publishedAt', 'correctionLabel', 'announcementId', 'sourceUrl']) {
      if (a[key] !== b[key]) return a[key] < b[key] ? 1 : -1;
    }
    return 0;
  });
  const id = locatorDigest(fundCode, periodEnd, candidates.map(item => item.candidateId));
  return value.digest === id ? { schemaVersion: 1, code: fundCode, targetPeriodEnd: periodEnd, digest: id, candidates } : null;
}

function normalizeLocatorReceipt(value, item, config, retrievedAt, now) {
  const observed = instant(retrievedAt), planned = instant(config?.traversal?.plannedAt), current = currentInstant(now);
  const attempted = instant(value?.attemptedAt), locator = normalizeLocator(item?.reportLocator, item?.code, config?.targetPeriodEnd, now);
  if (!object(value) || !locator || value.schemaVersion !== 1 || value.code !== item.code
    || value.targetPeriodEnd !== config.targetPeriodEnd || value.digest !== locator.digest
    || observed === null || planned === null || current === null || attempted === null
    || planned > attempted || attempted > observed || observed > current + 300000000n
    || !Array.isArray(value.attemptedCandidateIds) || !value.attemptedCandidateIds.length || value.attemptedCandidateIds.length > 2
    || new Set(value.attemptedCandidateIds).size !== value.attemptedCandidateIds.length
    || value.attemptedCandidateIds.some(id => !hash(id) || !locator.candidates.some(candidate => candidate.candidateId === id
      && instant(candidate.retrievedAt) <= attempted))) return null;
  return { schemaVersion: 1, code: item.code, targetPeriodEnd: config.targetPeriodEnd, digest: locator.digest,
    attemptedAt: value.attemptedAt, attemptedCandidateIds: [...value.attemptedCandidateIds].sort() };
}

function normalizeSourceLocatorEvidence(value, report, now = new Date()) {
  if (!object(report) || report.kind !== 'quarterly' || !code(report.fundCode)) return null;
  const candidate = normalizeCandidate(value, report.fundCode, report.periodEnd, now);
  if (!candidate || candidate.sourceUrl !== report.sourceUrl || candidate.publishedAt !== report.publishedAt) return null;
  const identity = require('./portfolio-identity').normalizePortfolioIdentity(report.portfolioIdentityEvidence, report, now);
  return identity ? candidate : null;
}

module.exports = { canonical, candidateId, locatorDigest, normalizeCandidate, normalizeLocator,
  normalizeLocatorReceipt, normalizeSourceLocatorEvidence };
