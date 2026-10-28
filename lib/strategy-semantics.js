'use strict';

// Serialized mapper output is checked again before an in-memory compare/swap.
// This module does not infer meaning, reparse private PDF text, call a provider,
// persist a ledger, or publish. Caller bindings must come from the work receipt.
const crypto = require('node:crypto');
const { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor } = require('./strategy-evidence-pool');
const { strategySectionCoverageFor } = require('./strategy-sections');
const { normalizePortfolioIdentity } = require('./portfolio-identity');
const { instant, canonical } = require('./strategy-claim-patches');
const METHOD = 'independent-semantic-pool-admission-v1';
const CANDIDATE_METHOD = 'semantic-canonical-pool-candidate-v1';
const SOURCE_FIELDS = ['sourceUrl', 'publishedAt', 'periodStart', 'periodEnd', 'kind', 'sourceTextHash',
  'retrievedAt', 'section', 'sectionContentHash', 'pages'];
const CANDIDATE_FIELDS = ['schemaVersion', 'kind', 'method', 'status', 'code', 'sourceSectionComplete',
  'inputCoverageComplete', 'offsetUnit', 'spanOrigin', 'sentenceRangeOrigin', 'selectedEvidenceOnly',
  'mappingCompleteForSelectedClaims', 'semanticCorrectnessVerified', 'semanticAnalysisComplete', 'productionReady',
  'productionAdmission', 'managerAttribution', 'explainsIndividualStockTrades', 'inputDigest', 'baselinePoolDigest',
  'resultPoolDigest', 'baselineSnapshotDigest', 'resultSnapshotDigest', 'pool', 'strategySectionCoverage',
  'evidenceIdMap', 'claims', 'sentenceStatuses', 'coverage', 'sidecar', 'usageHistory', 'usageAccounting', 'candidateDigest'];
const CLAIM_FIELDS = ['kind', 'subject', 'object', 'predicate', 'polarity', 'time', 'negation', 'conditional',
  'negation_spans', 'condition_spans', 'comparison_spans', 'attribution_spans', 'reason', 'sentence_id',
  'quotedContext', 'semanticVerification', 'provesTrade', 'provesTradeReason', 'evidencePoolId', 'sentenceRange'];
const COVERAGE_FIELDS = ['sentenceCount', 'interpreted', 'not-relevant', 'unknown', 'validatedClaimCount',
  'publishedClaimCount', 'longSentenceClaims', 'quoteBudgetClaims', 'claimLimitClaims', 'mappedClaimCount',
  'mapperOmittedClaims', 'newQuoteCount'];
const REASONS = { unknown: ['ambiguous-subject', 'ambiguous-scope', 'ambiguous-causality', 'uninterpretable',
  'insufficient-context', 'not-classified'], 'not-relevant': ['administrative', 'performance-only', 'background-only', 'non-strategy'] };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const same = (a, b) => canonical(a) === canonical(b);
