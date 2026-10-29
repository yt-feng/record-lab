'use strict';

const { validDate } = require('./fund-analysis');
const { calendar, validCompletePrice } = require('./price-coverage');
const { key: stockKey } = require('./price-patches');
const { createPriceValidationCache } = require('./price-validation-cache');
const { buildReference, normalizeReference, effectiveReference } = require('./price-range-reference');
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
  conflictingPriceKeyCount: 0, partialConflictRowCount: 0, conflictMarkedRowCount: 0, conflictMarkedFundCount: 0,
  conflictingPriceRowCount: 0, referenceAnnotatedRowCount: 0, referenceAnnotatedFundCount: 0, mutatedFunds: [] });

const identityFor = (row, interval) => ({ market: row.market, stockCode: row.stockCode, currency: row.currency,
  rangeStart: interval.start, rangeEnd: interval.end, calendarId: calendar.id });
const factFor = price => ({ price: clone(price), sourceUrl: price.sourceUrl,
  sourceUrls: price.sourceUrls || [price.sourceUrl], fetchedAt: price.fetchedAt });

function observedSource(row, fund, interval, now) {
  const price = row.priceRange;
  return price?.status === 'observed' && price.priceBasis === 'unadjusted'
    && Number.isFinite(price.low) && Number.isFinite(price.high) && price.low > 0 && price.high >= price.low
    && Number.isInteger(price.observedCount) && price.observedCount > 0
    && validDate(price.observedStart) && validDate(price.observedEnd)
    && interval.start <= price.observedStart && price.observedStart <= price.observedEnd && price.observedEnd <= interval.end
    && publicSource(price.sourceUrl) && timestamp(price.fetchedAt)
    && Date.parse(price.fetchedAt) >= Date.parse(interval.end)
    && Date.parse(price.fetchedAt) <= Math.max(...[fund.retrievedAt, fund.priceUpdatedAt].filter(timestamp).map(Date.parse))
    && Date.parse(price.fetchedAt) <= now + 300000
    && (price.sourceUrls === undefined || Array.isArray(price.sourceUrls) && price.sourceUrls.length <= 8
      && price.sourceUrls.every(publicSource));
}

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
function createPriceIndex(phase, now, { cacheValidation = true } = {}) {
  const interval = intervalFor(phase), current = now instanceof Date ? now.getTime() : Date.parse(now);
  return { interval, current, valid: Boolean(interval && Number.isFinite(current)), entries: new Map(),
    partials: new Map(), sealed: false, validation: createPriceValidationCache(current, { enabled: cacheValidation }) };
}

function rememberSource(index, key, source, row) {
  source = clone(source);
  delete source.price.globalPriceConflict;
  delete source.price.rangeReference;
  let existing = index.entries.get(key);
  if (!existing) {
    existing = { source: clone(source), conflict: false, witness: null, facts: new Map(), retained: new Map(), reference: null,
      identity: identityFor(row, index.interval) };
    index.entries.set(key, existing);
  } else if (preferred(existing.source, source) === source) existing.source = clone(source);
  const fact = index.validation.normalizeFact(factFor(source.price), existing.identity);
  if (fact) {
    // Read-only raw-difference diagnostics inspect an unsealed index. Retain
    // that view until sealing computes effective references over all facts.
    const prior = existing.facts.values().next().value;
    const witness = prior && (prior.price.low !== fact.price.low || prior.price.high !== fact.price.high)
      && index.validation.conflictFor([prior, fact], existing.identity);
    if (witness) {
      existing.conflict = true;
      if (!existing.witness || witness.witnessDigest < existing.witness.witnessDigest) existing.witness = witness;
    }
    const old = existing.facts.get(fact.factId);
    if (!old || require('../web/price-range-reference').preferredFact(old, fact) === fact) existing.facts.set(fact.factId, fact);
  }
}

function addPriceSources(index, funds) {
  if (!index?.valid || !Array.isArray(funds)) return index;
  if (index.sealed) throw new Error('PRICE_INDEX_ALREADY_APPLIED');
  for (const fund of funds) {
    if (!eligibleFund(fund, index)) continue;
    for (const row of fund.rows) {
      const key = rowIdentity(row, index.interval);
      if (!key) continue;
      // Preserve an unresolved witnessed disagreement even if portfolio
      // deduplication no longer publishes one of the original source rows.
      const retained = index.validation.normalizeConflict(row.priceRange.globalPriceConflict, identityFor(row, index.interval));
      if (retained) {
        for (const fact of retained.facts) {
          if (validCompletePrice(fact.price, row.market, row.stockCode, index.interval.start, index.interval.end)) {
            rememberSource(index, key, { code: fund.code, price: { ...fact.price, sourceUrl: fact.sourceUrl,
              sourceUrls: fact.sourceUrls, fetchedAt: fact.fetchedAt } }, row);
          }
        }
        const entry = index.entries.get(key);
        if (entry) {
          entry.retained.set(retained.witnessDigest, retained);
          entry.conflict = true;
          if (!entry.witness || retained.witnessDigest < entry.witness.witnessDigest) entry.witness = retained;
        }
      }
      // A previous reference may retain a source removed by portfolio dedup.
      const reference = normalizeReference(row.priceRange.rangeReference, identityFor(row, index.interval), index.current);
      if (reference) for (const fact of reference.facts) rememberSource(index, key,
        { code: fund.code, price: { ...fact.price, sourceUrl: fact.sourceUrl, sourceUrls: fact.sourceUrls, fetchedAt: fact.fetchedAt } }, row);
      const fact = index.validation.normalizeFact(factFor(row.priceRange), identityFor(row, index.interval));
      if (fact && completeSource(row, fund, index.interval, index.current)) rememberSource(index, key, { code: fund.code, price: row.priceRange }, row);
      else if (fact && observedSource(row, fund, index.interval, index.current)) {
        const old = index.partials.get(key) || [];
        const price = clone(row.priceRange); delete price.globalPriceConflict;
        const candidates = [...old, { code: fund.code, price, identity: identityFor(row, index.interval) }];
        const byLow = [...candidates].sort((a, b) => a.price.low - b.price.low || a.code.localeCompare(b.code));
        const byHigh = [...candidates].sort((a, b) => b.price.high - a.price.high || a.code.localeCompare(b.code));
        index.partials.set(key, [byLow[0], byHigh[0]]);
      }
    }
  }
  return index;
}

