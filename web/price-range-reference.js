(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RecordPriceRangeReference = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const METHOD = 'stored-range-endpoints-exact-or-yahoo-binary32-eastmoney-v1';
  const PRICE_FIELDS = ['status', 'low', 'high', 'market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd',
    'coverageVersion', 'calendarId', 'sessionScope', 'completeness', 'observedCount', 'observedStart', 'observedEnd',
    'expectedSessionCount', 'expectedDateSetHash', 'observedDateSetHash', 'missingSessionCount', 'missingSessionDates',
    'unexpectedSessionCount', 'unexpectedSessionDates', 'rejectedRowCount', 'conflictingDateCount'];
  const samePrice = (a, b) => PRICE_FIELDS.every(key => JSON.stringify(a?.[key]) === JSON.stringify(b?.[key]));
  const instantMicros = value => BigInt(Date.parse(value)) * 1000n + BigInt((/\.(\d{1,6})/.exec(value)?.[1] || '').padEnd(6, '0').slice(3));
  function preferredFact(a, b) {
    const delta = instantMicros(a.fetchedAt) - instantMicros(b.fetchedAt);
    if (delta) return delta > 0n ? a : b;
    return JSON.stringify([a.sourceUrl, a.sourceUrls, a.fetchedAt]) <= JSON.stringify([b.sourceUrl, b.sourceUrls, b.fetchedAt]) ? a : b;
  }
  const keyFor = p => [p?.market, p?.stockCode, p?.currency, 'unadjusted', p?.rangeStart, p?.rangeEnd, p?.calendarId].join('|');
  function sourceIdentity(value) {
    try {
      const u = new URL(value);
      if (u.protocol !== 'https:' || u.username || u.password || u.hash) return null;
      if (['query1.finance.yahoo.com', 'query2.finance.yahoo.com'].includes(u.hostname)) {
        u.hostname = 'query1.finance.yahoo.com'; u.searchParams.delete('period2');
      }
      const pairs = [...u.searchParams].sort((a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));
      return u.origin + u.pathname + '?' + JSON.stringify(pairs);
    } catch { return null; }
  }
  const sources = f => [...new Set([f?.sourceUrl, ...(f?.sourceUrls || [])].map(sourceIdentity))].sort();
  function providerFor(fact, identity) {
    const p = identity, code = p.stockCode;
    let secid, symbol;
    if (p.market === 'HK' && /^\d{5}$/.test(code)) { secid = '116.' + code; symbol = String(Number(code)).padStart(4, '0') + '.HK'; }
    else if (p.market === 'CN' && /^\d{6}$/.test(code)) {
      if (code.startsWith('6')) { secid = '1.' + code; symbol = code + '.SS'; }
      else if (/^[03]/.test(code)) { secid = '0.' + code; symbol = code + '.SZ'; }
      else if (/^(4|8|92)/.test(code)) { secid = '0.' + code; symbol = code + '.BJ'; }
    }
    if (!secid) return null;
    const providers = [];
    for (const value of [fact.sourceUrl, ...(fact.sourceUrls || [])]) {
      try {
        const u = new URL(value), pairs = [...u.searchParams], q = Object.fromEntries(pairs);
        if (u.protocol !== 'https:' || u.port || u.username || u.password || u.hash || pairs.length !== Object.keys(q).length) return null;
        if (u.hostname === 'push2his.eastmoney.com' && u.pathname === '/api/qt/stock/kline/get'
          && Object.keys(q).sort().join('|') === ['secid', 'klt', 'fqt', 'beg', 'end', 'fields1', 'fields2'].sort().join('|')
          && q.secid === secid && q.klt === '101' && q.fqt === '0'
          && q.beg === p.rangeStart.replaceAll('-', '') && q.end === p.rangeEnd.replaceAll('-', '')
          && q.fields1 === 'f1,f2,f3,f4,f5,f6' && q.fields2 === 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61') providers.push('eastmoney');
        else if (['query1.finance.yahoo.com', 'query2.finance.yahoo.com'].includes(u.hostname)
          && u.pathname === '/v8/finance/chart/' + symbol
          && Object.keys(q).sort().join('|') === ['period1', 'period2', 'interval', 'events', 'includeAdjustedClose'].sort().join('|')
          && q.period1 === String(Date.parse(p.rangeStart + 'T00:00:00Z') / 1000)
          && /^\d{1,12}$/.test(q.period2) && Number(q.period2) >= Date.parse(p.rangeEnd + 'T00:00:00Z') / 1000 + 86400
          && Number(q.period2) <= Date.parse(fact.fetchedAt) / 1000 + 172800
          && q.interval === '1d' && q.events === 'splits' && q.includeAdjustedClose === 'false') providers.push('yahoo');
        else return null;
      } catch { return null; }
    }
    return providers.length && providers.every(p => p === providers[0]) ? providers[0] : null;
  }
  function completeShape(p, identity) {
    return p && keyFor(p) === keyFor(identity) && p.status === 'observed' && p.priceBasis === 'unadjusted'
      && p.coverageVersion === 2 && p.calendarId === 'exchange-sessions-2026-q2-q3-v1'
      && p.sessionScope === 'regular-trading-session' && p.completeness === 'complete'
      && Number.isFinite(p.low) && Number.isFinite(p.high) && p.low > 0 && p.high >= p.low
      && Number.isInteger(p.expectedSessionCount) && p.expectedSessionCount > 0 && p.observedCount === p.expectedSessionCount
      && ['missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount'].every(k => p[k] === 0)
      && /^[a-f0-9]{64}$/.test(p.expectedDateSetHash) && p.observedDateSetHash === p.expectedDateSetHash
      && Array.isArray(p.missingSessionDates) && !p.missingSessionDates.length
      && Array.isArray(p.unexpectedSessionDates) && !p.unexpectedSessionDates.length;
  }
  function selectFact(facts, identity) {
    if (!Array.isArray(facts) || facts.length < 2 || facts.length > 128
      || facts.some(f => !completeShape(f?.price, identity) || !Array.isArray(f.sourceUrls) || !f.sourceUrls.length)) return null;
    const eastmoney = facts.filter(f => providerFor(f, identity) === 'eastmoney');
    if (!eastmoney.length) return null;
    const anchor = eastmoney.reduce(preferredFact);
    const exact = f => f.price.low === anchor.price.low && f.price.high === anchor.price.high;
    // Never join two different Eastmoney values through the same float32 bin.
    if (!eastmoney.every(exact) || !facts.some(f => !exact(f))) return null;
    if (!facts.every(f => exact(f) || providerFor(f, identity) === 'yahoo'
      && f.price.low === Math.fround(anchor.price.low) && f.price.high === Math.fround(anchor.price.high))) return null;
    return anchor;
  }
  function member(fact, facts) {
    const identity = JSON.stringify(sources(fact));
    return facts.some(f => samePrice(fact.price, f.price) && JSON.stringify(sources(f)) === identity);
  }
  function effectiveReference(range) {
    try {
      const ref = range?.rangeReference;
      if (ref?.schemaVersion !== 1 || ref.kind !== 'stored-range-reference' || ref.method !== METHOD
        || ref.scope !== 'quarter-extrema-only' || ref.key !== keyFor(range) || ref.marketDataRequests !== 0
        || ref.dailySeriesReverified !== false || ref.originalResponsesReverified !== false) return null;
      const selected = selectFact(ref.facts, range);
      if (!selected || selected.factId !== ref.referenceFactId) return null;
      if (Object.hasOwn(range, 'globalPriceConflict')) {
        const witness = range.globalPriceConflict;
        if (witness?.schemaVersion !== 1 || witness.key !== ref.key || !Array.isArray(witness.facts) || witness.facts.length !== 2
          || witness.facts.some(f => !completeShape(f.price, range) || !member(f, ref.facts))) return null;
      }
      if (range.status === 'observed') {
        if (range.completeness === 'complete') {
          if (!member({ price: range, sourceUrl: range.sourceUrl, sourceUrls: range.sourceUrls || [range.sourceUrl] }, ref.facts)) return null;
        } else if (!Number.isFinite(range.low) || !Number.isFinite(range.high)
          || range.low < selected.price.low || range.high > selected.price.high) return null;
      }
      return { ...selected.price, sourceUrl: selected.sourceUrl, sourceUrls: [...selected.sourceUrls], fetchedAt: selected.fetchedAt,
        rangeReference: ref, referenceOnly: true };
    } catch { return null; }
  }
  return { METHOD, PRICE_FIELDS, keyFor, providerFor, selectFact, member, preferredFact, effectiveReference };
});
