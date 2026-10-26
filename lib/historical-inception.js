'use strict';

const SOURCE = 'https://www.csrc.gov.cn/csrc/c101900/c1029655/1029655/files/公募基金产品索引（截至20260630）.xlsx';
const HASH = '0f6ac234b792bb6334c0c94ec37fa6c0548363c80017c946d11a5c7e95d3a9e0';
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;

function normalizeInception(raw, fund, now = new Date()) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.schemaVersion !== 1
    || !fund || !/^[0-9]{6}$/.test(fund.code) || raw.fundCode !== fund.code
    || raw.periodEnd !== '2026-06-30' || raw.sourceId !== 'csrc-fund-products-20260630'
    || raw.sourceUrl !== SOURCE || raw.contentHash !== HASH || raw.sheet !== 'Sheet1'
    || !Number.isInteger(raw.row) || raw.row < 3 || raw.row > 14172
    || !date(raw.date) || raw.date > raw.periodEnd || raw.date !== fund.inceptionDate || raw.sourceUrl !== fund.inceptionSourceUrl
    || typeof raw.retrievedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(raw.retrievedAt)
    || !date(raw.retrievedAt.slice(0, 10)) || !Number.isFinite(Date.parse(raw.retrievedAt))
    || raw.retrievedAt.slice(0, 10) < raw.periodEnd || Date.parse(raw.retrievedAt) > now.getTime() + 300000) return null;
  return Object.fromEntries(['schemaVersion', 'fundCode', 'periodEnd', 'date', 'sourceId', 'sourceUrl',
    'contentHash', 'sheet', 'row', 'retrievedAt'].map(key => [key, raw[key]]));
}

const profileSource = (value, code) => typeof code === 'string' && /^[0-9]{6}$/.test(code)
  && value === 'https://fundf10.eastmoney.com/jbgk_' + code + '.html';
function normalizeProfileInception(raw, code, now = new Date()) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.schemaVersion !== 1 || raw.fundCode !== code
    || !profileSource(raw.sourceUrl, code) || !date(raw.date)
    || typeof raw.retrievedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(raw.retrievedAt)
    || !date(raw.retrievedAt.slice(0, 10)) || !Number.isFinite(Date.parse(raw.retrievedAt))
    || raw.date > raw.retrievedAt.slice(0, 10) || Date.parse(raw.retrievedAt) > now.getTime() + 300000) return null;
  return Object.fromEntries(['schemaVersion', 'fundCode', 'date', 'sourceUrl', 'retrievedAt'].map(key => [key, raw[key]]));
}
function historical(raw, code, now) {
  return normalizeInception(raw, { code, inceptionDate: raw?.date, inceptionSourceUrl: raw?.sourceUrl }, now);
}
function normalizeInceptionConflict(raw, code, now = new Date()) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.schemaVersion !== 1 || raw.fundCode !== code
    || raw.status !== 'conflicting-sources') return null;
  const evidence = historical(raw.historicalEvidence, code, now);
  const profile = normalizeProfileInception(raw.profileEvidence, code, now);
  if (!evidence || evidence.periodEnd !== raw.periodEnd || !profile || profile.date === evidence.date) return null;
  return { schemaVersion: 1, fundCode: code, periodEnd: evidence.periodEnd, status: 'conflicting-sources',
    historicalEvidence: evidence, profileEvidence: profile };
}