function applyPriceIndex(index, funds, recoverPrice) {
  const result = emptyResult();
  if (!index?.valid || !Array.isArray(funds)) return result;
  if (typeof recoverPrice !== 'function') throw new TypeError('recoverPrice callback is required');
  if (!index.sealed) {
    for (const [key, entry] of index.entries) {
      const facts = [...entry.facts.values()];
      entry.reference = buildReference(facts, entry.identity, index.current);
      const rawWitnesses = [...entry.retained.values()];
      for (const fact of facts.slice(1)) {
        const witness = index.validation.conflictFor([facts[0], fact], entry.identity);
        if (witness) rawWitnesses.push(witness);
      }
      const eastmoney = facts.filter(f => require('../web/price-range-reference').providerFor(f, entry.identity) === 'eastmoney');
      for (const fact of eastmoney.slice(1)) {
        const witness = index.validation.conflictFor([eastmoney[0], fact], entry.identity);
        if (witness) rawWitnesses.push(witness);
      }
      const selected = entry.reference && entry.reference.facts.find(f => f.factId === entry.reference.referenceFactId);
      if (selected) entry.source = { code: entry.source.code, price: { ...selected.price, sourceUrl: selected.sourceUrl,
        sourceUrls: selected.sourceUrls, fetchedAt: selected.fetchedAt } };
      for (const partial of index.partials.get(key) || []) {
        const witness = index.validation.conflictFor([factFor(entry.source.price), factFor(partial.price)], partial.identity);
        if (witness) rawWitnesses.push(witness);
      }
      // Complete-fact compatibility never resolves a partial extreme.
      // Keep a material pair through portfolio deduplication. An arbitrary
      // minimum witness could retain only a compatible EM/Yahoo pair while
      // dropping the third, genuinely different EM observation.
      const unresolved = rawWitnesses.filter(w => w.types.includes('partial-extreme')
        || !entry.reference && !buildReference(w.facts, entry.identity, index.current));
      entry.witness = unresolved.sort((a, b) => a.witnessDigest.localeCompare(b.witnessDigest))[0] || null;
      entry.conflict = Boolean(entry.witness);
      if (entry.conflict) entry.reference = null;
    }
    index.cachedCounts = { eligiblePriceKeyCount: index.entries.size,
      conflictingPriceKeyCount: [...index.entries.values()].filter(entry => entry.conflict).length };
  }
  index.sealed = true;
  const interval = index.interval;
  Object.assign(result, index.cachedCounts);
  for (const fund of funds) {
    if (!eligibleFund(fund, index)) continue;
    let changed = false, priceChanged = false, conflictChanged = false, referenceChanged = false;
    for (const row of fund.rows) {
      const key = rowIdentity(row, interval), entry = key && index.entries.get(key), price = row.priceRange;
      if (!entry) continue;
      if (entry.conflict) {
        result.conflictingPriceRowCount++;
        if (!validCompletePrice(price, row.market, row.stockCode, interval.start, interval.end)
          && price.status === 'observed' && (price.low < entry.source.price.low || price.high > entry.source.price.high)) result.partialConflictRowCount++;
        if (JSON.stringify(price.globalPriceConflict) !== JSON.stringify(entry.witness)) {
          price.globalPriceConflict = clone(entry.witness);
          result.conflictMarkedRowCount++; changed = true; conflictChanged = true;
        }
        if (Object.hasOwn(price, 'rangeReference')) { delete price.rangeReference; changed = true; conflictChanged = true; }
        continue;
      }
      if (entry.reference) {
        const candidate = { ...price, rangeReference: entry.reference };
        if (effectiveReference(candidate, identityFor(row, interval), index.current)) {
          if (JSON.stringify(price.rangeReference) !== JSON.stringify(entry.reference)) {
            price.rangeReference = clone(entry.reference);
            result.referenceAnnotatedRowCount++; result.reusedPriceRowCount++;
            changed = true; priceChanged = true; referenceChanged = true;
          }
          continue;
        }
      }
      if (validCompletePrice(price, row.market, row.stockCode, interval.start, interval.end)) continue;
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
      changed = true; priceChanged = true;
    }
    if (changed) {
      if (fund.summary && typeof fund.summary === 'object') fund.summary.observedPriceCount = fund.rows.filter(row => row.priceRange?.status === 'observed').length;
      result.mutatedFunds.push(fund);
    }
    if (priceChanged) result.reusedPriceFundCount++;
    if (referenceChanged) result.referenceAnnotatedFundCount++;
    if (conflictChanged) result.conflictMarkedFundCount++;
  }
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
