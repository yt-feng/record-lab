'use strict';
const crypto = require('node:crypto');
const { priceRange, validDate } = require('./fund-analysis');
const { validCompletePrice } = require('./price-coverage');
const stamp = x => typeof x === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(x) && Number.isFinite(Date.parse(x));
const key = x => x && ['CN', 'HK', 'US'].includes(x.market) && typeof x.stockCode === 'string'
  && ({ US: /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/, CN: /^\d{6}$/, HK: /^\d{5}$/ }[x.market]).test(x.stockCode) ? `${x.market}:${x.stockCode}` : null;
const source = value => { try { const u = new URL(value); return typeof value === 'string' && !/[\u0000-\u001f]/.test(value)
  && value.length <= 4096 && u.protocol === 'https:' && !u.username && !u.password && !u.hash
  && /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(u.hostname) && u.hostname.includes('.')
  && /\.[a-z][a-z0-9-]*$/.test(u.hostname) && !/\.(localhost|local|internal)$/.test(u.hostname)
  && ![...u.searchParams.keys()].some(k => /^(token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(k)); } catch { return false; } };
const hash = keys => crypto.createHash('sha256').update(keys.join('|')).digest('hex');
function reportIdentity(report, period) {
  if (!report || report.periodEnd !== period || !validDate(period) || !validDate(report.publishedAt)
    || report.publishedAt < period || !source(report.sourceUrl)) return null;
  const value = Object.fromEntries(['periodEnd', 'publishedAt', 'sourceUrl', 'holdingsSourceUrl', 'reportPdfSourceUrl'].map(k => [k, report[k] ?? null]));
  return Object.entries(value).some(([k, v]) => k.endsWith('Url') && v !== null && !source(v)) ? null : value;
}
function cleanBinding(value, phase, code) {
  if (!value || value.schemaVersion !== 1 || value.code !== code || !/^\d{6}$/.test(code)
    || value.targetPeriodEnd !== phase.targetPeriodEnd || value.baselinePeriodEnd !== phase.baselinePeriodEnd
    || !validDate(phase.targetPeriodEnd) || !validDate(phase.baselinePeriodEnd)) return null;
  const start = `${phase.targetPeriodEnd.slice(0, 4)}-${String(Math.floor((Number(phase.targetPeriodEnd.slice(5, 7)) - 1) / 3) * 3 + 1).padStart(2, '0')}-01`;
  if (new Date(Date.parse(start) - 86400000).toISOString().slice(0, 10) !== phase.baselinePeriodEnd) return null;
  const selected = reportIdentity(value.selectedReport, phase.targetPeriodEnd);
  const previous = value.previousReport === null ? null : reportIdentity(value.previousReport, phase.baselinePeriodEnd);
  if (!selected || value.previousReport !== null && !previous || !Array.isArray(value.stockKeys)
    || !value.stockKeys.length || value.stockKeys.length > 20000) return null;
  if (value.stockKeys.some(k => typeof k !== 'string' || key({ market: k.split(':')[0], stockCode: k.split(':')[1] }) !== k)) return null;
  const keys = [...new Set(value.stockKeys)].sort();
  if (JSON.stringify(keys) !== JSON.stringify(value.stockKeys) || value.holdingsIdentity !== hash(keys)) return null;
  if (value.companyCode !== null && (typeof value.companyCode !== 'string' || !/^[A-Za-z0-9_-]{1,40}$/.test(value.companyCode))) return null;
  return { schemaVersion: 1, code, companyCode: value.companyCode, targetPeriodEnd: phase.targetPeriodEnd,
    baselinePeriodEnd: phase.baselinePeriodEnd, selectedReport: selected, previousReport: previous,
    stockKeys: keys, holdingsIdentity: value.holdingsIdentity };
}
function bindingFor(fund, phase) {
  if (!fund || !['ok', 'no-equities'].includes(fund.status) || !Array.isArray(fund.selectedReport?.holdings)) return null;
  const selected = reportIdentity(fund.selectedReport, phase.targetPeriodEnd);
  const previous = fund.previousReport ? reportIdentity(fund.previousReport, phase.baselinePeriodEnd) : null;
  if (!selected || fund.previousReport && !previous) return null;
  const rows = [...fund.selectedReport.holdings, ...(previous ? fund.previousReport.holdings || [] : [])];
  if (rows.some(row => !key(row))) return null;
  const keys = [...new Set(rows.map(key))].sort();
  return cleanBinding({ schemaVersion: 1, code: fund.code, companyCode: fund.companyId === 'unknown' ? null : fund.companyId ?? null,
    targetPeriodEnd: phase.targetPeriodEnd, baselinePeriodEnd: phase.baselinePeriodEnd,
    selectedReport: selected, previousReport: previous, stockKeys: keys, holdingsIdentity: hash(keys) }, phase, fund.code);
}
function normalizePatch(raw, item, bars, phase, retrievedAt, plannedAt) {
  const binding = cleanBinding(item.priceOnly, phase, item.code);
  const target = Array.isArray(item.priceTargets) ? item.priceTargets.map(key) : [];
  const valid = binding && binding.companyCode === (item.companyCode || item.companyId || null)
    && target.length && target.every(k => k && binding.stockKeys.includes(k))
    && new Set(target).size === target.length && raw
    && JSON.stringify(cleanBinding(raw.binding, phase, item.code)) === JSON.stringify(binding);
  if (!valid) return { schemaVersion: 1, code: item.code, priceOnly: true, binding: null,
    attemptedStockKeys: [], attemptedAt: null, prices: [], errorCode: 'PRICE_BINDING_INVALID' };
  const attempted = Array.isArray(raw.attemptedStockKeys) && raw.attemptedStockKeys.every(k => target.includes(k))
    ? [...new Set(raw.attemptedStockKeys)].sort() : [];
  const started = attempted.length > 0 && stamp(raw.attemptedAt) && Date.parse(raw.attemptedAt) <= Date.parse(retrievedAt)
    && (!plannedAt || Date.parse(raw.attemptedAt) >= Date.parse(plannedAt));
  const interval = { start: new Date(Date.parse(phase.baselinePeriodEnd) + 86400000).toISOString().slice(0, 10), end: phase.targetPeriodEnd };
  const prices = [];
  for (const k of started ? attempted : []) {
    if (!bars[k]) continue;
    const [market, stockCode] = k.split(':');
    const range = priceRange({ market, stockCode, currency: { CN: 'CNY', HK: 'HKD', US: 'USD' }[market] }, bars[k], interval);
    if (range.status !== 'observed') continue;
    const fetchedAt = stamp(bars[k].retrievedAt) ? bars[k].retrievedAt : retrievedAt;
    if (Date.parse(fetchedAt) > Date.parse(retrievedAt) || Date.parse(fetchedAt) < Date.parse(raw.attemptedAt)) continue;
    prices.push({ market, stockCode, priceRange: { ...range, fetchedAt, dataStatus: 'fresh' } });
  }
  return { schemaVersion: 1, code: item.code, priceOnly: true, binding,
    attemptedStockKeys: started ? attempted : [], attemptedAt: started ? raw.attemptedAt : null, prices,
    errorCode: started ? null : 'PRICE_BUDGET_REACHED' };
}
function validPatch(patch, phase, now) {
  const binding = cleanBinding(patch?.binding, phase, patch?.code);
  return Boolean(patch?.schemaVersion === 1 && patch.priceOnly === true && binding
    && JSON.stringify(binding) === JSON.stringify(patch.binding) && Array.isArray(patch.attemptedStockKeys)
    && patch.attemptedStockKeys.length > 0 && patch.attemptedStockKeys.every(k => binding.stockKeys.includes(k))
    && new Set(patch.attemptedStockKeys).size === patch.attemptedStockKeys.length
    && stamp(patch.attemptedAt) && Date.parse(patch.attemptedAt) <= now.getTime() + 300000
    && Array.isArray(patch.prices) && patch.prices.length <= patch.attemptedStockKeys.length
    && new Set(patch.prices.map(key)).size === patch.prices.length
    && patch.prices.every(row => patch.attemptedStockKeys.includes(key(row)) && stamp(row.priceRange?.fetchedAt)
      && Date.parse(row.priceRange.fetchedAt) >= Date.parse(patch.attemptedAt)));
}
function canReplacePrice(old, fresh, row, phase) {
  const start = new Date(Date.parse(phase.baselinePeriodEnd) + 86400000).toISOString().slice(0, 10);
  const complete = value => validCompletePrice(value, row.market, row.stockCode, start, phase.targetPeriodEnd);
  if (complete(old) && !complete(fresh)) return false;
  if (stamp(old?.fetchedAt) && Date.parse(old.fetchedAt) > Date.parse(fresh?.fetchedAt)) return false;
  return old?.status !== 'observed' || complete(fresh) || fresh.observedCount >= old.observedCount;
}
module.exports = { key, stamp, cleanBinding, bindingFor, normalizePatch, validPatch, canReplacePrice };
