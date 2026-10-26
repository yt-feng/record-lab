'use strict';

const { validDate } = require('./fund-analysis');
const { calendar, validCompletePrice } = require('./price-coverage');
const { key: stockKey } = require('./price-patches');
const clone = value => JSON.parse(JSON.stringify(value));
const timestamp = value => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value));

function publicSource(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\u0000-\u001f]/.test(value)) return false;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password && !url.hash
      && /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(url.hostname)
      && /\.[a-z][a-z0-9-]*$/.test(url.hostname) && !/\.(localhost|local|internal)$/.test(url.hostname)
      && ![...url.searchParams.keys()].some(key => /^(token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(key));
  } catch { return false; }
}

function intervalFor(phase) {
  if (!validDate(phase?.targetPeriodEnd) || !validDate(phase?.baselinePeriodEnd)) return null;
  const [year, month] = phase.targetPeriodEnd.split('-').map(Number);
  if (![3, 6, 9, 12].includes(month)
    || new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10) !== phase.targetPeriodEnd) return null;
  const start = `${year}-${String(month - 2).padStart(2, '0')}-01`;
  return new Date(Date.parse(start) - 86400000).toISOString().slice(0, 10) === phase.baselinePeriodEnd
    ? { start, end: phase.targetPeriodEnd } : null;
}

function rowIdentity(row, interval) {
  return stockKey(row) && row.currency === calendar.markets[row.market].currency
    && row.priceRange?.rangeStart === interval.start && row.priceRange?.rangeEnd === interval.end
    && row.priceRange.currency === row.currency
    ? [row.market, row.stockCode, row.currency, 'unadjusted', interval.start, interval.end, calendar.id].join('|') : null;
}

function completeSource(row, fund, interval, now) {
  const price = row.priceRange;
  if (!validCompletePrice(price, row.market, row.stockCode, interval.start, interval.end)
    || !publicSource(price.sourceUrl) || !timestamp(price.fetchedAt)
    || Date.parse(price.fetchedAt) < Date.parse(interval.end)
    || (price.sourceUrls !== undefined && (!Array.isArray(price.sourceUrls) || price.sourceUrls.length > 8
      || price.sourceUrls.some(url => !publicSource(url))))) return false;
  const bounds = [fund.retrievedAt, fund.priceUpdatedAt].filter(timestamp).map(Date.parse);
  return bounds.length > 0 && Math.max(...bounds) <= now + 300000
    && Date.parse(price.fetchedAt) <= Math.max(...bounds);
}

function preferred(a, b) {
  const time = Date.parse(a.price.fetchedAt) - Date.parse(b.price.fetchedAt);
  if (time !== 0) return time > 0 ? a : b;
  return [a.price.sourceUrl, a.code, a.price.fetchedAt].join('|')
    <= [b.price.sourceUrl, b.code, b.price.fetchedAt].join('|') ? a : b;
}

const emptyResult = () => ({ reusedPriceRowCount: 0, reusedPriceFundCount: 0, eligiblePriceKeyCount: 0,
  conflictingPriceKeyCount: 0, partialConflictRowCount: 0, mutatedFunds: [] });

function eligibleFund(fund, index) {
  return fund && typeof fund.code === 'string' && /^\d{6}$/.test(fund.code)
    && ['ok', 'no-equities'].includes(fund.status) && fund.selectedReport?.periodEnd === index.interval.end
    && timestamp(fund.retrievedAt) && Date.parse(fund.retrievedAt) <= index.current + 300000 && Array.isArray(fund.rows)
    && (fund.priceUpdatedAt == null || timestamp(fund.priceUpdatedAt) && Date.parse(fund.priceUpdatedAt) <= index.current + 300000);
}

/** A security-quarter index holds one independent price fact per key, not funds.
 * Add every original source partition before applying it to output partitions.
 * Applying seals the index so a propagated observation cannot become a source.
 */
function createPriceIndex(phase, now) {
  const interval = intervalFor(phase), current = now instanceof Date ? now.getTime() : Date.parse(now);
  return { interval, current, valid: Boolean(interval && Number.isFinite(current)), entries: new Map(), sealed: false };
}