const keys = (value, fields) => object(value) && same(Object.keys(value).sort(), [...fields].sort());
const clone = value => JSON.parse(JSON.stringify(value));
const sha = text => crypto.createHash('sha256').update(text).digest('hex');
const digest = value => sha(canonical(value));
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const count = (value, max = 60000) => Number.isSafeInteger(value) && value >= 0 && value <= max;
const chars = value => Array.from(value);
const without = (value, excluded) => Object.fromEntries(Object.entries(value).filter(([key]) => !excluded.includes(key)));
class SemanticAdmissionError extends Error {
  constructor(code) { super(code); this.name = 'SemanticAdmissionError'; this.code = code; }
}
const fail = code => { throw new SemanticAdmissionError(code); };
function clockFor(now = new Date()) {
  if (now instanceof Date) return Number.isFinite(now.getTime()) ? BigInt(now.getTime()) * 1000n : null;
  return typeof now === 'number' && Number.isSafeInteger(now) ? BigInt(now) * 1000n : instant(now);
}
function poolDigest(pool) {
  return digest({ schemaVersion: 1, source: without(pool.source, ['retrievedAt']), quotes: pool.quotes });
}
function pageRange(pages, allowed) {
  return Array.isArray(pages) && pages.length > 0 && pages.length <= 16
    && pages.every((page, i) => Number.isSafeInteger(page) && allowed.includes(page) && page === pages[0] + i);
}
function checkHistory(candidate, clock, bindings) {
  const history = candidate.usageHistory, accounting = candidate.usageAccounting;
  if (!Array.isArray(history) || !history.length || history.length > 64
    || !hash(bindings.expectedUsageHistoryDigest) || digest(history) !== bindings.expectedUsageHistoryDigest)
    fail('SEMANTIC_HISTORY_BINDING_INVALID');
  let previous = null, tokens = 0, unknown = 0;
  const attempts = new Set();
  for (const row of history) {
    const start = instant(row?.attemptedAt), end = instant(row?.finishedAt);
    if (!keys(row, ['requestDigest', 'inputDigest', 'attemptedAt', 'finishedAt', 'outcome', 'responseDigest', 'usage'])
      || !hash(row.requestDigest) || !hash(row.inputDigest) || start === null || start > clock + 300000000n
      || previous !== null && start < previous || !['started', 'unknown', 'failed', 'rejected', 'validated'].includes(row.outcome)
      || (row.outcome === 'started' ? row.finishedAt !== null : end === null || start > end || end > clock + 300000000n)
      || row.responseDigest !== null && !hash(row.responseDigest)) fail('SEMANTIC_HISTORY_INVALID');
    const key = row.requestDigest + ':' + start;
    if (attempts.has(key)) fail('SEMANTIC_HISTORY_DUPLICATE_ATTEMPT');
    attempts.add(key); previous = start;
    if (row.usage === null) unknown++;
    else if (!keys(row.usage, ['prompt_tokens', 'completion_tokens', 'total_tokens'])
      || Object.values(row.usage).some(value => !count(value, 2000000))
      || row.usage.total_tokens !== row.usage.prompt_tokens + row.usage.completion_tokens) fail('SEMANTIC_USAGE_INVALID');
    else tokens += row.usage.total_tokens;
  }
  if (!same(accounting, { historyDigest: bindings.expectedUsageHistoryDigest, attemptCount: history.length,
    knownTotalTokens: tokens, unknownUsageAttempts: unknown, accounting: 'read-only-supplied-history-no-reset',
    externalReceiptsIndependentlyVerified: false })) fail('SEMANTIC_USAGE_ACCOUNTING_MISMATCH');
  const model = candidate.sidecar.modelObservation, observation = history.at(-1);
  if (!keys(model, ['provider', 'model', 'responseModel', 'promptVersion', 'boundResponseDigest', 'observation'])
    || model.provider !== 'deepseek' || model.model !== 'deepseek-flash' || model.responseModel !== model.model
    || typeof model.promptVersion !== 'string' || !/^source-bound-deepseek-v[1-9][0-9]{0,2}$/.test(model.promptVersion) || !hash(model.boundResponseDigest)
    || !same(model.observation, observation) || observation.outcome !== 'validated' || !hash(observation.responseDigest)
    || observation.inputDigest !== candidate.inputDigest
    || instant(observation.attemptedAt) < instant(candidate.sidecar.source.retrievedAt)) fail('SEMANTIC_MODEL_OBSERVATION_MISMATCH');
}
function checkSpan(value, text, optional = false) {
  if (optional && value === null) return null;
  if (!keys(value, ['start', 'end', 'textHash']) || !count(value.start) || !count(value.end)
    || value.start >= value.end || value.end > text.length
    || value.textHash !== sha(text.slice(value.start, value.end).join(''))) fail('SEMANTIC_NONLITERAL_SPAN');
  return text.slice(value.start, value.end).join('');
}
function checkClaim(claim, sentence, quote, source) {
  if (!keys(claim, CLAIM_FIELDS) || !['theme', 'opinion', 'operation', 'reason', 'unknown'].includes(claim.kind)
    || !['positive', 'negative', 'neutral', 'mixed', 'unknown'].includes(claim.polarity)
    || !['report-period', 'past', 'future', 'conditional', 'mixed', 'unspecified'].includes(claim.time)
    || !['affirmed', 'negated', 'uncertain'].includes(claim.negation) || typeof claim.conditional !== 'boolean'
    || claim.semanticVerification !== 'pending' || claim.provesTrade !== false || claim.provesTradeReason !== false
    || !keys(claim.subject, ['scope', 'span']) || !['fund-report', 'market', 'quoted-other', 'unknown'].includes(claim.subject.scope)
    || !keys(claim.object, ['type', 'span']) || !['theme', 'industry', 'asset-class', 'security-mention', 'unknown'].includes(claim.object.type)
    || !same(claim.sentenceRange, Object.fromEntries(['start', 'end', 'pages', 'textHash'].map(key => [key, sentence[key]]))))
    fail('SEMANTIC_CLAIM_INVALID');
  const text = chars(quote.text), subject = checkSpan(claim.subject.span, text, true);
  const target = checkSpan(claim.object.span, text, true), predicate = checkSpan(claim.predicate, text, true);
  if (claim.subject.scope !== 'unknown' && subject === null || claim.object.type !== 'unknown' && target === null
    || claim.kind !== 'unknown' && predicate === null) fail('SEMANTIC_LITERAL_REQUIRED');
  for (const name of ['negation_spans', 'condition_spans', 'comparison_spans', 'attribution_spans']) {
    if (!Array.isArray(claim[name]) || claim[name].length > 8) fail('SEMANTIC_CONTEXT_INVALID');
    for (const span of claim[name]) checkSpan(span, text);
  }
  if (claim.conditional !== Boolean(claim.condition_spans.length) || claim.conditional && !['conditional', 'mixed'].includes(claim.time)
    || /如果|倘若|一旦|除非|只要|若|前提下/u.test(quote.text) && !claim.conditional
    || /并非|并不|不再|没有|未曾|未(?!来)|不/u.test(quote.text) && claim.negation === 'affirmed'
    || claim.negation === 'negated' && (!claim.negation_spans.length || claim.polarity === 'positive')
    || /未来|预计|计划|有望|将(?:继续|会|要)?/u.test(quote.text) && !['future', 'conditional', 'mixed', 'unspecified'].includes(claim.time))
    fail('SEMANTIC_QUALIFIER_CHANGED');
  for (const [pattern, field] of [[/并非|并不|不再|没有|未曾|未(?!来)|不/gu, 'negation_spans'],
    [/如果|倘若|一旦|除非|只要|若|前提下/gu, 'condition_spans'], [/相较|相比|较上|比去年|更看好|更加|相对/gu, 'comparison_spans']]) {
    for (const match of quote.text.matchAll(pattern)) {
      const start = chars(quote.text.slice(0, match.index)).length, end = start + chars(match[0]).length;
      if (!claim[field].some(span => span.start <= start && span.end >= end)) fail('SEMANTIC_QUALIFIER_CHANGED');
    }
  }
  if (!Array.isArray(claim.quotedContext) || claim.quotedContext.length > 8) fail('SEMANTIC_CONTEXT_INVALID');
  const seen = new Set();
  for (const context of claim.quotedContext) {
    if (!keys(context, ['start', 'end', 'closed', 'attribution', 'intro']) || !count(context.start) || !count(context.end)
      || context.start >= context.end || context.end > source.contentChars || context.start >= sentence.end || context.end <= sentence.start
      || typeof context.closed !== 'boolean' || !['quoted-other', 'unknown'].includes(context.attribution)
      || !context.closed && context.attribution !== 'unknown' || seen.has(digest(context))) fail('SEMANTIC_CONTEXT_INVALID');
    seen.add(digest(context));
    if (context.intro !== null) {
      const intro = context.intro;
      if (!keys(intro, ['start', 'end', 'textHash']) || !count(intro.start) || !count(intro.end)
        || intro.start >= intro.end || intro.end > context.start || !hash(intro.textHash)) fail('SEMANTIC_CONTEXT_INVALID');
      if (intro.start >= sentence.start && intro.end <= sentence.end) {
        const literal = checkSpan({ ...intro, start: intro.start - sentence.start, end: intro.end - sentence.start }, text);
        if (!/^(?:报告引用|引用|分析师|其他基金|市场人士)$/u.test(literal)) fail('SEMANTIC_CONTEXT_INVALID');
      }
    }
    if (context.attribution === 'quoted-other' && context.intro === null) fail('SEMANTIC_CONTEXT_INVALID');
  }
  if (/[“”‘’「」『』"']/u.test(quote.text) && !claim.quotedContext.length
    || claim.quotedContext.length && !['quoted-other', 'unknown'].includes(claim.subject.scope)
    || claim.quotedContext.some(row => !row.closed || row.attribution === 'unknown') && claim.subject.scope !== 'unknown'
    || claim.subject.scope === 'fund-report' && !/^(?:本基金|本组合|本产品|我们)$/u.test(subject)) fail('SEMANTIC_ATTRIBUTION_CHANGED');
  if (!keys(claim.reason, ['status', 'cause', 'connective', 'operation']) || !['explicit', 'unknown'].includes(claim.reason.status))
    fail('SEMANTIC_REASON_INVALID');
  if (claim.reason.status === 'unknown') {
    if (['cause', 'connective', 'operation'].some(key => claim.reason[key] !== null) || claim.kind === 'reason') fail('SEMANTIC_REASON_INVALID');
  } else {
    const reason = claim.reason, cause = checkSpan(reason.cause, text), connective = checkSpan(reason.connective, text);
    const operation = checkSpan(reason.operation, text);
    if (claim.subject.scope !== 'fund-report' || claim.negation !== 'affirmed' || claim.conditional || claim.comparison_spans.length
      || ['past', 'future', 'conditional', 'mixed'].includes(claim.time) || claim.object.type === 'security-mention'
      || !['operation', 'reason'].includes(claim.kind) || !['由于', '因为', '基于', '考虑到'].includes(connective)
      || reason.connective.start !== 0 || reason.connective.end !== reason.cause.start
      || !/^[，,]\s*$/u.test(text.slice(reason.cause.end, reason.operation.start).join(''))
      || /[，,；。]|由于|因为|基于|考虑到|引用|表示|指出|认为/u.test(cause)
      || [...quote.text.matchAll(/增持|减持|增配|减配|增加|减少|降低|提高|买入|卖出|加仓|减仓/gu)].length !== 1
      || reason.operation.end !== chars(quote.text.replace(/[。！？；]+$/u, '')).length || !subject || !predicate || !target
      || [claim.subject.span, claim.predicate, claim.object.span].some(span => span.start < reason.operation.start || span.end > reason.operation.end)
      || /[，,；。]/u.test(operation)) fail('SEMANTIC_CAUSAL_SCOPE_UNPROVEN');
  }
}
function checkEvidence(candidate, proof) {
  const coverage = candidate.coverage, section = proof.sections[0], statuses = candidate.sentenceStatuses;
  if (!keys(coverage, COVERAGE_FIELDS) || COVERAGE_FIELDS.filter(key => key !== 'mapperOmittedClaims').some(key => !count(coverage[key]))
    || !keys(coverage.mapperOmittedClaims, ['ambiguousLiteralClaims', 'quoteBudgetClaims'])
    || Object.values(coverage.mapperOmittedClaims).some(value => !count(value, 512))
    || coverage.sentenceCount !== proof.sentenceCount || !Array.isArray(statuses) || statuses.length !== proof.sentenceCount
    || statuses.length > 4096 || !Array.isArray(candidate.claims) || candidate.claims.length > 8
    || coverage.mappedClaimCount !== candidate.claims.length || coverage.publishedClaimCount > 8 || coverage.validatedClaimCount > 512
    || coverage.publishedClaimCount !== candidate.claims.length + Object.values(coverage.mapperOmittedClaims).reduce((a, b) => a + b, 0)
    || coverage.validatedClaimCount !== coverage.publishedClaimCount + coverage.longSentenceClaims + coverage.quoteBudgetClaims + coverage.claimLimitClaims
    || candidate.mappingCompleteForSelectedClaims !== Object.values(coverage.mapperOmittedClaims).every(value => value === 0))
    fail('SEMANTIC_COVERAGE_INVALID');
  const sourceDigest = digest(without(candidate.sidecar.source, ['retrievedAt']));
  const bySentence = new Map(), counts = { interpreted: 0, 'not-relevant': 0, unknown: 0 };
  let previous = null;
  for (const [index, row] of statuses.entries()) {
    if (!keys(row, ['sentence_id', 'index', 'textHash', 'start', 'end', 'pages', 'status', 'reason'])
      || row.index !== index || !hash(row.textHash) || row.sentence_id !== 's-' + sha(`${sourceDigest}:${index}:${row.textHash}`).slice(0, 24)
      || !count(row.start) || !count(row.end) || row.start >= row.end || row.end > proof.contentChars
      || previous !== null && row.start < previous.end || !pageRange(row.pages, section.pages)
      || previous !== null && row.pages[0] < previous.pages.at(-1) || !Object.hasOwn(counts, row.status)
      || (row.status === 'interpreted' ? row.reason !== null : !REASONS[row.status].includes(row.reason))) fail('SEMANTIC_SENTENCE_MAP_INVALID');
    bySentence.set(row.sentence_id, row); counts[row.status]++; previous = row;
  }
  if (Object.entries(counts).some(([key, value]) => coverage[key] !== value)
    || counts.interpreted > coverage.validatedClaimCount || coverage.validatedClaimCount > counts.interpreted * 8)
    fail('SEMANTIC_COVERAGE_INVALID');
  const poolById = new Map(candidate.pool.quotes.map(row => [row.id, row])), mapping = new Map(), claimsPerSentence = new Map();
  let lastIndex = -1;
  for (const claim of candidate.claims) {
    const sentence = bySentence.get(claim?.sentence_id), quote = poolById.get(claim?.evidencePoolId);
    if (!sentence || sentence.status !== 'interpreted' || !quote || !same(quote.pages, sentence.pages)
      || quote.textHash !== sentence.textHash || chars(quote.text).length !== sentence.end - sentence.start || sentence.index < lastIndex)
      fail('SEMANTIC_EVIDENCE_MISMATCH');
    lastIndex = sentence.index;
    const total = (claimsPerSentence.get(sentence.sentence_id) || 0) + 1;
    if (total > 8) fail('SEMANTIC_CLAIM_INVALID');
    claimsPerSentence.set(sentence.sentence_id, total);
    checkClaim(claim, sentence, quote, { contentChars: proof.contentChars });
    mapping.set('q-' + sha(`${section.contentHash}:${sentence.start}:${sentence.end}:${quote.text}`).slice(0, 24), quote.id);
  }
  if (!same(candidate.evidenceIdMap, [...mapping].map(([semanticEvidenceId, evidencePoolId]) => ({ semanticEvidenceId, evidencePoolId }))))
    fail('SEMANTIC_EVIDENCE_MAP_INVALID');
  const appended = candidate.pool.quotes.slice(candidate.pool.quotes.length - coverage.newQuoteCount);
  if (coverage.newQuoteCount > candidate.pool.quotes.length || appended.some(quote => ![...mapping.values()].includes(quote.id)))
    fail('SEMANTIC_UNREFERENCED_NEW_QUOTE');
}
function storedSidecar(candidate) {
  return { schemaVersion: 1, method: METHOD, status: 'structure-and-evidence-validated', poolRef: 'strategyEvidencePool',
    candidate: without(candidate, ['pool']), semanticCorrectnessVerified: false, semanticAnalysisComplete: false,
    privateSourceIndependentlyReparsed: false, productionReady: false, productionAdmission: 'serialized-admission-validated' };
}

// bindings require code, targetPeriodEnd and three trusted receipt digests:
// expectedInputDigest, expectedUsageHistoryDigest, expectedCandidateDigest.
// Do not construct these bindings by reading the candidate being admitted.
// Public sentence summaries omit private quotation context, so their original
// packet hash cannot be recomputed here; the receipt binds that mapper result.
function validateStrategySemanticCandidate(report, candidate, bindings = {}) {
  try {
    const clock = clockFor(bindings.now);
    if (!object(report) || clock === null || !keys(candidate, CANDIDATE_FIELDS)
      || candidate.schemaVersion !== 1 || candidate.kind !== 'strategy-semantic-pool-candidate' || candidate.method !== CANDIDATE_METHOD
      || candidate.status !== 'candidate-not-integrated' || typeof bindings.code !== 'string' || !/^\d{6}$/.test(bindings.code)
      || candidate.code !== bindings.code || !hash(bindings.expectedInputDigest) || candidate.inputDigest !== bindings.expectedInputDigest
      || !hash(candidate.candidateDigest) || digest(without(candidate, ['candidateDigest'])) !== candidate.candidateDigest
      || !hash(bindings.expectedCandidateDigest) || candidate.candidateDigest !== bindings.expectedCandidateDigest
      || !['baselinePoolDigest', 'resultPoolDigest', 'baselineSnapshotDigest', 'resultSnapshotDigest'].every(key => hash(candidate[key])))
      fail('SEMANTIC_CANDIDATE_BINDING_INVALID');
    if (candidate.sourceSectionComplete !== true || candidate.inputCoverageComplete !== true || candidate.selectedEvidenceOnly !== true
      || candidate.offsetUnit !== 'unicode-code-point' || candidate.spanOrigin !== 'sentence-relative'
      || candidate.sentenceRangeOrigin !== 'section-relative' || candidate.semanticCorrectnessVerified !== false
      || candidate.semanticAnalysisComplete !== false || candidate.productionReady !== false || candidate.productionAdmission !== 'not-connected'
      || candidate.managerAttribution !== 'fund-report-not-individual-speaker' || candidate.explainsIndividualStockTrades !== false)
      fail('SEMANTIC_CANDIDATE_SCOPE_INVALID');
    const proof = strategySectionCoverageFor(report.strategySectionCoverage, bindings.targetPeriodEnd);
    if (typeof bindings.targetPeriodEnd !== 'string' || !proof?.complete || proof.kind !== 'quarterly'
      || !same(proof, candidate.strategySectionCoverage) || !same(proof, report.strategySectionCoverage)
      || report.periodEnd !== proof.periodEnd) fail('SEMANTIC_SOURCE_PHASE_MISMATCH');
    if (!keys(candidate.sidecar, ['derivedFromObservedAt', 'retainedStrategyObservedAt', 'source', 'sourceIdentity', 'modelObservation'])
      || !keys(candidate.sidecar.source, SOURCE_FIELDS)) fail('SEMANTIC_SOURCE_INVALID');
    const sidecar = candidate.sidecar, source = sidecar.source, section = proof.sections[0];
    if (SOURCE_FIELDS.filter(key => !['retrievedAt', 'section', 'sectionContentHash', 'pages'].includes(key)).some(key => source[key] !== proof[key])
      || source.section !== section.section || source.sectionContentHash !== section.contentHash || !same(source.pages, section.pages))
      fail('SEMANTIC_SOURCE_HASH_MISMATCH');
    const pool = normalizeStrategyEvidencePool(candidate.pool, proof, {}, bindings.now);
    if (!pool || pool.digest !== candidate.resultPoolDigest || digest(pool) !== candidate.resultSnapshotDigest)
      fail('SEMANTIC_RESULT_POOL_INVALID');
    const oldTime = instant(pool.source.retrievedAt), newTime = instant(source.retrievedAt), retained = instant(report.strategyEvidenceRetrievedAt);
    if (oldTime === null || newTime === null || retained === null || retained > oldTime || oldTime > newTime || newTime > clock + 300000000n
      || sidecar.derivedFromObservedAt !== pool.source.retrievedAt || sidecar.retainedStrategyObservedAt !== report.strategyEvidenceRetrievedAt)
      fail('SEMANTIC_SOURCE_CLOCK_MISMATCH');
    const identity = normalizePortfolioIdentity(sidecar.sourceIdentity, { ...source, fundCode: bindings.code }, bindings.now);
    const oldIdentity = normalizePortfolioIdentity(report.strategyClaimsIdentityEvidence, { ...source, fundCode: bindings.code }, bindings.now);
    if (!identity || !oldIdentity || !same(identity, sidecar.sourceIdentity) || !same(oldIdentity, report.strategyClaimsIdentityEvidence)
      || identity.retrievedAt !== source.retrievedAt || oldIdentity.retrievedAt !== pool.source.retrievedAt
      || ['sourceTextHash', 'pdfSha256'].some(key => identity[key] !== oldIdentity[key])
      || !same(without(identity, ['retrievedAt']), without(oldIdentity, ['retrievedAt']))) fail('SEMANTIC_PDF_IDENTITY_CHANGED');
    checkHistory(candidate, clock, bindings);
    checkEvidence(candidate, proof);
    const baseline = clone(pool);
    baseline.quotes = baseline.quotes.slice(0, baseline.quotes.length - candidate.coverage.newQuoteCount);
    baseline.usedQuotes = baseline.quotes.length;
    baseline.usedQuoteChars = baseline.quotes.reduce((sum, quote) => sum + chars(quote.text).length, 0);
    baseline.digest = poolDigest(baseline);
    if (!normalizeStrategyEvidencePool(baseline, proof, {}, bindings.now) || baseline.digest !== candidate.baselinePoolDigest
      || digest(baseline) !== candidate.baselineSnapshotDigest) fail('SEMANTIC_BASELINE_PREFIX_CHANGED');
    const current = normalizeStrategyEvidencePool(report.strategyEvidencePool, proof, {}, bindings.now);
    if (!current) fail('SEMANTIC_BASELINE_BUDGET_INVALID');
    const currentDigest = digest(current), projected = storedSidecar(candidate);
    const already = currentDigest === candidate.resultSnapshotDigest && same(report.strategySemantics, projected);
    if (currentDigest !== candidate.baselineSnapshotDigest && !already) fail('SEMANTIC_CONCURRENT_POOL_CHANGED');
    if (strategyEvidenceBudgetFor(report, bindings.now).status !== 'verified') fail('SEMANTIC_BASELINE_BUDGET_INVALID');
    if (report.strategySemantics !== undefined && !already) {
      const previous = report.strategySemantics?.candidate;
      if (!previous || !same(report.strategySemantics, storedSidecar({ ...previous, pool: current }))
        || previous.resultSnapshotDigest !== currentDigest
        || !Array.isArray(previous.usageHistory) || previous.usageHistory.length >= candidate.usageHistory.length
        || !same(candidate.usageHistory.slice(0, previous.usageHistory.length), previous.usageHistory)) fail('SEMANTIC_HISTORY_RESET');
      // Recheck the retained serialized sidecar as an idempotent replay before
      // trusting its ledger prefix. The exact sidecar match above guarantees
      // this nested call takes the already-applied branch and cannot recurse.
      try {
        validateStrategySemanticCandidate(report, { ...previous, pool: current }, { ...bindings,
          expectedCandidateDigest: previous.candidateDigest, expectedInputDigest: previous.inputDigest,
          expectedUsageHistoryDigest: previous.usageAccounting?.historyDigest });
      } catch { fail('SEMANTIC_RETAINED_SIDECAR_INVALID'); }
    }
    const nextReport = { ...report, strategyEvidencePool: pool };
    const budget = strategyEvidenceBudgetFor(nextReport, bindings.now);
    if (budget.status !== 'verified') fail('SEMANTIC_EXISTING_PROJECTION_CHANGED');
    return { status: already ? 'already-applied' : 'validated', candidate: clone(candidate), sidecar: projected, budget };
  } catch (error) {
    if (error instanceof SemanticAdmissionError) throw error;
    fail('SEMANTIC_CANDIDATE_INVALID');
  }
}

function applyStrategySemanticCandidate(report, candidate, bindings) {
  const checked = validateStrategySemanticCandidate(report, candidate, bindings);
  if (checked.status === 'already-applied') return { status: 'already-applied', report: clone(report) };
  // Only the canonical pool, its derived budget and this separate lane change.
  // Finite claims, original source clocks and all unrelated report fields survive.
  return { status: 'applied-in-memory', report: { ...clone(report), strategyEvidencePool: checked.candidate.pool,
    strategyEvidenceBudget: checked.budget, strategySemantics: checked.sidecar } };
}

// Only for an already persisted report. New candidates must use the external
// mapper receipt required by applyStrategySemanticCandidate above.
function retainedStrategySemanticsFor(report, { code, now = new Date() } = {}) {
  try {
    const candidate = report?.strategySemantics?.candidate;
    if (!candidate || typeof code !== 'string' || !/^\d{6}$/.test(code)) return null;
    if (code !== candidate.code && !normalizePortfolioIdentity(candidate.sidecar?.sourceIdentity,
      { ...candidate.sidecar?.source, fundCode: code }, now)) return null;
    const checked = validateStrategySemanticCandidate(report, { ...candidate, pool: report.strategyEvidencePool }, {
      code: candidate.code, targetPeriodEnd: report.periodEnd, now, expectedInputDigest: candidate.inputDigest,
      expectedUsageHistoryDigest: candidate.usageAccounting?.historyDigest, expectedCandidateDigest: candidate.candidateDigest });
    return checked.status === 'already-applied' ? clone(report.strategySemantics) : null;
  } catch { return null; }
}

function retainStrategySemantics(target, source, options = {}) {
  if (retainedStrategySemanticsFor(target, options)) return false;
  const retained = retainedStrategySemanticsFor(source, options);
  if (!retained || !same(target?.strategySectionCoverage, source.strategySectionCoverage)
    || !target.strategyEvidencePool?.source || !same(without(target.strategyEvidencePool.source, ['retrievedAt']),
      without(source.strategyEvidencePool.source, ['retrievedAt']))) return false;
  const oldQuotes = target.strategyEvidencePool?.quotes;
  if (!Array.isArray(oldQuotes) || !oldQuotes.every(quote => source.strategyEvidencePool.quotes.some(other => same(quote, other)))) return false;
  const projected = { ...target, strategyEvidencePool: source.strategyEvidencePool, strategySemantics: retained };
  const clockFields = ['strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyClaimsDerivedFromObservedAt', 'strategyEvidenceRetrievedAt'];
  const differentClock = !same(target.strategyEvidencePool.source, source.strategyEvidencePool.source)
    || target.strategyEvidenceRetrievedAt !== source.strategyEvidenceRetrievedAt;
  if (differentClock) {
    // A later fetch of the identical source can reproduce the same finite
    // claims. Retain the older, actually model-analysed strategy observation as
    // a group; never relabel model evidence with the later fetch time.
    const facts = report => report.strategyClaims && { ...report.strategyClaims,
      source: without(report.strategyClaims.source, ['retrievedAt']) };
    if (!target.strategyClaims?.source || !source.strategyClaims?.source || !same(facts(target), facts(source))) return false;
    for (const key of clockFields) {
      if (Object.hasOwn(source, key)) projected[key] = source[key];
      else delete projected[key];
    }
  }
  if (!retainedStrategySemanticsFor(projected, options) || strategyEvidenceBudgetFor(projected, options.now).status !== 'verified') return false;
  target.strategyEvidencePool = clone(source.strategyEvidencePool);
  target.strategyEvidenceBudget = strategyEvidenceBudgetFor(projected, options.now);
  target.strategySemantics = retained;
  if (differentClock) for (const key of clockFields) {
    if (Object.hasOwn(projected, key)) target[key] = clone(projected[key]);
    else delete target[key];
  }
  return true;
}

module.exports = { validateStrategySemanticCandidate, applyStrategySemanticCandidate, retainedStrategySemanticsFor,
  retainStrategySemantics, SemanticAdmissionError, METHOD };
