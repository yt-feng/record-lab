'use strict';

// Admission validates source-bound finite-rule statements. It never certifies
// full semantic interpretation or attributes the report's voice to a person.
const crypto = require('node:crypto');
const { strategySectionCoverageFor } = require('./strategy-sections');
const { normalizePortfolioIdentity } = require('./portfolio-identity');
const { chinaDate } = require('../web/source-dates');

const METHOD = 'explicit-strategy-claims-v2';
const UNKNOWN = new Set(['OBJECT_AMBIGUOUS', 'OBJECT_UNRECOGNIZED', 'PREDICATE_AMBIGUOUS', 'PREDICATE_UNRECOGNIZED',
  'SUBJECT_UNRESOLVED', 'SUBJECT_SCOPE_UNRESOLVED', 'NEGATION_SCOPE_UNRESOLVED', 'MODALITY_UNRESOLVED',
  'OTHER_PERIOD_REFERENCE', 'OBJECT_NEGATION_UNRESOLVED', 'TIME_SCOPE_UNRESOLVED', 'OBJECT_PREDICATE_LINK_UNRESOLVED', 'STANCE_SCOPE_UNRESOLVED']);
const COVERAGE = ['sentenceCount', 'clauseCount', 'scannedClauseCount', 'classifiedClauseCount', 'unknownClauseCount',
  'selectedClaimCount', 'selectedSentenceCount', 'omittedClaimCount', 'omittedSentenceCount'];
const SOURCE = ['sourceUrl', 'publishedAt', 'kind', 'periodStart', 'periodEnd', 'sourceTextHash', 'section', 'sectionContentHash', 'pages', 'retrievedAt', 'digest'];
const SUBJECTS = new Set(['本基金', '本产品', '本组合', '我们']);
const ACTIONS = { 增配: 'increase', 加仓: 'increase', 增持: 'increase', 买入: 'increase', 增加: 'increase', 提高: 'increase',
  减配: 'decrease', 减仓: 'decrease', 减持: 'decrease', 卖出: 'decrease', 降低: 'decrease', 减少: 'decrease', 维持: 'maintain', 保持: 'maintain' };
