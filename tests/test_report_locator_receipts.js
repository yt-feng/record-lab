'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const api = require('../lib/report-locators');
const { buildSnapshot } = require('../scripts/build-data');
const { normalizeReport } = require('../lib/fund-analysis');
const clone = value => structuredClone(value);
const now = '2026-10-26T12:00:00Z', discovered = '2026-10-26T09:00:00Z';
const planned = '2026-10-26T10:00:00Z', attempted = '2026-10-26T10:30:00Z', observed = '2026-10-26T11:00:00Z';
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
function candidate(changes = {}) {
  const value = { announcementId: '12345678', codes: ['000001', '000002'], label: null,
    title: '合成基金2026年第2季度报告', publishedAt: '2026-07-21',
    sourceUrl: 'https://static.cninfo.com.cn/finalpage/2026-07-21/12345678.PDF', targetPeriod: '2026Q2',
    correctionLabel: false, pdfVerified: false, retrievedAt: discovered,
    discoverySourceUrl: 'https://www.cninfo.com.cn/new/hisAnnouncement/query',
    sourcePage: { start: '2026-07-01', end: '2026-07-31', page: 1, contentHash: 'a'.repeat(64), splitProbe: false }, ...changes };
  return { ...value, candidateId: api.candidateId(value) };
}
function configuration(candidates = [candidate()]) {
  const locator = { schemaVersion: 1, code: '000001', targetPeriodEnd: phase.targetPeriodEnd,
    digest: api.locatorDigest('000001', phase.targetPeriodEnd, candidates.map(value => value.candidateId)), candidates };
  return { ...phase, traversal: { plannedAt: planned }, funds: [{ code: '000001', reportLocator: locator }] };
}
function receipt(config = configuration(), changes = {}) {
  return { schemaVersion: 1, code: '000001', targetPeriodEnd: phase.targetPeriodEnd,
    digest: config.funds[0].reportLocator.digest, attemptedAt: attempted,
    attemptedCandidateIds: [config.funds[0].reportLocator.candidates[0].candidateId], ...changes };
}
function raw(config = configuration(), changes = {}) {
  return { asOf: '2026-10-26', retrievedAt: observed, barsByStock: {}, errors: [], funds: [{ code: '000001', reports: [],
    collectionAttempt: { attempted: true }, reportLocatorReceipt: receipt(config) }], ...changes };
}
const normalize = (value, config = configuration(), retrievedAt = observed) => api.normalizeLocatorReceipt(value, config.funds[0], config, retrievedAt, now);

test('Python and JS share candidate and locator identities independent of observation time or candidate order', () => {
  const first = candidate(), second = candidate({ announcementId: '12345679', sourceUrl: firstUrl('12345679') });
  const config = configuration([first, second]);
  const later = clone(first); later.retrievedAt = planned; later.sourcePage.contentHash = 'b'.repeat(64); later.sourcePage.page = 2;
  assert.equal(api.candidateId(later), first.candidateId);
  assert.equal(api.locatorDigest('000001', phase.targetPeriodEnd, [second.candidateId, first.candidateId]), config.funds[0].reportLocator.digest);
  const output = execFileSync('python3', ['-c', `import json,sys
sys.path.insert(0,'scripts')
from report_locators import normalize_locator, normalize_receipt
x=json.load(sys.stdin)
locator=normalize_locator(x['locator'],'000001','2026-06-30',x['now'])
print(json.dumps({'locator':locator,'receipt':normalize_receipt(x['receipt'],locator,x['now'])},ensure_ascii=False))`],
  { cwd: path.resolve(__dirname, '..'), encoding: 'utf8', input: JSON.stringify({ locator: config.funds[0].reportLocator, receipt: receipt(config), now }) });
  const python = JSON.parse(output);
  assert.deepEqual(api.normalizeLocator(config.funds[0].reportLocator, '000001', phase.targetPeriodEnd, now), python.locator);
  assert.deepEqual(normalize(receipt(config), config), python.receipt);
});
function firstUrl(id) { return `https://static.cninfo.com.cn/finalpage/2026-07-21/${id}.PDF`; }

