(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RecordLabZeroStock = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function validZeroStock(evidence) {
    if (!evidence || evidence.evidenceType !== 'zero-stock-balance-sheet'
      || typeof evidence.text !== 'string' || !evidence.text.length || evidence.text.length > 600
      || typeof evidence.periodEnd !== 'string' || !/^20\d{2}-(?:06-30|12-31)$/.test(evidence.periodEnd)
      || evidence.kind !== (evidence.periodEnd.endsWith('06-30') ? 'semiannual' : 'annual')) return false;
    const [year, month, day] = evidence.periodEnd.split('-').map(Number);
    const text = evidence.text, compact = text.replace(/\s+/g, '');
    const current = `${year}年0?${month}月0?${day}日`, prior = `${year - 1}年12月31日`;
    const zero = '(?:[-—－–]|0(?:\\.0+)?)';
    const rows = [...text.matchAll(new RegExp('^[ \\t]*其中[：:][ \\t]*股票投资[ \\t]+' + zero + '[ \\t]+' + zero + '[ \\t]*$', 'gm'))];
    return /^\d+\.\d+资产负债表/.test(compact)
      && new RegExp('报告截止日[:：]' + current).test(compact)
      && compact.includes('单位：人民币元')
      && new RegExp('本期末' + current + '上年度末' + prior).test(compact)
      && rows.length === 1 && (compact.match(/股票投资/g) || []).length === 1
      && compact.includes('交易性金融资产') && compact.indexOf('交易性金融资产') < compact.indexOf('股票投资')
      && rows[0].index + rows[0][0].length === text.trimEnd().length;
  }
  function validExplicitNoStock(evidence) {
    if (!evidence || evidence.evidenceType !== 'explicit-no-stock-detail'
      || typeof evidence.text !== 'string' || !evidence.text.length || evidence.text.length > 600
      || typeof evidence.periodEnd !== 'string' || !/^20\d{2}-(?:03-31|06-30|09-30|12-31)$/.test(evidence.periodEnd)
      || !['quarterly', 'semiannual', 'annual'].includes(evidence.kind)
      || (evidence.kind === 'semiannual' && !evidence.periodEnd.endsWith('06-30'))
      || (evidence.kind === 'annual' && !evidence.periodEnd.endsWith('12-31'))) return false;
    const match = evidence.text.replace(/\s+/g, '').match(/^(\d+(?:\.\d+){1,2})期末按公允价值占基金资产净值比例大小排序的所有股票投资明细本基金不投资于股票。(\d+(?:\.\d+){1,2})报告期内股票投资组合的重大变动$/);
    if (!match) return false;
    const current = match[1].split('.').map(Number), following = match[2].split('.').map(Number);
    return current.length === following.length && current.slice(0, -1).every((value, index) => value === following[index])
      && following[following.length - 1] === current[current.length - 1] + 1;
  }
  return { validZeroStock, validExplicitNoStock };
});
