'use strict';

// One invocation, one fixed clock. Cache the complete raw body and identity,
// never a self-declared witness/fact digest. Returned objects are independent.
const { canonical, normalizeConflict, normalizeFact, conflictFor } = require('./price-consistency');
const clone = value => JSON.parse(JSON.stringify(value));
const LIMITS = Object.freeze({ entries: 4096, bytes: 16 * 1024 * 1024, itemBytes: 256 * 1024 });

function fingerprint(value, seen = new Set()) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return Object.is(value, -0) ? '-0' : JSON.stringify(value);
  if (!value || typeof value !== 'object' || seen.has(value) || seen.size > 100
    || Object.getOwnPropertySymbols(value).length) throw new TypeError('NOT_JSON');
  const array = Array.isArray(value), prototype = Object.getPrototypeOf(value);
  if (!array && prototype !== Object.prototype && prototype !== null) throw new TypeError('NOT_JSON');
  const keys = Object.keys(value), descriptors = Object.getOwnPropertyDescriptors(value);
  if (keys.some(key => !Object.hasOwn(descriptors[key], 'value'))
    || Object.getOwnPropertyNames(value).length !== keys.length + (array ? 1 : 0)
    || array && (keys.length !== value.length || keys.some((key, i) => key !== String(i)))) throw new TypeError('NOT_JSON');
  seen.add(value);
  const result = array ? '[' + value.map(item => fingerprint(item, seen)).join(',') + ']'
    : '{' + keys.sort().map(key => JSON.stringify(key) + ':' + fingerprint(value[key], seen)).join(',') + '}';
  seen.delete(value); return result;
}

function createPriceValidationCache(now, { enabled = true, maxEntries = LIMITS.entries, maxBytes = LIMITS.bytes } = {}) {
  if (typeof enabled !== 'boolean' || !Number.isInteger(maxEntries) || maxEntries < 0 || maxEntries > LIMITS.entries
    || !Number.isInteger(maxBytes) || maxBytes < 0 || maxBytes > LIMITS.bytes) throw new TypeError('INVALID_PRICE_CACHE_LIMIT');
  const fixedNow = now === undefined ? Date.now() : now instanceof Date ? now.getTime() : now;
  // Separate buckets keep many one-off local quotes from evicting every
  // shared witness. The sum of all buckets remains under the stated bound.
  const kinds = ['normalizeConflict', 'normalizeFact', 'conflictFor'];
  const shares = [0.5, 0.25, 0.25], buckets = new Map(kinds.map((kind, i) => [kind,
    { entries: new Map(), bytes: 0, maxEntries: Math.floor(maxEntries * shares[i]), maxBytes: Math.floor(maxBytes * shares[i]) }]));
  const counts = { hits: 0, misses: 0, bypasses: 0, evictions: 0, checks: 0 };
  const validators = { normalizeConflict, normalizeFact, conflictFor };
  function validate(kind, value, identity) {
    const bucket = buckets.get(kind); let key;
    if (enabled && bucket.maxEntries && bucket.maxBytes) {
      try { key = fingerprint([fixedNow, kind, identity, value]); } catch { /* Original validator still decides. */ }
      if (typeof key === 'string' && Buffer.byteLength(key) <= Math.min(bucket.maxBytes, LIMITS.itemBytes)) {
        const old = bucket.entries.get(key);
        if (old) {
          bucket.entries.delete(key); bucket.entries.set(key, old); counts.hits++;
          return clone(old.value);
        }
      } else key = undefined;
    }
    counts.misses++; counts.checks++;
    const result = validators[kind](value, identity, fixedNow);
    if (!key || result === null) { counts.bypasses++; return result; }
    const bytes = Buffer.byteLength(key) + Buffer.byteLength(canonical(result));
    if (bytes > bucket.maxBytes || bytes > LIMITS.itemBytes) { counts.bypasses++; return result; }
    while (bucket.entries.size >= bucket.maxEntries || bucket.bytes + bytes > bucket.maxBytes) {
      const oldest = bucket.entries.keys().next().value;
      bucket.bytes -= bucket.entries.get(oldest).bytes; bucket.entries.delete(oldest); counts.evictions++;
    }
    bucket.entries.set(key, { value: clone(result), bytes }); bucket.bytes += bytes;
    return result;
  }
  return {
    normalizeConflict: (value, identity) => validate('normalizeConflict', value, identity),
    normalizeFact: (value, identity) => validate('normalizeFact', value, identity),
    conflictFor: (value, identity) => validate('conflictFor', value, identity),
    stats: () => ({ ...counts, entries: [...buckets.values()].reduce((sum, bucket) => sum + bucket.entries.size, 0),
      bytes: [...buckets.values()].reduce((sum, bucket) => sum + bucket.bytes, 0), maxEntries, maxBytes }),
  };
}

module.exports = { createPriceValidationCache, LIMITS };