const STANCES = { 看好: 'positive', 看多: 'positive', 乐观: 'positive', 谨慎: 'cautious', 审慎: 'cautious', 看淡: 'negative', 看空: 'negative', 悲观: 'negative' };
const OBJECTS = {
  'asset-class': ['权益类资产', '权益资产', '可转换债券', '可转债', '利率债', '信用债', '股票', '债券', '现金', '港股', 'A股', '美股', '黄金'],
  industry: ['银行行业', '银行板块', '科技行业', '科技板块', '消费行业', '消费板块', '医药行业', '医药板块', '保险行业', '保险板块',
    '地产行业', '地产板块', '房地产行业', '房地产板块', '新能源行业', '新能源板块', '半导体行业', '半导体板块',
    '人工智能行业', '人工智能板块', '制造业', '消费电子行业', '消费电子板块'],
  style: ['高股息策略', '红利策略', '价值风格', '成长风格', '大盘风格', '小盘风格'], theme: ['人工智能方向'],
};
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const count = (value, max = 4096) => Number.isInteger(value) && value >= 0 && value <= max;
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const canonical = value => JSON.stringify(value, function (key, item) {
  return object(item) ? Object.fromEntries(Object.keys(item).sort().map(name => [name, item[name]])) : item;
});
const same = (a, b) => canonical(a) === canonical(b);
const keys = (value, names) => object(value) && same(Object.keys(value).sort(), [...names].sort());
const cp = value => Array.from(value);
const plain = (value, max) => typeof value === 'string' && value.length > 0 && cp(value).length <= max
  && !/[\u0000-\u001f\u007f]|[\ud800-\udfff]/u.test(value);
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
function instant(value) {
  const m = typeof value === 'string' && /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
  return m && date(m[1]) && +m[2] < 24 && +m[3] < 60 && +m[4] < 60 && (!m[6] || +m[6] < 24 && +m[7] < 60)
    && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
}
function instantOrder(value) {
  const millis = instant(value);
  if (millis === null) return null;
  const fraction = /\.(\d{1,6})(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] || '';
  return BigInt(millis) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3));
}
function evidenceFor(raw, source, section) {
  const names = ['id', 'text', 'textHash', 'page', 'pages', 'section', 'sentenceIndex', 'sourceUrl', 'periodStart', 'periodEnd',
    'quoteTruncated', 'securityMentions', 'provesTrade', 'provesTradeReason', 'evidencePoolId'];
  if (!keys(raw, names) || !/^s\d+$/.test(raw.id) || raw.id !== `s${raw.sentenceIndex}` || !plain(raw.text, 160)
    || !hash(raw.textHash) || sha(raw.text) !== raw.textHash || !count(raw.sentenceIndex, 239999)
    || !/^q[a-f0-9]{64}$/.test(raw.evidencePoolId) || raw.quoteTruncated !== false || raw.provesTrade !== false || raw.provesTradeReason !== false
    || ['sourceUrl', 'periodStart', 'periodEnd', 'section'].some(key => raw[key] !== source[key])
    || !Array.isArray(raw.pages) || !raw.pages.length || raw.page !== raw.pages[0]
    || !raw.pages.every((page, i) => Number.isInteger(page) && section.pages.includes(page) && page === raw.pages[0] + i)
    || !Array.isArray(raw.securityMentions) || raw.securityMentions.length > 20) return null;
  for (const mention of raw.securityMentions) {
    if (!keys(mention, ['stockCode', 'market', 'stockName', 'matchType', 'provesTrade', 'provesTradeReason'])
      || !plain(mention.stockName, 100) || typeof mention.stockCode !== 'string' || mention.provesTrade !== false || mention.provesTradeReason !== false
      || !(mention.market === 'CN' && /^\d{6}$/.test(mention.stockCode) || mention.market === 'HK' && /^\d{5}$/.test(mention.stockCode)
        || mention.market === 'US' && /^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?$/.test(mention.stockCode))) return null;
    if (mention.matchType === 'exact-name') {
      const name = mention.stockName.replace(/\s+/gu, '');
      if (cp(name).length < 4 || ['银行行业', '保险行业', '科技行业', '金融行业', '人工智能', '中国经济'].includes(name)
        || !raw.text.includes(name)) return null;
    } else {
      const code = mention.stockCode.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      if (mention.matchType !== 'explicit-code' || !(mention.market === 'US'
        ? new RegExp(`(?:股票代码|证券代码|ticker)\\s*[:：]?\\s*${code}(?![A-Z0-9.])`, 'i')
        : new RegExp(`(?<!\\d)${code}(?!\\d)`)).test(raw.text)) return null;
    }
  }
  return JSON.parse(JSON.stringify(raw));
}
function claimFor(raw, evidence) {
  const names = ['id', 'sentenceIndex', 'clauseIndex', 'sentenceHash', 'clauseHash', 'clauseStart', 'clauseEnd', 'classificationStatus',
    'kind', 'subject', 'object', 'action', 'stance', 'time', 'negation', 'conditional', 'conditionText', 'comparison', 'direction',
    'reasonStatus', 'reason', 'unknownReasons', 'matchedSpans', 'provesTrade', 'provesTradeReason', 'evidenceId'];
  if (!keys(raw, names) || !evidence || raw.evidenceId !== evidence.id || raw.sentenceIndex !== evidence.sentenceIndex
    || !count(raw.clauseIndex) || raw.id !== `c${raw.sentenceIndex}-${raw.clauseIndex}` || raw.sentenceHash !== evidence.textHash
    || !hash(raw.clauseHash) || !count(raw.clauseStart, 160) || !count(raw.clauseEnd, 160) || raw.clauseEnd <= raw.clauseStart
    || raw.clauseEnd > cp(evidence.text).length || raw.provesTrade !== false || raw.provesTradeReason !== false
    || !['classified', 'unknown'].includes(raw.classificationStatus) || !['viewpoint', 'portfolio-operation', 'outlook', 'unknown'].includes(raw.kind)
    || !keys(raw.subject, ['scope', 'text', 'isIndividualManager']) || raw.subject.isIndividualManager !== false
    || !keys(raw.object, ['type', 'label']) || !['report-period', 'forward-looking', 'conditional', 'mixed', 'unspecified'].includes(raw.time)
    || !['affirmed', 'negated', 'uncertain'].includes(raw.negation) || typeof raw.conditional !== 'boolean'
    || !keys(raw.comparison, ['present', 'text']) || typeof raw.comparison.present !== 'boolean'
    || !['increase', 'decrease', 'maintain', 'unknown'].includes(raw.direction)
    || !['unknown', 'explicit-same-sentence'].includes(raw.reasonStatus)
    || !Array.isArray(raw.unknownReasons) || new Set(raw.unknownReasons).size !== raw.unknownReasons.length || raw.unknownReasons.some(v => !UNKNOWN.has(v))
    || !Array.isArray(raw.matchedSpans) || raw.matchedSpans.length > 20) return null;
  const chars = cp(evidence.text), clause = chars.slice(raw.clauseStart, raw.clauseEnd).join('');
  const pieces = [...evidence.text.matchAll(/[^，,]+/gu)];
  const piece = pieces[raw.clauseIndex];
  if (!piece || cp(evidence.text.slice(0, piece.index)).length !== raw.clauseStart || piece[0] !== clause || sha(clause) !== raw.clauseHash) return null;
  const spans = new Map();
  for (const span of raw.matchedSpans) {
    if (!keys(span, ['field', 'text', 'start', 'end']) || !['subject', 'object', 'operation', 'stance', 'negation'].includes(span.field)
      || !count(span.start, 160) || !count(span.end, 160) || span.start < raw.clauseStart || span.end > raw.clauseEnd || span.start >= span.end
      || chars.slice(span.start, span.end).join('') !== span.text) return null;
    if (!spans.has(span.field)) spans.set(span.field, []); spans.get(span.field).push(span);
  }
  const subjectMatch = /本基金|本产品|本组合|我们/u.exec(clause);
  const subject = raw.subject.text;
  if (subject !== (subjectMatch?.[0] || null) || subjectMatch && (spans.get('subject')?.[0]?.start !==
    raw.clauseStart + cp(clause.slice(0, subjectMatch.index)).length)) return null;
  const objectPattern = Object.values(OBJECTS).flat().sort((a, b) => cp(b).length - cp(a).length).join('|');
  const foundObjects = [...clause.matchAll(new RegExp(objectPattern, 'gu'))];
  const objectLabels = [...new Set(foundObjects.map(item => item[0]))];
  if (objectLabels.length === 1) {
    if (raw.object.label !== objectLabels[0] || (spans.get('object') || []).length !== foundObjects.length
      || foundObjects.some((item, index) => spans.get('object')[index].start !== raw.clauseStart + cp(clause.slice(0, item.index)).length)) return null;
  } else if (raw.object.type !== 'unknown' || spans.has('object')) return null;
  if (subject === null) { if (raw.subject.scope !== 'unknown' || spans.has('subject')) return null; }
  else if (!SUBJECTS.has(subject) || raw.subject.scope !== (subject === '我们' ? 'report-author' : 'portfolio')
    || spans.get('subject')?.length !== 1 || spans.get('subject')[0].text !== subject) return null;
  if (raw.object.type === 'unknown') { if (raw.object.label !== null || spans.has('object')) return null; }
  else if (!OBJECTS[raw.object.type]?.includes(raw.object.label) || !spans.get('object')?.length
    || spans.get('object').some(span => span.text !== raw.object.label)) return null;
  if (raw.conditional ? !plain(raw.conditionText, 100) || !evidence.text.includes(raw.conditionText)
    : raw.conditionText !== null) return null;
  if (raw.comparison.present ? !plain(raw.comparison.text, 80) || !clause.includes(raw.comparison.text) : raw.comparison.text !== null) return null;
  if (raw.negation === 'negated' && spans.get('negation')?.length !== 1) return null;
  const condition = /(?:如果|倘若|若|一旦|只要|除非)[^，,。；]{1,60}|在[^，,。；]{1,50}(?:前提下|情况下)/u.exec(evidence.text);
  const comparison = /相比|相较|较(?:上期|上季|去年|此前)|相对(?:于|其他|更)|比[^，,。；]{1,20}更|更(?:加)?(?:看好|看多|谨慎|审慎|乐观|看淡|看空|悲观)/u.exec(clause);
  const future = /展望|未来|下一阶段|后续|后市|预计|计划|有望|打算|准备|拟(?:增|减|加|买|卖|调|提|降)|将(?:继续|会|要)?/u.test(clause);
  const past = /本期|报告期内|报告期|本季度|本季|期内|已经|已(?:增|减|加|买|卖|调|提|降|维|保)/u.test(clause);
  let time = condition ? 'conditional' : future && past ? 'mixed' : future ? 'forward-looking' : past ? 'report-period' : 'unspecified';
  const otherPeriod = !comparison && /上(?:一)?(?:季度|季|期)|去年|年初以来|今年以来|以往|此前/u.test(clause);
  if (otherPeriod) time = 'unspecified';
  if (raw.classificationStatus === 'classified' && raw.action && time === 'unspecified') time = 'report-period';
  if (raw.time !== time || raw.conditional !== Boolean(condition) || raw.conditionText !== (condition?.[0] || null)
    || raw.comparison.present !== Boolean(comparison) || raw.comparison.text !== (comparison?.[0] || null)) return null;
  const actions = [...clause.matchAll(/增配|加仓|增持|买入|减配|减仓|减持|卖出|增加|提高|降低|减少|维持|保持/gu)];
  const operations = actions.filter(item => !['增加', '提高', '降低', '减少', '维持', '保持'].includes(item[0]) || /配置|仓位|比重|权重|持仓|敞口/u.test(clause));
  const stances = [...clause.matchAll(/看好|看多|乐观|谨慎|审慎|看淡|看空|悲观/gu)];
  const predicates = [...operations.map(item => ['operation', item]), ...stances.map(item => ['stance', item])];
  const uncertain = /并非|并不|不是不|未必|不一定|不排除|不能排除|没有理由不|不再|尚难|难以/u.test(clause);
  let negation = uncertain ? 'uncertain' : 'affirmed';
  if (predicates.length === 1) {
    const [field, match] = predicates[0];
    const before = cp(clause.slice(0, match.index)).slice(-8).join('');
    const negated = /(?:没有|并未|尚未|未曾|未|不)(?:再)?$/u.exec(before);
    if (negated && !uncertain) negation = 'negated';
    else if (!negated && /没有|并未|尚未|未曾|未(?!来)|不/u.test(before)) negation = 'uncertain';
    const start = raw.clauseStart + cp(clause.slice(0, match.index)).length;
    if (spans.get(field)?.length !== 1 || spans.get(field)[0].start !== start || spans.get(field)[0].text !== match[0]) return null;
    if (negation === 'negated' && (spans.get('negation')[0].text !== negated[0] || spans.get('negation')[0].end !== start)) return null;
  } else if (spans.has('operation') || spans.has('stance')) return null;
  if (raw.negation !== negation || negation !== 'negated' && spans.has('negation')) return null;
  if (raw.classificationStatus === 'unknown') {
    if (!raw.unknownReasons.length || raw.kind !== 'unknown' || raw.action !== null || raw.stance !== null || raw.direction !== 'unknown'
      || raw.reasonStatus !== 'unknown' || raw.reason !== null) return null;
  } else {
    if (raw.unknownReasons.length || !subject || raw.object.type === 'unknown' || raw.negation === 'uncertain' || raw.time === 'mixed'
      || /引用|转述|据称|听说|声称|表示|指出|提到|认为|[“”「」『』‘’"']/u.test(evidence.text)
      || [...clause.matchAll(/本基金|本产品|本组合|我们/gu)].length !== 1 || predicates.length !== 1
      || /其他基金|其他组合|该基金|该组合|他们|他人|业内|分析师|可能|或许|也许|假设|是否|取决于|视.{0,20}而定|应当|必须|建议|不宜/u.test(clause)
      || otherPeriod || future && past) return null;
    const [predicateKind, predicate] = predicates[0];
    const linked = (spans.get('object') || []).some(span => {
      const start = span.start - raw.clauseStart, end = span.end - raw.clauseStart;
      const predStart = cp(clause.slice(0, predicate.index)).length, predEnd = predStart + cp(predicate[0]).length;
      const body = cp(clause), between = start >= predEnd ? body.slice(predEnd, start).join('') : body.slice(end, predStart).join('');
      if (/非|不属于|除$/u.test(body.slice(Math.max(0, start - 4), start).join(''))) return false;
      return (start >= predEnd ? /^(?:了|对|的|在|部分|少量|适度|相关)*$/u
        : predicateKind === 'stance' ? /^(?:的|配置|仓位|比重|权重|持仓|敞口|方面|保持|维持)*$/u
        : /^(?:的|配置|仓位|比重|权重|持仓|敞口|方面)*$/u).test(between);
    });
    if (!linked || predicateKind === 'stance' && actions.some(item => ['增加', '提高', '降低', '减少'].includes(item[0]))) return null;
    const operations = spans.get('operation') || [], stances = spans.get('stance') || [];
    if (raw.action !== null) {
      if (operations.length !== 1 || stances.length || ACTIONS[operations[0].text] !== raw.action || raw.stance !== null) return null;
    } else if (stances.length !== 1 || operations.length || STANCES[stances[0].text] !== raw.stance) return null;
    const expectedKind = raw.conditional || raw.time === 'forward-looking' ? 'outlook' : raw.action ? 'portfolio-operation' : 'viewpoint';
    const expectedDirection = raw.action && expectedKind === 'portfolio-operation' && raw.negation === 'affirmed' && !raw.comparison.present ? raw.action : 'unknown';
    if (raw.kind !== expectedKind || raw.direction !== expectedDirection) return null;
    if (raw.reasonStatus === 'explicit-same-sentence') {
      const reason = raw.reason, prefix = /^(?:由于|因为|基于|考虑到)([^，,。；]{2,60})[，,]/u.exec(evidence.text);
      if (!keys(reason, ['text', 'start', 'end', 'scope', 'provesIndividualStockReason']) || !raw.action || raw.negation !== 'affirmed'
        || raw.conditional || raw.comparison.present || raw.clauseIndex !== 1 || !prefix || prefix[1] !== reason.text
        || reason.scope !== 'portfolio-statement' || reason.provesIndividualStockReason !== false
        || !count(reason.start, 160) || !count(reason.end, 160) || reason.start >= reason.end || reason.end > raw.clauseStart
        || chars.slice(reason.start, reason.end).join('') !== reason.text
        || reason.start !== cp(prefix[0].slice(0, prefix[0].indexOf(prefix[1]))).length
        || reason.end !== reason.start + cp(reason.text).length
        || /由于|因为|基于|考虑到|以及|及|和|与|表示|声称|认为|指出|提到|听说|据称|[“”「」‘’"]/u.test(reason.text)) return null;
    } else if (raw.reason !== null) return null;
  }
  return JSON.parse(JSON.stringify(raw));
}

function strategyClaimsFor(report, { code = report?.fundCode || report?.sourceFundCode, now = new Date(), asOf = null } = {}) {
  if (!object(report) || !/^\d{6}$/.test(code || '') || code === '000000') return null;
  const raw = report.strategyClaims, proof = strategySectionCoverageFor(report.strategySectionCoverage, report.periodEnd);
  const current = now instanceof Date ? now.getTime() : typeof now === 'number' ? now : Date.parse(now);
  if (!object(raw) || raw.schemaVersion !== 1 || raw.method !== METHOD || !Number.isFinite(current) || !proof?.complete
    || proof.kind !== 'quarterly' || proof.sections.length !== 1 || raw.sourceSectionComplete !== true || raw.ruleScanComplete !== true
    || raw.semanticAnalysisComplete !== false || raw.scanScope !== 'all-source-clauses-finite-rules' || raw.publicEvidenceScope !== 'selected-evidence'
    || raw.managerAttribution !== 'fund-report-not-individual-speaker' || raw.explainsIndividualStockTrades !== false
    || !Array.isArray(raw.reasonCodes) || raw.reasonCodes.length || !keys(raw.source, SOURCE)) return null;
  const source = raw.source, section = proof.sections[0], observed = instant(source.retrievedAt);
  const priorObserved = instant(report.strategyEvidenceRetrievedAt);
  const derived = report.strategyClaimsDerivedFromObservedAt;
  const clockBound = derived === undefined || derived === null
    ? report.strategyEvidenceRetrievedAt === source.retrievedAt
    : derived === report.strategyEvidenceRetrievedAt && priorObserved !== null && observed !== null && observed >= priorObserved;
  if (['sourceUrl', 'publishedAt', 'kind', 'periodStart', 'periodEnd', 'sourceTextHash'].some(key => source[key] !== proof[key])
    || source.section !== section.section || source.sectionContentHash !== section.contentHash || !same(source.pages, section.pages)
    || observed === null || observed > current + 300000 || source.publishedAt > chinaDate(source.retrievedAt)
    || !clockBound || asOf && (!date(asOf) || source.publishedAt > asOf)
    || source.digest !== sha(canonical({ ...Object.fromEntries(SOURCE.filter(key => !['retrievedAt', 'digest'].includes(key)).map(key => [key, source[key]])), method: METHOD }))) return null;
  const identity = normalizePortfolioIdentity(report.strategyClaimsIdentityEvidence,
    { sourceUrl: source.sourceUrl, periodEnd: source.periodEnd, publishedAt: source.publishedAt, kind: source.kind, fundCode: code }, new Date(current));
  if (!identity || identity.retrievedAt !== source.retrievedAt) return null;
  const { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor } = require('./strategy-evidence-pool');
  const pool = normalizeStrategyEvidencePool(report.strategyEvidencePool, proof, source, current);
  const budget = strategyEvidenceBudgetFor(report, current);
  if (!pool || budget?.status !== 'verified') return null;
  const totals = raw.coverage;
  if (!keys(totals, COVERAGE) || COVERAGE.some(key => !count(totals[key], key.includes('Sentence') || key === 'sentenceCount' ? 240000 : 4096))
    || totals.sentenceCount !== proof.sentenceCount || totals.clauseCount !== totals.scannedClauseCount
    || totals.scannedClauseCount !== totals.classifiedClauseCount + totals.unknownClauseCount
    || !['scanned', 'scanned-with-unknown'].includes(raw.status) || raw.status !== (totals.unknownClauseCount ? 'scanned-with-unknown' : 'scanned')
    || !Array.isArray(raw.claims) || raw.claims.length > 8 || !Array.isArray(raw.evidence) || raw.evidence.length > 8
    || totals.selectedClaimCount !== raw.claims.length || totals.selectedSentenceCount !== raw.evidence.length
    || totals.omittedClaimCount + totals.selectedClaimCount !== totals.clauseCount
    || totals.omittedSentenceCount + totals.selectedSentenceCount !== totals.sentenceCount
    || !object(raw.unknownReasonCounts) || Object.entries(raw.unknownReasonCounts).some(([key, value]) => !UNKNOWN.has(key) || !count(value) || value > totals.unknownClauseCount)) return null;
  const evidence = raw.evidence.map(item => evidenceFor(item, source, section));
  if (evidence.some(item => !item || item.sentenceIndex >= proof.sentenceCount) || new Set(evidence.map(item => item.id)).size !== evidence.length) return null;
  const byId = new Map(evidence.map(item => [item.id, item]));
  const claims = raw.claims.map(item => claimFor(item, byId.get(item?.evidenceId)));
  if (claims.some(item => !item) || new Set(claims.map(item => item.id)).size !== claims.length
    || evidence.some(item => !claims.some(claim => claim.evidenceId === item.id))
    || claims.filter(item => item.classificationStatus === 'classified').length > totals.classifiedClauseCount
    || claims.filter(item => item.classificationStatus === 'unknown').length > totals.unknownClauseCount) return null;
  for (const reason of UNKNOWN) if (claims.filter(item => item.unknownReasons.includes(reason)).length > (raw.unknownReasonCounts[reason] || 0)) return null;
  const used = evidence.reduce((sum, item) => sum + cp(item.text).length, 0), limit = raw.publicQuoteBudget;
  if (!keys(limit, ['sharedAcrossReport', 'requiresSharedBudgetAtIntegration', 'maxQuotes', 'maxQuoteChars', 'maxTotalQuoteChars', 'usedQuotes', 'usedQuoteChars'])
    || limit.sharedAcrossReport !== true || limit.requiresSharedBudgetAtIntegration !== true || limit.maxQuotes !== 8 || limit.maxQuoteChars !== 160
    || limit.maxTotalQuoteChars !== 1280 || limit.usedQuotes !== evidence.length || limit.usedQuoteChars !== used || used > 1280) return null;
  return { schemaVersion: 1, method: METHOD, status: raw.status, source: JSON.parse(JSON.stringify(source)), sourceSectionComplete: true,
    ruleScanComplete: true, semanticAnalysisComplete: false, scanScope: raw.scanScope, publicEvidenceScope: raw.publicEvidenceScope,
    managerAttribution: raw.managerAttribution, explainsIndividualStockTrades: false, coverage: { ...totals },
    unknownReasonCounts: { ...raw.unknownReasonCounts }, reasonCodes: [], claims, evidence, publicQuoteBudget: { ...limit } };
}

const CLAIM_FIELDS = ['strategyClaims', 'strategyClaimsStatus', 'strategyClaimsIdentityEvidence', 'strategyClaimsDerivedFromObservedAt',
  'strategyEvidencePool', 'strategyEvidenceBudget', 'strategyAnalysis', 'strategyExcerpts', 'strategyThemes', 'narrative'];
function retainStrategyClaims(target, source, { code, now = new Date(), asOf = null } = {}) {
  const incoming = strategyClaimsFor(source, { code, now, asOf });
  if (!incoming || strategyClaimsFor(target, { code, now, asOf })) return false;
  const oldProof = strategySectionCoverageFor(target?.strategySectionCoverage, target?.periodEnd);
  const newProof = strategySectionCoverageFor(source?.strategySectionCoverage, target?.periodEnd);
  if (!oldProof?.complete || !newProof?.complete
    || ['sourceUrl', 'publishedAt', 'periodStart', 'periodEnd', 'kind', 'sourceTextHash'].some(key => oldProof[key] !== newProof[key])
    || !same(oldProof.sections.map(item => [item.section, item.contentHash, item.pages]), newProof.sections.map(item => [item.section, item.contentHash, item.pages]))) return false;
  const candidate = { ...target };
  for (const key of CLAIM_FIELDS) {
    if (Object.hasOwn(source, key)) candidate[key] = JSON.parse(JSON.stringify(source[key]));
    else delete candidate[key];
  }
  const oldTime = instantOrder(target.strategyEvidenceRetrievedAt), newTime = instantOrder(incoming.source.retrievedAt);
  if (oldTime === null || newTime === null) return false;
  if (newTime < oldTime) {
    // A newer holdings observation may contain a failed analysis attempt.
    // Restore the earlier verified analysis and its actual source clock as a
    // group; never relabel old claims as if fetched with the new holdings.
    candidate.strategyEvidenceRetrievedAt = source.strategyEvidenceRetrievedAt;
  } else if (target.strategyEvidenceRetrievedAt !== incoming.source.retrievedAt) {
    candidate.strategyClaimsDerivedFromObservedAt = target.strategyEvidenceRetrievedAt;
  } else delete candidate.strategyClaimsDerivedFromObservedAt;
  // Revalidate the full projected quote budget against the retained quarter's
  // section and old strategy clock before copying only the analysis fields.
  if (!strategyClaimsFor(candidate, { code, now, asOf })) return false;
  for (const key of CLAIM_FIELDS) {
    if (Object.hasOwn(candidate, key)) target[key] = candidate[key];
    else delete target[key];
  }
  target.strategyEvidenceRetrievedAt = candidate.strategyEvidenceRetrievedAt;
  return true;
}

module.exports = { METHOD, strategyClaimsFor, retainStrategyClaims, instant, canonical };
