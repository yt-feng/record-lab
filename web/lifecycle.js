(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RecordLabLifecycle = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const dateText = '20\\d{2}(?:年\\d{1,2}月\\d{1,2}日|[-/]\\d{1,2}[-/]\\d{1,2})';
  const compact = value => value.replace(/\s+/g, '');
  function iso(value) {
    if (typeof value !== 'string') return null;
    const match = /^(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?$/.exec(value);
    if (!match) return null;
    const result = `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
    const date = new Date(result);
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === result ? result : null;
  }
  const validDate = value => typeof value === 'string' && iso(value) === value;
  function publicSource(value) {
    if (typeof value !== 'string' || value.length > 4096 || /[\u0000-\u0020]/.test(value)) return false;
    try {
      const url = new URL(value), host = url.hostname;
      if (url.protocol !== 'https:' || url.username || url.password || url.hash || !host.includes('.')
        || host.endsWith('.local') || host.endsWith('.localhost') || host.startsWith('[')
        || /^(?:0|10|127)\.|^192\.168\.|^169\.254\.|^172\.(?:1[6-9]|2\d|3[01])\./.test(host)) return false;
      return ![...url.searchParams.keys()].some(key => /token|secret|password|signature|authorization|api.?key/i.test(key));
    } catch { return false; }
  }
  const datePatterns = {
    lastOperationDate: [`(?:本基金(?:的)?)?(?:最后|最终)运作日(?:为|是|[:：])?(${dateText})`,
      `(${dateText})(?:为|是)本基金(?:的)?(?:最后|最终)运作日`],
    terminationDate: [`(?:本基金(?:的)?)?(?:《基金合同》|基金合同)(?:已)?(?:自|于)(${dateText})(?:起)?(?:已|正式|自动)?终止`,
      `(?:本基金(?:的)?)?(?:《基金合同》|基金合同)终止日(?:为|是|[:：])(${dateText})`],
    liquidationStartDate: [`自(${dateText})起[，,]?(?:本基金)?(?:已)?进入(?:基金财产)?清算(?:程序|期)`],
    finalLiquidationEnd: [`自${dateText}起?至(${dateText})止?为(?:本基金)?清算报告期`,
      `清算报告期(?:结束|截止)日[（(:：]?(${dateText})`],
  };
  function confirmed(quote) {
    const text = compact(quote);
    const pattern = new RegExp(`(?:本基金(?:的)?(?:《基金合同》|基金合同)|《基金合同》)(?:已)?(?:(?:自|于)${dateText}(?:起)?)?(?:正式|自动)?终止(?=[。；;，,]|$)`, 'g');
    return [...text.matchAll(pattern)].some(match => {
      const prior = text.slice(0, match.index).split(/[。；;！？!?]/).at(-1);
      const after = text.slice(match.index + match[0].length).split(/[。；;！？!?]/)[0];
      return prior.length <= 260 && !/若|如果|如届时|一旦|可能|拟|计划|将|应当|需满足|未终止|不终止/.test(prior)
        && !/可能|拟|计划|将|尚未|尚待|未发生|待确认/.test(after);
    });
  }
  function normalizeLifecycleEvidence(raw, { code, periodEnd, asOf } = {}) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.reason !== 'TERMINATED_BEFORE_PERIOD'
      || !/^\d{6}$/.test(code || '') || raw.fundCode !== code || !validDate(periodEnd) || raw.periodEnd !== periodEnd
      || !validDate(asOf) || !validDate(raw.publishedAt) || raw.publishedAt > asOf || !publicSource(raw.sourceUrl)
      || raw.terminationConfirmed !== true || raw.scope !== 'quarter-end-applicability-only'
      || !['termination', 'final-liquidation'].includes(raw.eventType)
      || !['terminationDate', 'lastOperationDate'].includes(raw.dateBasis)
      || raw.parseMethod !== 'pypdf' || raw.pageNumbersVerified !== true || !/^[a-f0-9]{64}$/.test(raw.sourceTextHash || '')
      || !Number.isInteger(raw.totalPages) || raw.totalPages < 1 || raw.totalPages > 20
      || !Array.isArray(raw.identityFundCodes) || !raw.identityFundCodes.length || raw.identityFundCodes.length > 64
      || raw.identityFundCodes.some(value => typeof value !== 'string' || !/^\d{6}$/.test(value))
      || new Set(raw.identityFundCodes).size !== raw.identityFundCodes.length || !raw.identityFundCodes.includes(code)
      || !Array.isArray(raw.evidence) || raw.evidence.length < 3 || raw.evidence.length > 16) return null;
    const values = {};
    for (const field of Object.keys(datePatterns)) {
      const value = raw[field] ?? null;
      if (value !== null && (!validDate(value) || value > raw.publishedAt)) return null;
      values[field] = value;
    }
    if (!validDate(raw.date) || raw.date !== values[raw.dateBasis] || raw.date >= periodEnd
      || (values.lastOperationDate && values.lastOperationDate >= periodEnd)
      || (values.terminationDate && raw.dateBasis !== 'terminationDate')
      || (values.liquidationStartDate && values.liquidationStartDate < raw.date)
      || (values.finalLiquidationEnd && (raw.eventType !== 'final-liquidation'
        || values.finalLiquidationEnd < (values.liquidationStartDate || raw.date)
        || raw.finalLiquidationEndBasis !== 'liquidation-report-period-end'))
      || (!values.finalLiquidationEnd && raw.finalLiquidationEndBasis != null)) return null;
    const fields = new Set(['terminationConfirmed', 'identityFundCodes', ...Object.keys(datePatterns)]);
    const seenFields = new Set(), identityCodes = new Set(), citations = [];
    for (const item of raw.evidence) {
      if (!item || !fields.has(item.field) || !Number.isInteger(item.page) || item.page < 1 || item.page > raw.totalPages
        || typeof item.quote !== 'string' || !item.quote.length || item.quote.length > 420) return null;
      const text = compact(item.quote);
      if (item.field === 'terminationConfirmed') { if (!confirmed(item.quote)) return null; }
      else if (item.field === 'identityFundCodes') {
        const matches = [...text.matchAll(/(?:基金主代码|基金(?:份额|交易)?代码|基金份额(?:交易)?代码|下属(?:分级|分类)基金的交易代码)[:：]?((?:\d{6}){1,8})(?!\d)/g)];
        if (!matches.length) return null;
        for (const match of matches) for (const identity of match[1].match(/\d{6}/g)) identityCodes.add(identity);
      } else {
        if (!values[item.field] || /若|如果|可能|拟|计划|将/.test(text)) return null;
        const dates = new Set(datePatterns[item.field].flatMap(pattern =>
          [...text.matchAll(new RegExp(pattern, 'g'))].map(match => iso(match[1]))));
        if (dates.size !== 1 || !dates.has(values[item.field])) return null;
      }
      seenFields.add(item.field);
      citations.push({ field: item.field, page: item.page, quote: item.quote });
    }
    if (!['terminationConfirmed', 'identityFundCodes', raw.dateBasis].every(field => seenFields.has(field))
      || Object.keys(values).some(field => values[field] && !seenFields.has(field))
      || !citations.some(item => item.field === 'terminationConfirmed' && item.page === raw.page && item.quote === raw.quote)
      || identityCodes.size !== raw.identityFundCodes.length || raw.identityFundCodes.some(value => !identityCodes.has(value))) return null;
    return { reason: raw.reason, fundCode: code, periodEnd, date: raw.date, dateBasis: raw.dateBasis,
      sourceUrl: raw.sourceUrl, publishedAt: raw.publishedAt, eventType: raw.eventType, terminationConfirmed: true,
      identityFundCodes: [...identityCodes].sort(), ...values,
      finalLiquidationEndBasis: values.finalLiquidationEnd ? 'liquidation-report-period-end' : null,
      page: raw.page, quote: raw.quote, evidence: citations, sourceTextHash: raw.sourceTextHash,
      parseMethod: 'pypdf', pageNumbersVerified: true, totalPages: raw.totalPages, scope: raw.scope };
  }
  return { normalizeLifecycleEvidence };
});