test('candidate metadata requires exact public source, code, period, page and unverified PDF state', () => {
  const original = candidate();
  for (const change of [{ codes: ['000000', '000001'] }, { codes: ['000002'] }, { title: '合成基金2026年第3季度报告' },
    { title: '合成基金2026年第2季度报告摘要' }, { title: '合成基金2026年第2季度报告的提示公告' },
    { correctionLabel: true }, { pdfVerified: true }, { targetPeriod: '2026Q3' },
    { sourceUrl: 'https://static.cninfo.com.cn/finalpage/2026-07-21/12345678.PDF?page=1' },
    { sourceUrl: firstUrl('12345679') }, { sourceUrl: 'https://example.org/report.PDF' },
    { discoverySourceUrl: 'https://example.org/announcements' }, { publishedAt: '2026-07-20' },
    { retrievedAt: '2026-10-26T24:00:00Z' }, { retrievedAt: '2026-10-26T09:00:00+00:60' },
    { sourcePage: { ...original.sourcePage, page: 33 } }, { sourcePage: { ...original.sourcePage, splitProbe: 0 } },
    { sourcePage: { ...original.sourcePage, contentHash: 'bad' } }, { sourcePage: { ...original.sourcePage, end: '2026-07-20' } }]) {
    const value = candidate(change);
    assert.equal(api.normalizeCandidate(value, '000001', phase.targetPeriodEnd, now), null, JSON.stringify(change));
  }
  const unordered = candidate({ codes: ['000002', '000001'] });
  assert.deepEqual(api.normalizeCandidate(unordered, '000001', phase.targetPeriodEnd, now).codes, ['000001', '000002']);
  assert.equal(api.normalizeCandidate({ ...original, sourceUrl: original.sourceUrl.replace('.PDF', '.pdf') }, '000001', phase.targetPeriodEnd, now).sourceUrl, original.sourceUrl);
});

test('receipts bind only unique actually attempted IDs from the same frozen code and quarter', () => {
  const config = configuration(), original = receipt(config);
  for (const change of [{ schemaVersion: true }, { code: '000002' }, { targetPeriodEnd: '2026-09-30' },
    { digest: 'b'.repeat(64) }, { attemptedCandidateIds: [] }, { attemptedCandidateIds: ['b'.repeat(64)] },
    { attemptedCandidateIds: [original.attemptedCandidateIds[0], original.attemptedCandidateIds[0]] }]) {
    assert.equal(normalize({ ...original, ...change }, config), null);
  }
  const clean = normalize({ ...original, rawPdfText: 'must not publish', debug: 'must not publish' }, config);
  assert.deepEqual(clean, original);
  const changed = clone(config); changed.funds[0].reportLocator.candidates[0].title += '修订';
  assert.equal(normalize(original, changed), null);
  const repeated = configuration([candidate(), candidate()]);
  assert.equal(normalize(receipt(repeated), repeated), null);
});

test('attempt time is bounded by the real plan and receipt instant, including microseconds and offsets', () => {
  const config = configuration(), value = receipt(config);
  for (const at of [null, '2026-10-26', '2026-10-26T09:59:59.999999Z', '2026-10-26T11:00:00.000001Z', '2026-10-27T10:30:00Z']) {
    assert.equal(normalize({ ...value, attemptedAt: at }, config), null);
  }
  assert.equal(normalize(value, { ...config, traversal: {} }), null);
  assert.equal(normalize(value, config, '2026-10-26T12:05:00.000001Z'), null);
  assert.equal(normalize({ ...value, attemptedAt: '2026-10-26T18:30:00+08:00' }, config).attemptedAt, '2026-10-26T18:30:00+08:00');
  const fractional = { ...config, traversal: { plannedAt: '2026-10-26T10:30:00.123456Z' } };
  assert.equal(normalize({ ...value, attemptedAt: '2026-10-26T10:30:00.123455Z' }, fractional), null);
  assert.ok(normalize({ ...value, attemptedAt: '2026-10-26T10:30:00.123456Z' }, fractional));
  const futureCandidate = configuration([candidate({ retrievedAt: '2026-10-26T10:30:00.000001Z' })]);
  assert.equal(normalize(receipt(futureCandidate), futureCandidate), null);
});

test('failed PDF attempts produce only a validated receipt and failed status, never a fresh fund', () => {
  const config = configuration(), result = buildSnapshot(raw(config), { config, now });
  assert.equal(result.sourceMode, 'actions'); assert.deepEqual(result.funds, []);
  assert.equal(result.coverage.batchFreshFundCount, 0); assert.equal(result.coverage.failedFundCount, 1);
  assert.equal(result.batchResults[0].status, 'failed'); assert.equal(result.batchResults[0].attempted, true);
  assert.deepEqual(result.reportLocatorReceipts, [receipt(config)]);
  for (const wrong of [raw(config, { funds: [{ code: '000001', reports: [] }] }),
    raw(config, { funds: [{ code: '000001', reports: [], reportLocatorReceipt: receipt(config, { digest: 'b'.repeat(64) }) }] })]) {
    assert.throws(() => buildSnapshot(wrong, { config, now }), { code: 'NO_VALID_FRESH_FUNDS' });
  }
});

