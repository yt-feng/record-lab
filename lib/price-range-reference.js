'use strict';
const raw = require('./price-consistency');
const math = require('../web/price-range-reference');
const { validCompletePrice } = require('./price-coverage');
const clone = value => JSON.parse(JSON.stringify(value));

function buildReference(facts, identity, now) {
  if (!Array.isArray(facts) || facts.length > 4096) return null;
  const byId = new Map();
  for (const value of facts) {
    const fact = raw.normalizeFact(value, identity, now);
    if (!fact || !validCompletePrice(fact.price, identity.market, identity.stockCode, identity.rangeStart, identity.rangeEnd)) return null;
    const previous = byId.get(fact.factId);
    if (!previous || math.preferredFact(previous, fact) === fact) byId.set(fact.factId, fact);
  }
  const normalized = [...byId.values()].sort((a, b) => a.factId.localeCompare(b.factId));
  const selected = math.selectFact(normalized, identity);
  if (!selected) return null;
  return { schemaVersion: 1, kind: 'stored-range-reference', method: math.METHOD, scope: 'quarter-extrema-only',
    key: math.keyFor(identity), facts: normalized, referenceFactId: selected.factId,
    factSetDigest: raw.digest(normalized.map(f => f.factId)),
    marketDataRequests: 0, dailySeriesReverified: false, originalResponsesReverified: false };
}
function normalizeReference(value, identity, now) {
  try {
    const rebuilt = buildReference(value?.facts, identity, now);
    return rebuilt && raw.canonical(rebuilt) === raw.canonical(value) ? rebuilt : null;
  } catch { return null; }
}
function effectiveReference(range, identity = range, now) {
  const ref = normalizeReference(range?.rangeReference, identity, now);
  if (!ref) return null;
  if (Object.hasOwn(range, 'globalPriceConflict') && !raw.normalizeConflict(range.globalPriceConflict, identity, now)) return null;
  if (range.status === 'observed' && !raw.normalizeFact({ price: range, sourceUrl: range.sourceUrl,
    sourceUrls: range.sourceUrls || [range.sourceUrl], fetchedAt: range.fetchedAt }, identity, now)) return null;
  return math.effectiveReference({ ...range, rangeReference: ref });
}
function effectivePriceRange(range, identity = range, now) {
  const reference = effectiveReference(range, identity, now);
  return reference || (range && !Object.hasOwn(range, 'globalPriceConflict') ? range : null);
}
function unresolvedConflict(range, identity = range, now) {
  return Boolean(range && raw.normalizeConflict(range.globalPriceConflict, identity, now) && !effectiveReference(range, identity, now));
}
function resolutionFor(range, identity = range, now) {
  const effective = effectiveReference(range, identity, now);
  return effective && raw.normalizeConflict(range.globalPriceConflict, identity, now)
    ? { market: identity.market, stockCode: identity.stockCode, reference: clone(effective.rangeReference),
      rawWitness: clone(range.globalPriceConflict) } : null;
}
module.exports = { buildReference, normalizeReference, effectiveReference, effectivePriceRange, unresolvedConflict,
  resolutionFor,
  factFor: price => ({ price: clone(price), sourceUrl: price.sourceUrl, sourceUrls: price.sourceUrls || [price.sourceUrl], fetchedAt: price.fetchedAt }) };
