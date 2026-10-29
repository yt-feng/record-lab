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
    || !/^(?:4\.1(?:\.2)?|7\.1\.2)$/.test(raw.section || '') || !text(raw.text, 240)
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
const managerKey = item => JSON.stringify([item.name, item.startDate, item.endDate ?? null, item.role]);
const tableCounts = ['candidateRows', 'parsedRows', 'ignoredKnownRoleRows', 'excludedRows', 'outsidePeriodRows', 'duplicateRows'];
const count = value => Number.isInteger(value) && value >= 0 && value <= 10000;
function managerCoverageFor(raw, managers = null, periodEnd = null) {
  if (!raw || raw.schemaVersion !== 1 || raw.method !== 'bounded-report-manager-tables' || !source(raw.sourceUrl)
    || !date(raw.periodStart) || !date(raw.periodEnd) || raw.periodStart > raw.periodEnd || periodEnd && raw.periodEnd !== periodEnd
    || !['quarterly', 'interim', 'semiannual', 'annual'].includes(raw.kind) || !/^[a-f0-9]{64}$/.test(raw.sourceTextHash || '')
    || [...tableCounts, 'outputManagerCount'].some(key => !count(raw[key]))
    || !Array.isArray(raw.tables) || raw.tables.length > 30 || !Array.isArray(raw.managerKeys) || raw.managerKeys.length > 30
    || raw.managerKeys.some(key => typeof key !== 'string') || new Set(raw.managerKeys).size !== raw.managerKeys.length) return null;
  const month = Number(raw.periodEnd.slice(5, 7));
  const expectedStart = raw.kind === 'quarterly' ? `${raw.periodEnd.slice(0, 4)}-${String(Math.floor((month - 1) / 3) * 3 + 1).padStart(2, '0')}-01` : `${raw.periodEnd.slice(0, 4)}-01-01`;
  if (raw.periodStart !== expectedStart) return null;
  try {
    for (const key of raw.managerKeys) {
      const value = JSON.parse(key);
      if (!Array.isArray(value) || value.length !== 4 || !text(value[0], 100) || !date(value[1])
        || value[2] !== null && (!date(value[2]) || value[2] < value[1] || value[2] < raw.periodStart)
        || value[1] > raw.periodEnd || !text(value[3], 60)?.includes('基金经理') || value[3].includes('基金经理助理')) return null;
    }
  } catch { return null; }
  const tables = [], checks = [];
  for (const rawTable of raw.tables) {
    if (!rawTable || !/^(?:4\.1(?:\.2)?|7\.1\.2)$/.test(rawTable.section || '') || tableCounts.some(key => !count(rawTable[key]))) return null;
    const prior = rawTable.section.split('.').map(Number), next = /^\d+(?:\.\d+){0,2}$/.test(rawTable.endSection || '') ? rawTable.endSection.split('.').map(Number) : [];
    const different = next.findIndex((value, index) => value !== prior[index]);
    const forward = next.length > 0 && different >= 0 && (prior[different] === undefined || next[different] > prior[different]);
    const pages = page(rawTable.startPage) && page(rawTable.endPage) && rawTable.startPage <= rawTable.endPage;
    const heading = text(rawTable.heading, 240), ending = text(rawTable.endHeading, 240);
    const headings = heading?.replace(/\s/g, '').startsWith(rawTable.section) && heading.replace(/\s/g, '').includes('基金经理')
      && ending?.replace(/\s/g, '').startsWith(rawTable.endSection || '~');
    const ignored = Array.isArray(rawTable.ignoredRoles) ? rawTable.ignoredRoles.map(item => ({ name: text(item?.name, 100),
      role: text(item?.role, 100), page: page(item?.page) ? item.page : null, text: text(item?.text, 240) })) : [];
    const roles = ignored.length === rawTable.ignoredKnownRoleRows && ignored.every(item => item.name && item.role
      && /^[\u3400-\u9fff]{2,6}(?:[·•][\u3400-\u9fff]{1,6})?$/.test(item.name)
      && (['董事长', '公司总经理', '总经理', '投资总监'].includes(item.role)
        || (item.role.match(/基金经理/g) || []).length === 1 && item.role.includes('基金经理助理')
        && !/曾|历任|担任|毕业|加入|学历|学位|硕士|博士|学士|就职|从事/.test(item.role))
      && pages && item.page >= rawTable.startPage && item.page <= rawTable.endPage
      && item.text?.replace(/\s/g, '').startsWith(item.name) && item.text.replace(/\s/g, '').includes(item.role));
    const balanced = rawTable.candidateRows === rawTable.parsedRows + rawTable.ignoredKnownRoleRows + rawTable.excludedRows;
    checks.push(Boolean(forward && pages && headings && roles && balanced && rawTable.excludedRows === 0
      && rawTable.sectionBounded === true && rawTable.pageNumbersVerified === true && rawTable.pageLimitReached === false));
    tables.push({ section: rawTable.section, startPage: page(rawTable.startPage) ? rawTable.startPage : null,
      endPage: page(rawTable.endPage) ? rawTable.endPage : null, endSection: text(rawTable.endSection, 30), heading, endHeading: ending,
      sectionBounded: rawTable.sectionBounded === true, pageNumbersVerified: rawTable.pageNumbersVerified === true,
      pageLimitReached: rawTable.pageLimitReached === true, ignoredRoles: ignored,
      ...Object.fromEntries(tableCounts.map(key => [key, rawTable[key]])) });
  }
  let aligned = raw.outputManagerCount === raw.managerKeys.length;
  if (managers !== null) aligned = aligned && Array.isArray(managers)
    && managers.every(item => evidenceFor(item, raw.periodEnd) && item.evidence.sourceUrl === raw.sourceUrl)
    && JSON.stringify(managers.map(managerKey).sort()) === JSON.stringify([...raw.managerKeys].sort());
  const publishedAt = date(raw.publishedAt) ? raw.publishedAt : null;
  const parserVersion = Number.isInteger(raw.parserVersion) ? raw.parserVersion : 1;
  const extractionMethod = raw.extractionMethod || 'plain-text-manager-table';
  const geometryEvidence = extractionMethod === 'pdf-clipping-manager-table'
    ? require('./manager-cells').normalizeGeometryEvidence(raw.geometryEvidence, raw) : null;
  const methodValid = extractionMethod === 'plain-text-manager-table' || extractionMethod === 'pdf-clipping-manager-table' && geometryEvidence;
  const complete = Boolean(parserVersion === 2 && methodValid && publishedAt && publishedAt >= raw.periodEnd && raw.complete === true && aligned && raw.managerKeys.length && tables.length && checks.every(Boolean)
    && tableCounts.every(key => raw[key] === tables.reduce((total, item) => total + item[key], 0))
    && raw.candidateRows === raw.parsedRows + raw.ignoredKnownRoleRows
    && raw.parsedRows === raw.outputManagerCount + raw.outsidePeriodRows + raw.duplicateRows);
  return { schemaVersion: 1, parserVersion, extractionMethod, ...(geometryEvidence ? { geometryEvidence } : {}),
    sourceUrl: raw.sourceUrl, periodStart: raw.periodStart, periodEnd: raw.periodEnd, kind: raw.kind,
    publishedAt, sourceTextHash: raw.sourceTextHash, method: raw.method, tables,
    ...Object.fromEntries([...tableCounts, 'outputManagerCount'].map(key => [key, raw[key]])), managerKeys: [...raw.managerKeys].sort(),
    complete, status: complete ? 'complete' : raw.managerKeys.length || tables.length ? 'partial' : 'missing' };
}
function completeManagerCoverage(fund, periodEnd = fund?.selectedReport?.periodEnd) {
  const proof = managerCoverageFor(fund?.managerTableCoverage, null, periodEnd);
  if (!proof?.complete || fund.managerAttribution !== 'report' || fund.managerSourceUrl !== proof.sourceUrl) return false;
  const month = Number(periodEnd.slice(5, 7));
  const start = fund.selectedReport?.kind === 'quarterly'
    ? `${periodEnd.slice(0, 4)}-${String(Math.floor((month - 1) / 3) * 3 + 1).padStart(2, '0')}-01`
    : `${periodEnd.slice(0, 4)}-01-01`;
  const rows = verifiedReportManagers(fund.reportManagers, start, periodEnd);
  const expected = proof.managerKeys.filter(key => { const item = JSON.parse(key); return item[1] <= periodEnd && (!item[2] || item[2] >= start); }).sort();
  return rows.length > 0 && rows.length === fund.reportManagers.length && rows.every(item => item.evidence.sourceUrl === proof.sourceUrl)
    && JSON.stringify(rows.map(managerKey).sort()) === JSON.stringify(expected);
}
module.exports = { normalizeManagers, verifiedReportManagers, evidenceFor, overlaps, managerKey, managerCoverageFor, completeManagerCoverage };
