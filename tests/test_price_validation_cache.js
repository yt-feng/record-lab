'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const { createPriceValidationCache, LIMITS } = require('../lib/price-validation-cache');
const { conflictFor, normalizeConflict, normalizeFact } = require('../lib/price-consistency');
const { priceRange } = require('../lib/fund-analysis');
const { calendar, expectedSessions } = require('../lib/price-coverage');
const { createPriceIndex, addPriceSources, applyPriceIndex } = require('../lib/reuse-prices');
const NOW = '2026-10-27T02:00:00Z', FETCHED = '2026-10-27T01:00:00Z';
const phase = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
const identity = { market: 'CN', stockCode: '600001', currency: 'CNY', rangeStart: '2026-04-01', rangeEnd: phase.targetPeriodEnd, calendarId: calendar.id };
function facts(suffix = '') {
  return [1, .9].map((low, i) => {
    const sourceUrl = `https://example.org/prices-${i}${suffix}`;
    const price = priceRange(identity, { ...identity, requestedStart: identity.rangeStart, requestedEnd: identity.rangeEnd,
      priceBasis: 'unadjusted', sourceUrl, sourceUrls: [sourceUrl], sourceValidation: { rejectedRowCount: 0, conflictingDateCount: 0 },
      rows: expectedSessions(identity.market, identity.rangeStart, identity.rangeEnd).map(date => ({ date, low, high: 2, close: 1.5 })) },
      { start: identity.rangeStart, end: identity.rangeEnd });
    return { price, sourceUrl, sourceUrls: [sourceUrl], fetchedAt: FETCHED };
  });
}
const witness = conflictFor(facts(), identity, NOW);

test('duplicate full witness, fact and pair bodies each require only one validation', () => {
  const cache = createPriceValidationCache(NOW), raw = facts();
  for (let i = 0; i < 12; i++) {
    assert.deepEqual(cache.normalizeConflict(structuredClone(witness), { ...identity }), witness);
    assert.deepEqual(cache.normalizeFact(structuredClone(raw[0]), identity), normalizeFact(raw[0], identity, NOW));
    assert.deepEqual(cache.conflictFor(structuredClone(raw), identity), witness);
  }
  assert.equal(cache.stats().checks, 3); assert.equal(cache.stats().hits, 33);
});

test('changed raw body with retained self-declared digest cannot hit a previous valid witness', () => {
  const cache = createPriceValidationCache(NOW); cache.normalizeConflict(witness, identity);
  const changes = [value => value.facts[0].price.low = .5, value => value.facts[0].price.observedDateSetHash = '0'.repeat(64),
    value => value.facts[0].sourceUrl = 'https://example.org/other', value => value.facts[0].fetchedAt = '2026-10-28T00:00:00Z',
    value => value.extra = true, value => value.extra = undefined];
  for (const change of changes) {
    const bad = structuredClone(witness); change(bad);
    assert.equal(normalizeConflict(bad, identity, NOW), null);
    assert.equal(cache.normalizeConflict(bad, identity), null);
  }
  assert.equal(cache.stats().hits, 0); assert.deepEqual(cache.normalizeConflict(witness, identity), witness);
});

test('identity, fixed clock and non-JSON input remain separate validation boundaries', () => {
  const cache = createPriceValidationCache(NOW); cache.normalizeConflict(witness, identity);
  assert.equal(cache.normalizeConflict(witness, { ...identity, stockCode: '600002' }), null);
  assert.equal(cache.normalizeConflict(witness, { ...identity, rangeStart: '2026-07-01', rangeEnd: '2026-09-30' }), null);
  assert.equal(createPriceValidationCache('2026-10-27T00:00:00Z').normalizeConflict(witness, identity), null);
  const date = new Date(NOW), fixed = createPriceValidationCache(date); date.setTime(0);
  assert.deepEqual(fixed.normalizeConflict(witness, identity), witness);
  const negativeZero = structuredClone(witness); negativeZero.facts[0].price.missingSessionCount = -0;
  const before = cache.stats().hits;
  assert.deepEqual(cache.normalizeConflict(negativeZero, identity), normalizeConflict(negativeZero, identity, NOW));
  assert.equal(cache.stats().hits, before);
  const nonJson = structuredClone(witness); nonJson.extra = () => undefined;
  assert.equal(cache.normalizeConflict(nonJson, identity), normalizeConflict(nonJson, identity, NOW));
});

test('consumer mutation never poisons a later cached result', () => {
  const cache = createPriceValidationCache(NOW), first = cache.normalizeConflict(witness, identity);
  first.facts[0].price.low = 100; first.resolution = 'resolved';
  const second = cache.normalizeConflict(witness, identity); assert.deepEqual(second, witness);
  second.facts.reverse(); assert.deepEqual(cache.normalizeConflict(witness, identity), witness);
});

test('entry and byte limits evict or bypass and revalidate without dropping observations', () => {
  const cache = createPriceValidationCache(NOW, { maxEntries: 4, maxBytes: 60000 });
  const values = ['a', 'b', 'c'].map(suffix => conflictFor(facts(suffix), identity, NOW));
  for (const value of values) assert.deepEqual(cache.normalizeConflict(value, identity), value);
  assert.ok(cache.stats().evictions > 0); assert.ok(cache.stats().entries <= 4); assert.ok(cache.stats().bytes <= 60000);
  const checks = cache.stats().checks; assert.deepEqual(cache.normalizeConflict(values[0], identity), values[0]);
  assert.equal(cache.stats().checks, checks + 1);
  const small = createPriceValidationCache(NOW, { maxEntries: 4, maxBytes: 100 });
  assert.deepEqual(small.normalizeConflict(witness, identity), witness); assert.deepEqual(small.normalizeConflict(witness, identity), witness);
  assert.equal(small.stats().entries, 0); assert.equal(small.stats().checks, 2);
  assert.throws(() => createPriceValidationCache(NOW, { maxBytes: LIMITS.bytes + 1 }), /INVALID_PRICE_CACHE_LIMIT/);
});

test('complete shared index results are identical with validation cache disabled and copied rows stay linear', () => {
  const raw = facts()[0], price = { ...raw.price, sourceUrl: raw.sourceUrl, sourceUrls: raw.sourceUrls, fetchedAt: raw.fetchedAt,
    globalPriceConflict: witness };
  const make = count => Array.from({ length: count }, (_, i) => ({ code: String(100000 + i), status: 'ok', retrievedAt: FETCHED,
    selectedReport: { periodEnd: phase.targetPeriodEnd }, rows: [{ ...identity, priceRange: structuredClone(price) }] }));
  const one = createPriceIndex(phase, NOW); addPriceSources(one, make(1));
  const enabled = createPriceIndex(phase, NOW), disabled = createPriceIndex(phase, NOW, { cacheValidation: false });
  const a = make(12), b = structuredClone(a); addPriceSources(enabled, a); addPriceSources(disabled, b);
  assert.equal(enabled.validation.stats().checks, one.validation.stats().checks);
  assert.ok(disabled.validation.stats().checks >= enabled.validation.stats().checks * 8);
  assert.deepEqual(applyPriceIndex(enabled, a, () => false), applyPriceIndex(disabled, b, () => false));
  assert.deepEqual(a, b);
  // A fresh index never borrows a proof whose observation is future at its clock.
  const earlier = createPriceIndex(phase, '2026-10-27T00:00:00Z'); addPriceSources(earlier, make(1));
  assert.equal(earlier.entries.size, 0);
});
