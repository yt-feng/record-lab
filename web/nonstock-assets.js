(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RecordLabNonstockAssets = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const TYPE = 'exhaustive-nonstock-assets', METHOD = 'two-closed-nonstock-asset-tables';
  const MONEY = '(?:[-—－–]|(?:0|[1-9]\\d{0,17}|[1-9]\\d{0,2}(?:,\\d{3}){1,5})(?:\\.\\d{1,2})?)';
  const RATIO = '(?:[-—－–]|(?:0|[1-9]\\d{0,2})(?:\\.\\d{1,2})?)';
  const PERIOD = /^(\d{4})年(?:第([1-4一二三四])季度|(中期|半年度|年度))报告$/;
  const SECTION = /^\s*(?<heading>(?<number>\d{1,2}\.\d{1,2}(?:\.\d{1,2})?|§\s*\d{1,2})\s+[^\n]{2,120})\s*$/;
  const ASSET_TITLE = /^(\d{1,2}\.1)(?:报告)?期末基金资产组合情况$/;
  const OTHER_TITLE = /^(\d{1,2}\.\d{1,2}\.\d{1,2})其他(?:各项)?资产构成$/;
  const OTHER_NAMES = ['存出保证金', '应收证券清算款', '应收利息', '应收申购款', '其他应收款', '待摊费用', '其他'];
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const compact = value => value.replace(/\s+/g, '');
  const esc = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const phrase = value => [...value].map(esc).join('\\s*');
  const field = (name, pattern = MONEY) => `(?<${name}>${pattern})`;
  const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value + 'T00:00:00Z')) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  function source(value) {
    if (typeof value !== 'string' || value.length > 2048) return false;
    try {
      const url = new URL(value), host = url.hostname.toLowerCase();
      return ['http:', 'https:'].includes(url.protocol) && host.includes('.') && !url.username && !url.password && !url.hash
        && !host.includes(':') && !/^\d+(?:\.\d+){3}$/.test(host) && !host.endsWith('.local') && !host.endsWith('.localhost')
        && ![...url.searchParams.keys()].some(key => /^(?:token|key|secret|password|signature|authorization|access_token|api_key)$/i.test(key));
    } catch { return false; }
  }
  function period(value) {
    const match = typeof value === 'string' && compact(value).match(PERIOD);
    if (!match || Number(match[1]) < 1900) return null;
    if (match[2]) {
      const quarter = '一二三四'.includes(match[2]) ? '一二三四'.indexOf(match[2]) + 1 : Number(match[2]);
      return ['quarterly', `${match[1]}-${['03-31', '06-30', '09-30', '12-31'][quarter - 1]}`];
    }
    return match[3] === '年度' ? ['annual', match[1] + '-12-31'] : ['semiannual', match[1] + '-06-30'];
  }
  function cents(value, ratio = false) {
    if (typeof value !== 'string' || !new RegExp(`^(?:${ratio ? RATIO : MONEY})$`).test(value)) return null;
    if (/^[-—－–]$/.test(value)) return 0n;
    const [whole, decimal = ''] = value.replace(/,/g, '').split('.');
    const amount = BigInt(whole) * 100n + BigInt(decimal.padEnd(2, '0'));
    return amount <= (ratio ? 10000n : 10n ** 20n) ? amount : null;
  }
  const decimal = amount => `${amount / 100n}.${String(amount % 100n).padStart(2, '0')}`;
  const roundedWeight = (amount, total) => (2n * amount * 10000n + total) / (2n * total);
  const sum = values => values.reduce((total, value) => total + value, 0n);
  const bodyMatch = (table, body) => new RegExp('^\\s*' + esc(table.heading) + '\\s*' + body + '\\s*' + esc(table.endHeading) + '\\s*$', 'u').exec(table.text)?.groups;
  function assetValues(table) {
    const unit = '(?:' + phrase('金额单位：人民币元') + '\\s*)?';
    const header = phrase('序号') + '\\s*' + phrase('项目') + '\\s*' + phrase('金额') + '\\s*(?:[（(]\\s*元\\s*[）)])?\\s*'
      + phrase('占基金总资产的比例') + '\\s*[（(]\\s*[%％]\\s*[）)]\\s*';
    const row = (prefix, label, key) => prefix + '\\s*' + phrase(label) + '\\s+' + field('a' + key) + '\\s+' + field('w' + key, RATIO) + '\\s*';
    let body = unit + header + row('1', '固定收益投资', '1');
    body += row('其中\\s*[：:]', '债券', 'bond') + row('(?:其中\\s*[：:]\\s*)?', '资产支持证券', 'abs');
    body += row('2', '买入返售金融资产', '2') + row('其中\\s*[：:]', '买断式回购的买入返售金融资产', 'repo');
    body += row('3', '银行存款和结算备付金合计', '3');
    body += '4\\s*(?:' + phrase('其他资产') + '|' + phrase('其他各项资产') + ')\\s+' + field('a4') + '\\s+' + field('w4', RATIO) + '\\s*';
    body += row('5', '合计', 'total') + '(?:' + phrase('注：由于四舍五入的原因，分项之和与合计项可能存在尾差。') + ')?';
    const match = bodyMatch(table, body), plain = compact(table.text);
    if (!match || !(/金额[（(]元[）)]/.test(plain) || plain.includes('金额单位：人民币元'))) return null;
    const keys = ['1', '2', '3', '4', 'bond', 'abs', 'repo', 'total'];
    const amounts = Object.fromEntries(keys.map(key => [key, cents(match['a' + key])]));
    const weights = Object.fromEntries(keys.map(key => [key, cents(match['w' + key], true)]));
    if ([...Object.values(amounts), ...Object.values(weights)].includes(null) || amounts.total <= 0n
      || sum(['1', '2', '3', '4'].map(key => amounts[key])) !== amounts.total || weights.total !== 10000n
      || amounts.bond + amounts.abs !== amounts['1'] || amounts.repo > amounts['2']) return null;
    if (keys.some(key => weights[key] !== roundedWeight(amounts[key], amounts.total)
      || /^[-—－–]$/.test(match['w' + key]) && amounts[key] !== 0n)) return null;
    const delta = sum(['1', '2', '3', '4'].map(key => weights[key])) - 10000n;
    if ((delta < 0n ? -delta : delta) * 2n > 4n) return null;
    const rows = ['固定收益投资', '买入返售金融资产', '银行存款和结算备付金合计', '其他资产'].map((name, i) =>
      ({ rank: i + 1, name, amount: decimal(amounts[String(i + 1)]), weightPct: decimal(weights[String(i + 1)]) }));
    const subrows = [[1, '债券', 'bond'], [1, '资产支持证券', 'abs'], [2, '买断式回购的买入返售金融资产', 'repo']]
      .map(([parentRank, name, key]) => ({ parentRank, name, amount: decimal(amounts[key]), weightPct: decimal(weights[key]) }));
    return { rows, subrows, total: { rank: 5, name: '合计', amount: decimal(amounts.total), weightPct: '100.00' } };
  }
  function otherValues(table) {
    let body = phrase('序号') + '\\s*' + phrase('名称') + '\\s*' + phrase('金额') + '\\s*[（(]\\s*元\\s*[）)]\\s*';
    [...OTHER_NAMES, '合计'].forEach((name, index) => { body += String(index + 1) + '\\s*' + phrase(name) + '\\s+' + field('a' + (index + 1)) + '\\s*'; });
    const match = bodyMatch(table, body);
    if (!match) return null;
    const amounts = Array.from({ length: 8 }, (_, index) => cents(match['a' + (index + 1)]));
    if (amounts.includes(null) || amounts[6] !== 0n || sum(amounts.slice(0, 7)) !== amounts[7]) return null;
    return { rows: OTHER_NAMES.map((name, index) => ({ rank: index + 1, name, amount: decimal(amounts[index]) })),
      total: { rank: 8, name: '合计', amount: decimal(amounts[7]) } };
  }
  function same(a, b) {
    if (typeof a !== typeof b) return false;
    if (Array.isArray(b)) return Array.isArray(a) && a.length === b.length && b.every((item, i) => same(a[i], item));
    if (object(b)) return object(a) && same(Object.keys(a).sort(), Object.keys(b).sort()) && Object.keys(b).every(key => same(a[key], b[key]));
    return a === b;
  }
  const number = value => typeof value === 'string' && /^\d{1,2}(?:\.\d{1,2}){0,2}$/.test(value) ? value.split('.').map(Number) : null;
  function table(raw, other = false) {
    if (!object(raw) || raw.sectionBounded !== true || raw.pageNumbersVerified !== true) return null;
    const { startPage: start, endPage: end, pageNumbers: numbers, text, heading, endHeading: ending } = raw;
    if (!Number.isInteger(start) || !Number.isInteger(end) || !(1 <= start && start <= end && end <= 10000) || end - start >= 3
      || !same(numbers, Array.from({ length: end - start + 1 }, (_, i) => start + i))
      || ![text, heading, ending].every(value => typeof value === 'string' && value.trim())
      || heading.length > 120 || ending.length > 120 || text.length > (other ? 600 : 800)) return null;
    const title = compact(heading).match(other ? OTHER_TITLE : ASSET_TITLE), closing = ending.match(SECTION)?.groups;
    if (!title || !closing) return null;
    const section = title[1], endSection = closing.number.replace(/[§\s]/g, '');
    const current = number(section), following = number(endSection);
    if (raw.section !== section || raw.endSection !== endSection || !following) return null;
    const adjacent = same(following, [...current.slice(0, -1), current.at(-1) + 1]);
    const chapter = other && same(following, [current[0] + 1]) && closing.number.startsWith('§');
    if (!adjacent && !chapter) return null;
    const result = { section, endSection, heading, endHeading: ending, startPage: start, endPage: end, pageNumbers: [...numbers],
      sectionBounded: true, pageNumbersVerified: true, text, amountUnit: '元' };
    const values = other ? otherValues(result) : assetValues(result);
    if (!values || Object.entries(values).some(([key, value]) => !same(raw[key], value))) return null;
    Object.assign(result, values);
    if (!other) { result.ratioBasis = 'fund-total-assets'; if (raw.ratioBasis !== result.ratioBasis) return null; }
    return raw.amountUnit === '元' ? result : null;
  }
  function normalizeNonstockAssets(evidence) {
    if (!object(evidence) || evidence.schemaVersion !== 1 || evidence.evidenceType !== TYPE || evidence.confirmed !== true
      || evidence.method !== METHOD || evidence.scope !== 'period-end-stock-book-value' || evidence.stockBookValue !== '0.00'
      || evidence.amountUnit !== '元' || !source(evidence.sourceUrl) || !date(evidence.periodEnd) || !date(evidence.publishedAt)
      || evidence.publishedAt < evidence.periodEnd || !same(period(evidence.reportPeriodText), [evidence.kind, evidence.periodEnd])
      || !/^[a-f0-9]{64}$/.test(evidence.sourceTextHash || '')) return null;
    const asset = table(evidence.assetTable), other = table(evidence.otherAssetsTable, true);
    if (!asset || !other || other.startPage < asset.endPage || number(other.section)[0] !== number(asset.section)[0]
      || number(other.section)[1] < number(asset.endSection)[1]
      || asset.rows.at(-1).amount !== other.total.amount) return null;
    const pages = [...new Set([...asset.pageNumbers, ...other.pageNumbers])].sort((a, b) => a - b);
    if (evidence.page !== asset.startPage || !same(evidence.pages, pages)) return null;
    return { schemaVersion: 1, evidenceType: TYPE, confirmed: true, method: METHOD, scope: 'period-end-stock-book-value',
      stockBookValue: '0.00', amountUnit: '元', sourceUrl: evidence.sourceUrl, periodEnd: evidence.periodEnd,
      publishedAt: evidence.publishedAt, kind: evidence.kind, reportPeriodText: evidence.reportPeriodText, sourceTextHash: evidence.sourceTextHash,
      page: asset.startPage, pages, assetTable: asset, otherAssetsTable: other };
  }
  return { TYPE, normalizeNonstockAssets, validNonstockAssets: evidence => normalizeNonstockAssets(evidence) !== null };
});