// Keep independent observation dates. Empty attempts neither erase history nor
// resolve an explicit disagreement. A later matching profile can resolve it.
function inceptionState(current, previous = null, now = new Date(), observedAt = null) {
  const code = current.code;
  if (previous?.code !== code) previous = null;
  const conflict = normalizeInceptionConflict(current.inceptionConflict, code, now);
  const priorConflict = normalizeInceptionConflict(previous?.inceptionConflict, code, now);
  const evidence = normalizeInception(current.inceptionEvidence, current, now) || conflict?.historicalEvidence
    || normalizeInception(previous?.inceptionEvidence, previous, now) || priorConflict?.historicalEvidence || null;
  function profile(fund, at) {
    return normalizeProfileInception(fund?.inceptionProfileEvidence, code, now)
      || normalizeProfileInception({ schemaVersion: 1, fundCode: code, date: fund?.inceptionDate,
        sourceUrl: fund?.inceptionSourceUrl, retrievedAt: at }, code, now);
  }
  const profiles = [profile(current, observedAt || current.retrievedAt), conflict?.profileEvidence,
    profile(previous, previous?.retrievedAt), priorConflict?.profileEvidence].filter(Boolean)
    .sort((a, b) => Date.parse(b.retrievedAt) - Date.parse(a.retrievedAt)
      || Number(b.date !== evidence?.date) - Number(a.date !== evidence?.date));
  const latestProfile = profiles[0] || null;
  const disagreement = evidence && latestProfile && evidence.date !== latestProfile.date
    ? { schemaVersion: 1, fundCode: code, periodEnd: evidence.periodEnd, status: 'conflicting-sources',
      historicalEvidence: evidence, profileEvidence: latestProfile } : null;
  if (disagreement) return { inceptionDate: null, inceptionSourceUrl: null, inceptionEvidence: null,
    inceptionProfileEvidence: latestProfile, inceptionConflict: disagreement };
  return { inceptionDate: evidence?.date || latestProfile?.date || (date(current.inceptionDate) ? current.inceptionDate : null),
    inceptionSourceUrl: evidence?.sourceUrl || latestProfile?.sourceUrl || current.inceptionSourceUrl || null,
    inceptionEvidence: evidence, inceptionProfileEvidence: latestProfile, inceptionConflict: null };
}
function retainInception(target, source, now = new Date()) {
  const state = inceptionState(target, source, now);
  const changed = Object.entries(state).some(([key, value]) => JSON.stringify(target[key] ?? null) !== JSON.stringify(value));
  Object.assign(target, state);
  return changed;
}

function normalizeInceptionPatch(raw, config, now = new Date(), receiptAt = null) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.schemaVersion !== 1
    || raw.kind !== 'inception-source-check' || raw.targetPeriodEnd !== config?.targetPeriodEnd
    || raw.targetPeriodEnd !== '2026-06-30' || typeof raw.code !== 'string' || !/^[0-9]{6}$/.test(raw.code)) return null;
  const evidence = historical(raw.historicalEvidence, raw.code, now);
  const profile = normalizeProfileInception(raw.profileEvidence, raw.code, now);
  if (!evidence || evidence.periodEnd !== raw.targetPeriodEnd || !profile || raw.observedAt !== profile.retrievedAt
    || !Number.isFinite(Date.parse(receiptAt)) || Date.parse(profile.retrievedAt) > Date.parse(receiptAt)
    || raw.status !== (profile.date === evidence.date ? 'consistent' : 'conflict')) return null;
  return { schemaVersion: 1, kind: 'inception-source-check', code: raw.code, targetPeriodEnd: raw.targetPeriodEnd,
    status: raw.status, observedAt: profile.retrievedAt, historicalEvidence: evidence, profileEvidence: profile };
}
function inceptionPatch(fund, config, now, receiptAt) {
  const conflict = normalizeInceptionConflict(fund.inceptionConflict, fund.code, now);
  const evidence = normalizeInception(fund.inceptionEvidence, fund, now) || conflict?.historicalEvidence;
  // Metadata-only repair requires an actual, separately dated profile receipt.
  const profile = normalizeProfileInception(fund.inceptionProfileEvidence, fund.code, now) || conflict?.profileEvidence;
  if (!evidence || !profile) return null;
  return normalizeInceptionPatch({ schemaVersion: 1, kind: 'inception-source-check', code: fund.code,
    targetPeriodEnd: config?.targetPeriodEnd, observedAt: profile.retrievedAt,
    status: profile.date === evidence.date ? 'consistent' : 'conflict',
    historicalEvidence: evidence, profileEvidence: profile }, config, now, receiptAt);
}

module.exports = { normalizeInception, normalizeProfileInception, normalizeInceptionConflict, profileSource,
  inceptionState, retainInception, inceptionPatch, normalizeInceptionPatch };
