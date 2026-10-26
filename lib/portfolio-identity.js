'use strict';

const METHOD = 'pdf-explicit-fund-code-rows-v1';
const LABELS = { mainCode: ['基金主代码'], shareClassCodes: ['下属分级基金的交易代码', '下属分类基金的交易代码'] };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const page = value => Number.isInteger(value) && value > 0 && value <= 10000;
const stamp = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && date(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
const compact = value => typeof value === 'string' ? value.replace(/\s+/g, '') : '';
const spaced = value => [...value].join('\\s*');
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
function source(value) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const url = new URL(value), host = url.hostname;
    return ['http:', 'https:'].includes(url.protocol) && host.includes('.') && !host.includes(':')
      && !/^\d+(?:\.\d+){3}$/.test(host) && !/\.(?:local|localhost)$/.test(host) && !url.username && !url.password && !url.hash
      && ![...url.searchParams.keys()].some(key => /token|secret|password|signature|authorization|credential|key/i.test(key));
  } catch { return false; }
}
function period(text) {
  const match = /^(20\d{2})年(?:第([1-4一二三四])季度|(中期|半年度|年度))报告$/.exec(compact(text));
  if (!match) return null;
  if (match[2]) {
    const quarter = /\d/.test(match[2]) ? Number(match[2]) : '一二三四'.indexOf(match[2]) + 1;
    return ['quarterly', `${match[1]}-${['03-31', '06-30', '09-30', '12-31'][quarter - 1]}`];
  }
  return [match[3] === '年度' ? 'annual' : 'semiannual', `${match[1]}-${match[3] === '年度' ? '12-31' : '06-30'}`];
}
function rowCodes(text, field) {
  if (typeof text !== 'string' || !text.length || text.length > 350 || !LABELS[field]) return null;
  const match = new RegExp('^[ \\t|]*(?:' + LABELS[field].map(spaced).join('|') + ')[ \\t|：:]*\\s*(\\d{6}(?:[\\s|]+\\d{6})*)[\\s|]*$').exec(text);
  if (!match) return null;
  const codes = match[1].match(/\d{6}/g);
  return codes.length <= 20 && new Set(codes).size === codes.length && (field !== 'mainCode' || codes.length === 1) ? codes : null;
}

function normalizePortfolioIdentity(raw, report = null, now = new Date()) {
  const observedNow = now instanceof Date ? now.getTime() : Date.parse(now);
  if (!Number.isFinite(observedNow) || !object(raw) || raw.schemaVersion !== 1 || raw.method !== METHOD
    || raw.scope !== 'explicitly-listed-fund-codes' || raw.shareClassUniverseComplete !== false || raw.shareClassSpecificMetricsProvided !== false
    || !source(raw.sourceUrl) || !date(raw.periodEnd) || !date(raw.publishedAt) || raw.publishedAt < raw.periodEnd
    || !hash(raw.pdfSha256) || !hash(raw.sourceTextHash) || raw.textHashScope !== 'provided-extracted-pages'
    || !stamp(raw.retrievedAt) || Date.parse(raw.retrievedAt) > observedNow + 300000 || raw.retrievedAt.slice(0, 10) < raw.publishedAt) return null;
  if (report && ['sourceUrl', 'periodEnd', 'publishedAt', 'kind'].some(key => raw[key] !== report[key])) return null;
  const title = raw.reportPeriodEvidence, table = raw.identitySection;
  if (!object(title) || !page(title.page) || typeof title.text !== 'string' || title.text.length > 80
    || !same(period(title.text), [raw.kind, raw.periodEnd]) || !object(table)) return null;
  const pages = table.pages;
  if (!Array.isArray(pages) || pages.length < 1 || pages.length > 5 || !pages.every(page)
    || !pages.every((value, index) => value === pages[0] + index) || table.startPage !== pages[0] || table.endPage !== pages.at(-1)
    || table.pageNumbersVerified !== true || table.sectionBounded !== true
    || typeof table.heading !== 'string' || table.heading.length > 160 || typeof table.endHeading !== 'string' || table.endHeading.length > 160) return null;
  if (!(compact(table.heading) === '§2基金产品概况' && /^§3[^\d]{2,100}$/.test(compact(table.endHeading))
    || compact(table.heading) === '2.1基金基本情况' && /^2\.2[^\d]{2,100}$/.test(compact(table.endHeading)))) return null;
  if (!Array.isArray(raw.rows) || raw.rows.length < 1 || raw.rows.length > 2) return null;
  const values = new Map(), rows = [];
  for (const row of raw.rows) {
    if (!object(row) || !Object.hasOwn(LABELS, row.field) || values.has(row.field)) return null;
    const codes = rowCodes(row.text, row.field), rp = row.pages;
    if (!codes || !same(codes, row.codes) || !Array.isArray(rp) || !rp.length || row.page !== rp[0]
      || !rp.every((value, index) => page(value) && pages.includes(value) && value === rp[0] + index)) return null;
    values.set(row.field, codes);
    rows.push({ field: row.field, text: row.text, codes, page: rp[0], pages: [...rp] });
  }
  if (!values.has('mainCode')) return null;
  const main = values.get('mainCode')[0], classes = values.get('shareClassCodes') || [];
  if (classes.length && !classes.includes(main) || raw.sourcePortfolioCode !== main || !same(raw.shareClassCodes, classes)) return null;
  if (report && ['fundCode', 'sourceFundCode'].some(key => report[key] != null && ![main, ...classes].includes(report[key]))) return null;
  return { schemaVersion: 1, method: METHOD, scope: raw.scope,
    ...Object.fromEntries(['sourceUrl', 'periodEnd', 'publishedAt', 'kind', 'retrievedAt', 'pdfSha256', 'sourceTextHash', 'textHashScope'].map(key => [key, raw[key]])),
    reportPeriodEvidence: { text: title.text, page: title.page },
    identitySection: Object.fromEntries(['heading', 'endHeading', 'startPage', 'endPage', 'pages', 'sectionBounded', 'pageNumbersVerified'].map(key => [key, table[key]])),
    rows, sourcePortfolioCode: main, shareClassCodes: classes, eligibleForShareClassExpansion: classes.length > 0,
    shareClassUniverseComplete: false, shareClassSpecificMetricsProvided: false };
}

// An empty extraction cannot refresh or erase an earlier observation of the
// same report. Independent report URLs keep independent identity evidence.
function retainPortfolioIdentity(report, previous) {
  if (!report || !previous || normalizePortfolioIdentity(report.portfolioIdentityEvidence, report)) return false;
  const proof = normalizePortfolioIdentity(previous.portfolioIdentityEvidence, report);
  if (!proof) return false;
  report.portfolioIdentityEvidence = proof;
  return true;
}

module.exports = { normalizePortfolioIdentity, retainPortfolioIdentity, METHOD };
