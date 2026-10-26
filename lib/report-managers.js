'use strict';

// Report manager proof is separate from current profile names and inferred
// tenure overlap. No current profile can manufacture a historical PDF citation.
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value + 'T00:00:00Z'))
  && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
const text = (value, max) => typeof value === 'string' && value.trim() ? value.trim().slice(0, max) : null;
const page = value => Number.isInteger(value) && value > 0 && value <= 10000;
function source(value) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const url = new URL(value), host = url.hostname.toLowerCase();
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.hash
      && host.includes('.') && !host.endsWith('.localhost') && !host.endsWith('.local') && !host.startsWith('[')
      && !/^(?:0|10|127|169\.254|192\.168)\./.test(host) && !/^172\.(?:1[6-9]|2\d|3[01])\./.test(host)
      && ![...url.searchParams.keys()].some(key => /^(?:token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(key));
  } catch { return false; }
}
function evidenceFor(manager, periodEnd = null) {
  const raw = manager?.evidence;
  if (!raw || raw.method !== 'explicit-report-manager-table' || !source(raw.sourceUrl)
    || !page(raw.page) || !page(raw.tablePage) || raw.tablePage > raw.page
    || !/^4\.1(?:\.2)?$/.test(raw.section || '') || !text(raw.text, 240)
    || !date(manager.periodStart) || !date(manager.periodEnd) || manager.periodStart > manager.periodEnd
    || raw.periodEnd !== manager.periodEnd || periodEnd && raw.periodEnd !== periodEnd
    || !date(manager.startDate) || manager.endDate != null && (!date(manager.endDate) || manager.endDate < manager.startDate)
    || !text(manager.role, 60)?.includes('基金经理') || manager.role.includes('基金经理助理')
    || !text(manager.name, 100) || !raw.text.replace(/\s/g, '').startsWith(manager.name.replace(/\s/g, ''))
    || !Array.isArray(raw.pages) || !raw.pages.length || raw.pages.length > 4
    || raw.pages.some((value, index) => !page(value) || value !== raw.page + index)) return null;
  return { sourceUrl: raw.sourceUrl, page: raw.page, pages: [...raw.pages], tablePage: raw.tablePage,
    section: raw.section, text: text(raw.text, 240), method: raw.method, periodEnd: raw.periodEnd };
}
function normalizeManagers(managers, periodEnd = null) {
  return (Array.isArray(managers) ? managers : []).filter(item => item && text(item.name, 100)).slice(0, 50).map(item => {
    const value = { name: text(item.name, 100), startDate: date(item.startDate) ? item.startDate : null,
      endDate: date(item.endDate) ? item.endDate : null };
    const evidence = evidenceFor(item, periodEnd);
    if (evidence) Object.assign(value, { role: text(item.role, 60), periodStart: item.periodStart, periodEnd: item.periodEnd,
      activeAtPeriodEnd: !value.endDate || value.endDate >= item.periodEnd, evidence });
    return value;
  });
}
function overlaps(manager, start, end) {
  return date(manager?.startDate) && manager.startDate <= end
    && (!manager.endDate || date(manager.endDate) && manager.endDate >= start && manager.endDate >= manager.startDate);
}
function verifiedReportManagers(managers, start, end) {
  return normalizeManagers(managers, end).filter(item => evidenceFor(item, end) && overlaps(item, start, end));
}
module.exports = { normalizeManagers, verifiedReportManagers, evidenceFor, overlaps };