function report(candidateValue = candidate()) {
  const fixture = require('./fixtures/portfolio-identity.json');
  const proof = { ...clone(fixture.proof), sourceUrl: candidateValue.sourceUrl, retrievedAt: attempted };
  return { ...clone(fixture.report), sourceUrl: candidateValue.sourceUrl, coverage: 'top10', navYuan: 10000,
    portfolioIdentityEvidence: proof, sourceLocatorEvidence: candidateValue,
    holdings: [{ stockCode: '600001', market: 'CN', currency: 'CNY', shares: 100, weightPct: 1 }] };
}
test('receipt-only failed recollection retains all original report observation times', () => {
  const config = configuration(), originalReport = report(); delete originalReport.sourceLocatorEvidence;
  const previous = buildSnapshot(raw(config, { retrievedAt: '2026-10-25T08:00:00Z', asOf: '2026-10-25',
    funds: [{ code: '000001', reports: [originalReport] }] }), { config, now });
  const result = buildSnapshot(raw(config), { config, now, previous });
  assert.equal(result.coverage.batchFreshFundCount, 0); assert.equal(result.funds[0].dataStatus, 'cached');
  for (const key of ['asOf', 'retrievedAt', 'selectedReport', 'reportManagers', 'priceUpdatedAt']) assert.deepEqual(result.funds[0][key], previous.funds[0][key]);
  assert.deepEqual(result.reportLocatorReceipts, [receipt(config)]);
});

test('conflicting duplicated receipts or an explicit unstarted record cannot manufacture attempts', () => {
  const config = configuration();
  const first = raw(config).funds[0], other = clone(first); other.reportLocatorReceipt.attemptedAt = '2026-10-26T10:31:00Z';
  assert.throws(() => buildSnapshot(raw(config, { funds: [first, other] }), { config, now }), { code: 'NO_VALID_FRESH_FUNDS' });
  const unstarted = { ...first, collectionAttempt: { attempted: false, status: 'deferred-budget', errorCode: 'BATCH_DEADLINE_REACHED' } };
  const result = buildSnapshot(raw(config, { funds: [unstarted] }), { config, now });
  assert.deepEqual(result.reportLocatorReceipts, []); assert.equal(result.batchResults[0].attempted, false);
});

test('locator provenance survives only with an independently verified matching original PDF identity', () => {
  const value = report(), clean = api.normalizeSourceLocatorEvidence(value.sourceLocatorEvidence, value, now);
  assert.deepEqual(clean, value.sourceLocatorEvidence); assert.equal(clean.pdfVerified, false);
  assert.deepEqual(normalizeReport(value).sourceLocatorEvidence, clean);
  const config = configuration(), result = buildSnapshot(raw(config, { funds: [{ code: '000001', reports: [value] }] }), { config, now });
  assert.deepEqual(result.funds[0].selectedReport.sourceLocatorEvidence, clean);
  assert.equal(result.funds[0].selectedReport.portfolioIdentityEvidence.retrievedAt, attempted);
  for (const change of [{ kind: 'semiannual' }, { fundCode: '000003' }, { sourceUrl: 'https://example.org/other.pdf' },
    { publishedAt: '2026-07-22' }, { periodEnd: '2026-09-30' }, { portfolioIdentityEvidence: null }]) {
    assert.equal(api.normalizeSourceLocatorEvidence(value.sourceLocatorEvidence, { ...value, ...change }, now), null);
  }
  const wrong = report(); wrong.portfolioIdentityEvidence.pdfSha256 = null;
  assert.throws(() => buildSnapshot(raw(config, { funds: [{ code: '000001', reports: [wrong] }] }), { config, now }), { code: 'NO_VALID_FRESH_FUNDS' });
});

test('outer requested code binds provenance even if a raw report omits its code', () => {
  const value = report(); delete value.fundCode;
  const config = configuration();
  const accepted = buildSnapshot(raw(config, { funds: [{ code: '000001', reports: [value] }] }), { config, now });
  assert.equal(accepted.funds[0].selectedReport.fundCode, '000001');
  assert.ok(accepted.funds[0].selectedReport.sourceLocatorEvidence);
  const otherConfig = { ...phase, funds: [{ code: '000003' }] };
  assert.throws(() => buildSnapshot(raw(config, { funds: [{ code: '000003', reports: [value] }] }), { config: otherConfig, now }), { code: 'NO_VALID_FRESH_FUNDS' });
});
