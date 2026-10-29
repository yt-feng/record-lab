'use strict';
const crypto = require('node:crypto');
const calendar = require('../config/trading-calendars-2026.json');
const VERSION = 2;
const validDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const dateHash = values => crypto.createHash('sha256').update([...new Set(values)].sort().join('|')).digest('hex');
const sessionCache = new Map();

function expectedSessions(market, start, end) {
  if (!Object.hasOwn(calendar.markets, market) || !validDate(start) || !validDate(end)
    || start < calendar.supportedStart || end > calendar.supportedEnd || start > end) return null;
  const key = `${market}:${start}:${end}`;
  if (sessionCache.has(key)) return sessionCache.get(key);
  const closed = new Set(calendar.markets[market].closedDates), result = [];
  for (let date = new Date(`${start}T00:00:00Z`); date.toISOString().slice(0, 10) <= end; date.setUTCDate(date.getUTCDate() + 1)) {
    const day = date.toISOString().slice(0, 10);
    if (![0, 6].includes(date.getUTCDay()) && !closed.has(day)) result.push(day);
  }
  sessionCache.set(key, Object.freeze(result));
  return sessionCache.get(key);
}

function inspectPriceCoverage(holding, series, interval, observations, rejectedRowCount) {
  const expected = expectedSessions(holding?.market, interval?.start, interval?.end);
  const dates = observations.map(row => row.date), seen = new Set(dates), required = new Set(expected || []);
  const missing = (expected || []).filter(day => !seen.has(day));
  const unexpected = expected ? dates.filter(day => !required.has(day)) : [];
  const source = series?.sourceValidation || {};
  const metadataVerified = Number.isInteger(source.rejectedRowCount) && source.rejectedRowCount >= 0
    && Number.isInteger(source.conflictingDateCount) && source.conflictingDateCount >= 0;
  const sourceRejected = Number.isInteger(source.rejectedRowCount) && source.rejectedRowCount >= 0 ? source.rejectedRowCount : 0;
  const conflicts = Number.isInteger(source.conflictingDateCount) && source.conflictingDateCount >= 0 ? source.conflictingDateCount : 0;
  const rejected = rejectedRowCount + sourceRejected;
  const identity = metadataVerified && series?.market === holding?.market && series?.stockCode === holding?.stockCode
    && validDate(series?.requestedStart) && validDate(series?.requestedEnd)
    && series.requestedStart <= interval.start && series.requestedEnd >= interval.end;
  return { coverageVersion: VERSION, calendarId: calendar.id, sessionScope: calendar.sessionScope,
    expectedSessionCount: expected ? expected.length : null, expectedDateSetHash: expected ? dateHash(expected) : null,
    observedDateSetHash: dateHash(dates), missingSessionCount: expected ? missing.length : null, missingSessionDates: missing,
    unexpectedSessionCount: unexpected.length, unexpectedSessionDates: unexpected,
    rejectedRowCount: rejected, conflictingDateCount: conflicts,
    completeness: expected?.length && identity && !missing.length && !unexpected.length && !rejected && !conflicts
      ? 'complete' : expected ? 'partial' : 'calendar-unavailable' };
}

function validCompletePrice(value, market, code, start, end) {
  const expected = expectedSessions(market, start, end);
  return Boolean(value && expected?.length && value.coverageVersion === VERSION && value.calendarId === calendar.id
    && value.sessionScope === calendar.sessionScope && value.market === market && value.stockCode === code
    && value.rangeStart === start && value.rangeEnd === end && value.currency === calendar.markets[market].currency
    && value.priceBasis === 'unadjusted' && value.status === 'observed' && value.completeness === 'complete'
    && Number.isFinite(value.low) && Number.isFinite(value.high) && value.low > 0 && value.high >= value.low
    && value.expectedSessionCount === expected.length && value.observedCount === expected.length
    && value.observedStart === expected[0] && value.observedEnd === expected.at(-1)
    && value.expectedDateSetHash === dateHash(expected) && value.observedDateSetHash === value.expectedDateSetHash
    && ['missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount'].every(key => value[key] === 0)
    && Array.isArray(value.missingSessionDates) && !value.missingSessionDates.length
    && Array.isArray(value.unexpectedSessionDates) && !value.unexpectedSessionDates.length);
}

function retainedCoverage(value, market, code, start, end) {
  const expected = expectedSessions(market, start, end);
  const current = value?.coverageVersion === VERSION && value.calendarId === calendar.id
    && expected && value.expectedDateSetHash === dateHash(expected);
  const count = key => current && Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 10000 ? value[key] : null;
  const dates = key => current && Array.isArray(value[key]) && value[key].length <= 400
    && value[key].every(day => validDate(day) && start <= day && day <= end) ? [...new Set(value[key])].sort() : [];
  return { coverageVersion: VERSION, calendarId: calendar.id, sessionScope: calendar.sessionScope,
    market, stockCode: code, expectedSessionCount: expected?.length ?? null, expectedDateSetHash: expected ? dateHash(expected) : null,
    observedDateSetHash: current && /^[a-f0-9]{64}$/.test(value.observedDateSetHash) ? value.observedDateSetHash : null,
    missingSessionCount: count('missingSessionCount'), missingSessionDates: dates('missingSessionDates'),
    unexpectedSessionCount: count('unexpectedSessionCount'), unexpectedSessionDates: dates('unexpectedSessionDates'),
    rejectedRowCount: count('rejectedRowCount') ?? 0, conflictingDateCount: count('conflictingDateCount') ?? 0,
    completeness: validCompletePrice(value, market, code, start, end) ? 'complete' : expected ? 'partial' : 'calendar-unavailable' };
}

function validCompleteQuarterPrice(value, market, code, period) {
  if (!validDate(period)) return false;
  const month = Math.floor((Number(period.slice(5, 7)) - 1) / 3) * 3 + 1;
  const start = `${period.slice(0, 4)}-${String(month).padStart(2, '0')}-01`;
  return validCompletePrice(value, market, code, start, period);
}

// Calendar completeness is an original source fact. A global disagreement is
// a separate acceptance gate; never erase the calendar proof to represent it.
function validUsableCompletePrice(value, market, code, start, end) {
  const effective = require('./price-range-reference').effectivePriceRange(value,
    { market, stockCode: code, currency: calendar.markets[market]?.currency, rangeStart: start, rangeEnd: end, calendarId: calendar.id });
  return validCompletePrice(effective, market, code, start, end);
}

function validUsableCompleteQuarterPrice(value, market, code, period) {
  if (!validDate(period)) return false;
  const month = Math.floor((Number(period.slice(5, 7)) - 1) / 3) * 3 + 1;
  return validUsableCompletePrice(value, market, code, `${period.slice(0, 4)}-${String(month).padStart(2, '0')}-01`, period);
}

module.exports = { VERSION, calendar, dateHash, expectedSessions, inspectPriceCoverage, validCompletePrice, validCompleteQuarterPrice,
  validUsableCompletePrice, validUsableCompleteQuarterPrice, retainedCoverage };