function addPriceSources(index, funds) {
  if (!index?.valid || !Array.isArray(funds)) return index;
  if (index.sealed) throw new Error('PRICE_INDEX_ALREADY_APPLIED');
  for (const fund of funds) {
    if (!eligibleFund(fund, index)) continue;
    for (const row of fund.rows) {
      const key = rowIdentity(row, index.interval);
      if (!key || !completeSource(row, fund, index.interval, index.current)) continue;
      const source = { code: fund.code, price: row.priceRange }, existing = index.entries.get(key);
      if (!existing) index.entries.set(key, { source: { code: source.code, price: clone(source.price) }, conflict: false });
      else {
        if (existing.source.price.low !== source.price.low || existing.source.price.high !== source.price.high) existing.conflict = true;
        if (preferred(existing.source, source) === source) existing.source = { code: source.code, price: clone(source.price) };
      }
    }
  }
  return index;
}

function applyPriceIndex(index, funds, recoverPrice) {
  const result = emptyResult();
  if (!index?.valid || !Array.isArray(funds)) return result;
  if (typeof recoverPrice !== 'function') throw new TypeError('recoverPrice callback is required');
  index.sealed = true;
  const interval = index.interval;
  result.eligiblePriceKeyCount = index.entries.size;
  for (const entry of index.entries.values()) if (entry.conflict) result.conflictingPriceKeyCount++;
  for (const fund of funds) {
    if (!eligibleFund(fund, index)) continue;
    let changed = false;
    for (const row of fund.rows) {
      const key = rowIdentity(row, interval), entry = key && index.entries.get(key), price = row.priceRange;
      if (!entry || entry.conflict || validCompletePrice(price, row.market, row.stockCode, interval.start, interval.end)) continue;
      const source = entry.source.price;
      if (price.status === 'observed' && price.priceBasis === 'unadjusted'
        && Number.isFinite(price.low) && Number.isFinite(price.high) && price.low > 0 && price.high >= price.low
        && (price.low < source.low || price.high > source.high)) {
        result.partialConflictRowCount++;
        continue;
      }
      const candidate = { market: row.market, stockCode: row.stockCode, currency: row.currency, priceRange: clone(price) };
      if (!recoverPrice(candidate, { ...clone(source), dataStatus: 'cached' }, source.fetchedAt)
        || !validCompletePrice(candidate.priceRange, row.market, row.stockCode, interval.start, interval.end)
        || candidate.priceRange.fetchedAt !== source.fetchedAt || candidate.priceRange.sourceUrl !== source.sourceUrl
        || candidate.priceRange.low !== source.low || candidate.priceRange.high !== source.high) continue;
      candidate.priceRange.sourceUrls = [...new Set(source.sourceUrls || [source.sourceUrl])];
      candidate.priceRange.dataStatus = 'cached';
      row.priceRange = candidate.priceRange;
      if (!timestamp(fund.priceUpdatedAt) || Date.parse(source.fetchedAt) > Date.parse(fund.priceUpdatedAt)) fund.priceUpdatedAt = source.fetchedAt;
      result.reusedPriceRowCount++;
      changed = true;
    }
    if (changed) {
      if (fund.summary && typeof fund.summary === 'object') fund.summary.observedPriceCount = fund.rows.filter(row => row.priceRange?.status === 'observed').length;
      result.mutatedFunds.push(fund);
    }
  }
  result.reusedPriceFundCount = result.mutatedFunds.length;
  return result;
}

/** Compatibility wrapper for a complete in-memory pass. */
function reuseCompletePrices(funds, { phase, now, recoverPrice, sourceFunds = funds } = {}) {
  if (!Array.isArray(funds) || !Array.isArray(sourceFunds)) return emptyResult();
  const index = createPriceIndex(phase, now);
  addPriceSources(index, sourceFunds);
  return applyPriceIndex(index, funds, recoverPrice);
}

module.exports = { reuseCompletePrices, createPriceIndex, addPriceSources, applyPriceIndex };
