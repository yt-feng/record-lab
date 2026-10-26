'use strict';

// A witness identifies a contradiction. It does not decide which quote is right.
const crypto = require('node:crypto');
const { calendar, expectedSessions, dateHash, validCompletePrice } = require('./price-coverage');
const PRICE_FIELDS = ['status', 'low', 'high', 'market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd',
  'coverageVersion', 'calendarId', 'sessionScope', 'completeness', 'observedCount', 'observedStart', 'observedEnd',
  'expectedSessionCount', 'expectedDateSetHash', 'observedDateSetHash', 'missingSessionCount', 'missingSessionDates',
  'unexpectedSessionCount', 'unexpectedSessionDates', 'rejectedRowCount', 'conflictingDateCount'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const clone = value => JSON.parse(JSON.stringify(value));
const same = (a, b) => canonical(a) === canonical(b);
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const code = row => object(row) && typeof row.stockCode === 'string'
  && ({ CN: /^\d{6}$/, HK: /^\d{5}$/, US: /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/ }[row.market])?.test(row.stockCode);
function compareText(a, b) {
  const left = Array.from(a, value => value.codePointAt(0)), right = Array.from(b, value => value.codePointAt(0));
  for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i] - right[i];
  return left.length - right.length;
}
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (object(value)) return '{' + Object.keys(value).sort(compareText).map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('INVALID_NUMBER');
  if (value === undefined) throw new Error('INVALID_VALUE');
  return JSON.stringify(value);
}
function floatHex(value) {
  if (!Number.isFinite(value)) throw new Error('INVALID_NUMBER');
  const buffer = Buffer.alloc(8); buffer.writeDoubleBE(value);
  const bits = buffer.readBigUInt64BE(), negative = bits >> 63n ? '-' : '';
  const exponent = Number((bits >> 52n) & 2047n), fraction = bits & ((1n << 52n) - 1n);
  if (!exponent && !fraction) return negative + '0x0.0p+0';
  const power = exponent ? exponent - 1023 : -1022;
  return `${negative}0x${exponent ? '1' : '0'}.${fraction.toString(16).padStart(13, '0')}p${power >= 0 ? '+' : ''}${power}`;
}
function digest(value) {
  const numeric = item => typeof item === 'number' ? { number: floatHex(item) }
    : Array.isArray(item) ? item.map(numeric)
      : object(item) ? Object.fromEntries(Object.entries(item).map(([key, row]) => [key, numeric(row)])) : item;
  return crypto.createHash('sha256').update(canonical(numeric(value))).digest('hex');
}
const quotePlus = value => encodeURIComponent(value).replace(/[!'()*]/g, character => '%' + character.charCodeAt(0).toString(16).toUpperCase()).replace(/%20/g, '+');
function sourceIdentity(source) {
  if (typeof source !== 'string' || source.length > 2048 || /[\s\\\u0000-\u001f\u007f]/.test(source) || /[\ud800-\udfff]/u.test(source)) return null;
  try {
    const parsed = new URL(source), raw = /^https:\/\/([^/?#]+)([^?#]*)(?:\?([^#]*))?$/i.exec(source);
    if (!raw || !/^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]*)?$/.test(raw[1])
      || parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.hash
      || !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(parsed.hostname) || !/\.[a-z][a-z0-9-]*$/.test(parsed.hostname)
      || /\.(localhost|local|internal)$/.test(parsed.hostname)) return null;
    let pairs = [...new URLSearchParams(raw[3] || '')];
    if (pairs.some(([key]) => /^(token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(key))) return null;
    let authority = raw[1];
    if (['query1.finance.yahoo.com', 'query2.finance.yahoo.com'].includes(parsed.hostname)) {
      authority = 'query1.finance.yahoo.com'; pairs = pairs.filter(([key]) => key !== 'period2');
    }
    pairs.sort((a, b) => compareText(a[0], b[0]) || compareText(a[1], b[1]));
    const query = pairs.map(([key, value]) => quotePlus(key) + '=' + quotePlus(value)).join('&');
    return 'https://' + authority + raw[2] + (query ? '?' + query : '');
  } catch { return null; }
}
function instant(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match || !date(match[1]) || Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4]) > 59
    || match[6] !== 'Z' && (Number(match[6].slice(1, 3)) > 23 || Number(match[6].slice(4)) > 59)) return null;
  const ms = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}${match[6]}`);
  return Number.isFinite(ms) ? BigInt(ms) * 1000n + BigInt((match[5] || '').padEnd(6, '0')) : null;
}
function currentInstant(value) {
  if (value === undefined) return BigInt(Date.now()) * 1000n;
  if (value instanceof Date) value = value.getTime();
  return typeof value === 'number' && Number.isSafeInteger(value) ? BigInt(value) * 1000n : instant(value);
}
function identityFor(value) {
  if (!code(value)) return null;
  const row = Object.fromEntries(['market', 'stockCode', 'currency', 'rangeStart', 'rangeEnd', 'calendarId'].map(key => [key, value[key]]));
  if (row.currency !== calendar.markets[row.market].currency || row.calendarId !== calendar.id || !date(row.rangeStart) || !date(row.rangeEnd)) return null;
  const [year, month] = row.rangeEnd.split('-').map(Number);
  if (![3, 6, 9, 12].includes(month) || row.rangeEnd !== new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10)
    || row.rangeStart !== `${year}-${String(month - 2).padStart(2, '0')}-01` || !expectedSessions(row.market, row.rangeStart, row.rangeEnd)?.length) return null;
  return row;
}
function cleanPrice(value, row) {
  if (!object(value) || PRICE_FIELDS.some(key => !Object.hasOwn(value, key))) return null;
  const price = Object.fromEntries(PRICE_FIELDS.map(key => [key, clone(value[key])]));
  if (Object.keys(row).some(key => price[key] !== row[key]) || price.status !== 'observed' || price.priceBasis !== 'unadjusted'
    || price.coverageVersion !== 2 || price.sessionScope !== calendar.sessionScope || !['complete', 'partial'].includes(price.completeness)
    || !Number.isFinite(price.low) || !Number.isFinite(price.high) || !(price.low > 0 && price.high >= price.low)) return null;
  if (['observedCount', 'expectedSessionCount', 'missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount']
    .some(key => !Number.isInteger(price[key]) || price[key] < 0 || price[key] > 100000) || price.rejectedRowCount || price.conflictingDateCount) return null;
  const expected = expectedSessions(row.market, row.rangeStart, row.rangeEnd), expectedSet = new Set(expected);
  for (const key of ['missingSessionDates', 'unexpectedSessionDates']) {
    const values = price[key];
    if (!Array.isArray(values) || values.length > 100 || values.some(day => !date(day) || day < row.rangeStart || day > row.rangeEnd)
      || !same(values, [...new Set(values)].sort())) return null;
  }
  const missing = new Set(price.missingSessionDates), unexpected = price.unexpectedSessionDates;
  if ([...missing].some(day => !expectedSet.has(day)) || unexpected.some(day => expectedSet.has(day))) return null;
  const observed = [...expected.filter(day => !missing.has(day)), ...unexpected].sort();
  if (!observed.length || price.expectedSessionCount !== expected.length || price.expectedDateSetHash !== dateHash(expected)
    || price.missingSessionCount !== missing.size || price.unexpectedSessionCount !== unexpected.length
    || price.observedCount !== observed.length || price.observedStart !== observed[0] || price.observedEnd !== observed.at(-1)
    || price.observedDateSetHash !== dateHash(observed)) return null;
  const complete = validCompletePrice(price, row.market, row.stockCode, row.rangeStart, row.rangeEnd);
  if (price.completeness === 'complete' && !complete || price.completeness === 'partial' && !missing.size && !unexpected.length) return null;
  return price;
}
function factFor(value, row, now) {
  if (!object(value)) return null;
  const price = cleanPrice(value.price, row), observed = instant(value.fetchedAt);
  if (!price || !Array.isArray(value.sourceUrls) || value.sourceUrls.length < 1 || value.sourceUrls.length > 8
    || observed === null || observed < instant(row.rangeEnd + 'T00:00:00Z') || observed > now + 300000000n) return null;
  const sources = [value.sourceUrl, ...value.sourceUrls].map(sourceIdentity);
  if (sources.some(item => item === null)) return null;
  return { factId: digest({ sourceIdentity: [...new Set(sources)].sort(compareText), price }), price,
    sourceUrl: value.sourceUrl, sourceUrls: [...value.sourceUrls], fetchedAt: value.fetchedAt };
}
function conflictFor(facts, identity, now) {
  try {
    const current = currentInstant(now), row = identityFor(identity);
    if (current === null || !row || !Array.isArray(facts) || facts.length !== 2) return null;
    const clean = facts.map(value => factFor(value, row, current));
    if (clean.some(value => !value)) return null;
    clean.sort((a, b) => compareText(a.factId, b.factId));
    if (clean[0].factId === clean[1].factId) return null;
    const complete = clean.map(value => validCompletePrice(value.price, row.market, row.stockCode, row.rangeStart, row.rangeEnd));
    const [a, b] = clean.map(value => value.price); let kind;
    if (complete.every(Boolean) && (a.low !== b.low || a.high !== b.high)) kind = 'complete-source-disagreement';
    else if (complete.filter(Boolean).length === 1) {
      const [whole, partial] = complete[0] ? [a, b] : [b, a];
      if (!(partial.low < whole.low || partial.high > whole.high)) return null;
      kind = 'partial-extreme';
    } else return null;
    const key = [row.market, row.stockCode, row.currency, 'unadjusted', row.rangeStart, row.rangeEnd, row.calendarId].join('|');
    const types = [kind], result = { schemaVersion: 1, key, types, facts: clean,
      witnessDigest: digest({ key, types, factIds: clean.map(value => value.factId) }), resolution: 'unresolved' };
    return Buffer.byteLength(canonical(result)) <= 65536 ? result : null;
  } catch { return null; }
}
function normalizeConflict(value, identity, now) {
  if (!object(value)) return null;
  const result = conflictFor(value.facts, identity, now);
  try { return result && same(result, value) ? result : null; } catch { return null; }
}
function normalizeFact(value, identity, now) {
  try {
    const row = identityFor(identity), current = currentInstant(now);
    return row && current !== null ? factFor(value, row, current) : null;
  } catch { return null; }
}
const isConflict = (value, identity, now) => Boolean(normalizeConflict(value, identity, now));
module.exports = { PRICE_FIELDS, canonical, floatHex, digest, sourceIdentity, conflictFor, normalizeFact, normalizeConflict, isConflict };
