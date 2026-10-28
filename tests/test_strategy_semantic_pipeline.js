'use strict';

// Small cross-language fixtures use the real Python mapper; no requests or PDFs.
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { canonical } = require('../lib/strategy-claim-patches');
const { strategyEvidenceBudgetFor } = require('../lib/strategy-evidence-pool');
const { validateStrategySemanticCandidate: validate, applyStrategySemanticCandidate: apply } = require('../lib/strategy-semantics');
const sha = text => crypto.createHash('sha256').update(text).digest('hex');
const digest = value => sha(canonical(value));
const copy = value => JSON.parse(JSON.stringify(value));
const omit = (value, fields) => Object.fromEntries(Object.entries(value).filter(([key]) => !fields.includes(key)));
const fixtureProcess = spawnSync('python3', ['-c', `
import copy, json, sys
sys.path[:0] = ['scripts', 'tests']
from test_strategy_semantic_pool import case, mapped, observations, NOW
from test_strategy_semantics import claim, span, response
import strategy_semantics as semantic
import strategy_semantic_pool as mapper
def fixture(body='本基金看好科技行业。市场保持平稳。', old=(1,), change=None):
    report, packet, identity = case(body, old_indices=old)
    c = claim(packet.model_input()['sentences'][0]['text'])
    if change: change(c, packet.model_input()['sentences'][0]['text'])
    return {'report': report, 'candidate': mapped(report, packet, identity, {0: [c]}), 'now': NOW}
normal = fixture()
reused = fixture(old=(0,))
unicode = fixture('😀本基金看好科技行业。市场保持平稳。')
def future(c, t): c['time'] = 'future'
future_case = fixture('本基金未来看好科技行业。市场保持平稳。', change=future)
def negated(c, t):
    c['polarity'] = 'negative'; c['negation'] = 'negated'; c['negation_spans'] = [span(t, '不')]
negative = fixture('本基金不看好科技行业。市场保持平稳。', change=negated)
def conditional(c, t):
    c['conditional'] = True; c['time'] = 'conditional'; c['condition_spans'] = [span(t, '如果')]
condition = fixture('如果市场改善，本基金看好科技行业。市场保持平稳。', change=conditional)
report, packet, identity = case('由于需求增长，本基金增配科技行业。市场保持平稳。', old_indices=(1,))
t = packet.model_input()['sentences'][0]['text']; c = claim(t, predicate='增配'); c['kind'] = 'reason'
c['reason'] = {'status': 'explicit', 'cause': span(t, '需求增长'), 'connective': span(t, '由于'), 'operation': span(t, '本基金增配科技行业')}
reason = {'report': report, 'candidate': mapped(report, packet, identity, {0: [c]}), 'now': NOW}
report, packet, identity = case(old_indices=(0, 1))
raw = response(packet, {0: [claim(packet.model_input()['sentences'][0]['text'])]})
model, history = observations(packet, raw)
extra = copy.deepcopy(history[-1]); extra.update(requestDigest='7' * 64,
    attemptedAt='2026-10-26T20:07:00.123456+00:00', finishedAt='2026-10-26T20:07:05.123456+00:00')
history.append(extra); model['observation'] = copy.deepcopy(extra)
# The retained first result stores the old quote first, then the newly mapped
# quote, so construct the exact same order for a successive CAS candidate.
report['strategyEvidencePool'] = copy.deepcopy(normal['candidate']['pool'])
next_candidate = mapped(report, packet, identity, {0: [claim(packet.model_input()['sentences'][0]['text'])]},
    usage_history=history, model_observation=model, expected_usage_history_digest=mapper.canonical_digest(history))
print(json.dumps({'normal': normal, 'reused': reused, 'unicode': unicode, 'future': future_case,
    'negative': negative, 'condition': condition, 'reason': reason, 'nextCandidate': next_candidate}, ensure_ascii=False))
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8', maxBuffer: 300000, timeout: 20000 });
assert.equal(fixtureProcess.status, 0, 'synthetic mapper fixtures must succeed');
const fixtures = JSON.parse(fixtureProcess.stdout);
function bindings(fixture, candidate = fixture.candidate) {
  return { code: '000001', targetPeriodEnd: '2026-06-30', now: fixture.now,
    expectedInputDigest: candidate.inputDigest, expectedUsageHistoryDigest: candidate.usageAccounting.historyDigest,
    expectedCandidateDigest: candidate.candidateDigest };
}
function rehash(candidate) { candidate.candidateDigest = digest(omit(candidate, ['candidateDigest'])); return candidate; }
function mutate(fixture, callback) { const candidate = copy(fixture.candidate); callback(candidate); return rehash(candidate); }
function rejected(fixture, callback, code, options) {
  const candidate = mutate(fixture, callback);
  assert.throws(() => validate(fixture.report, candidate, options || { ...bindings(fixture), expectedCandidateDigest: candidate.candidateDigest }), { code });
}
function refreshPool(candidate) {
  const pool = candidate.pool;
  pool.usedQuotes = pool.quotes.length;
  pool.usedQuoteChars = pool.quotes.reduce((n, quote) => n + Array.from(quote.text).length, 0);
  pool.digest = digest({ schemaVersion: 1, source: omit(pool.source, ['retrievedAt']), quotes: pool.quotes });
  candidate.resultPoolDigest = pool.digest; candidate.resultSnapshotDigest = digest(pool);
}

test('Python candidate admits to one shared pool, keeps all old facts and clocks, and replays without changes', () => {
  const fixture = fixtures.normal, before = copy(fixture);
  const result = apply(fixture.report, fixture.candidate, bindings(fixture));
  assert.equal(result.status, 'applied-in-memory');
  assert.deepEqual(fixture, before);
  assert.deepEqual(omit(result.report, ['strategyEvidencePool', 'strategyEvidenceBudget', 'strategySemantics']),
    omit(fixture.report, ['strategyEvidencePool', 'strategyEvidenceBudget', 'strategySemantics']));
  assert.deepEqual(result.report.strategyEvidencePool, fixture.candidate.pool);
  assert.deepEqual(result.report.strategyEvidencePool.quotes.slice(0, 1), fixture.report.strategyEvidencePool.quotes);
  assert.equal(result.report.strategyEvidencePool.source.retrievedAt, fixture.report.strategyEvidencePool.source.retrievedAt);
  assert.equal(result.report.strategySemantics.candidate.sidecar.source.retrievedAt, fixture.candidate.sidecar.source.retrievedAt);
  assert.deepEqual(result.report.strategySemantics.candidate.usageHistory, fixture.candidate.usageHistory);
  assert.equal(strategyEvidenceBudgetFor(result.report, fixture.now).status, 'verified');
  assert.equal(result.report.strategySemantics.productionReady, false);
  assert.equal(result.report.strategySemantics.semanticCorrectnessVerified, false);
  assert.equal(result.report.strategySemantics.privateSourceIndependentlyReparsed, false);
  assert.equal(Object.hasOwn(result.report.strategySemantics.candidate, 'pool'), false);
  assert.equal(Object.hasOwn(result.report.strategySemantics.candidate, 'evidence'), false);
  const quote = '本基金看好科技行业。';
  assert.equal(JSON.stringify(result.report.strategySemantics).includes(quote), false);
  assert.deepEqual(apply(result.report, fixture.candidate, bindings(fixture)), { status: 'already-applied', report: result.report });
});

test('a reused quote still attaches the separate lane and repeated apply is idempotent', () => {
  const fixture = fixtures.reused;
  assert.equal(fixture.candidate.coverage.newQuoteCount, 0);
  const result = apply(fixture.report, fixture.candidate, bindings(fixture));
  assert.equal(result.status, 'applied-in-memory');
  assert.deepEqual(result.report.strategyEvidencePool, fixture.report.strategyEvidencePool);
  assert.equal(apply(result.report, fixture.candidate, bindings(fixture)).status, 'already-applied');
});

test('unicode scalar offsets are checked against literal text, including supplementary characters', () => {
  const fixture = fixtures.unicode;
  assert.equal(validate(fixture.report, fixture.candidate, bindings(fixture)).status, 'validated');
  assert.equal(fixture.candidate.claims[0].subject.span.start, 1);
  rejected(fixture, candidate => { candidate.claims[0].subject.span.start = 2; }, 'SEMANTIC_NONLITERAL_SPAN');
});

test('trusted input, usage receipt, product and quarter bindings are mandatory', () => {
  const fixture = fixtures.normal;
  for (const field of ['code', 'targetPeriodEnd', 'expectedInputDigest', 'expectedUsageHistoryDigest', 'expectedCandidateDigest']) {
    const options = bindings(fixture); delete options[field];
    assert.throws(() => validate(fixture.report, fixture.candidate, options));
  }
  for (const changed of [{ code: '000003' }, { targetPeriodEnd: '2026-09-30' },
    { expectedInputDigest: 'a'.repeat(64) }, { expectedUsageHistoryDigest: 'a'.repeat(64) },
    { expectedCandidateDigest: 'a'.repeat(64) }]) {
    assert.throws(() => validate(fixture.report, fixture.candidate, { ...bindings(fixture), ...changed }));
  }
});

test('candidate digest, source hash, physical pages, identity and closed schema are independently checked', () => {
  const fixture = fixtures.normal, bad = copy(fixture.candidate);
  bad.claims[0].polarity = 'negative';
  assert.throws(() => validate(fixture.report, bad, bindings(fixture)), { code: 'SEMANTIC_CANDIDATE_BINDING_INVALID' });
  rejected(fixture, candidate => { candidate.sidecar.source.sourceTextHash = 'a'.repeat(64); }, 'SEMANTIC_SOURCE_HASH_MISMATCH');
  rejected(fixture, candidate => { candidate.sidecar.source.sectionContentHash = 'a'.repeat(64); }, 'SEMANTIC_SOURCE_HASH_MISMATCH');
  rejected(fixture, candidate => { candidate.sidecar.source.pages = [9]; }, 'SEMANTIC_SOURCE_HASH_MISMATCH');
  rejected(fixture, candidate => { candidate.sidecar.sourceIdentity.pdfSha256 = 'a'.repeat(64); }, 'SEMANTIC_PDF_IDENTITY_CHANGED');
  rejected(fixture, candidate => { candidate.sidecar.sourceIdentity.rawText = 'private'; }, 'SEMANTIC_PDF_IDENTITY_CHANGED');
  rejected(fixture, candidate => { candidate.rawPacket = { text: 'private' }; }, 'SEMANTIC_CANDIDATE_BINDING_INVALID');
  rejected(fixture, candidate => { candidate.claims[0].subject.span.text = '本基金'; }, 'SEMANTIC_NONLITERAL_SPAN');
  rejected(fixture, candidate => { candidate.productionReady = true; }, 'SEMANTIC_CANDIDATE_SCOPE_INVALID');
  rejected(fixture, candidate => { candidate.claims[0].provesTradeReason = true; }, 'SEMANTIC_CLAIM_INVALID');
});

test('source and pool timestamps retain microsecond precision and cannot be refreshed by a model result', () => {
  const fixture = fixtures.normal;
  rejected(fixture, candidate => { candidate.sidecar.source.retrievedAt = '2026-10-26T19:10:00.123455+00:00'; },
    'SEMANTIC_SOURCE_CLOCK_MISMATCH');
  rejected(fixture, candidate => { candidate.sidecar.retainedStrategyObservedAt = candidate.sidecar.derivedFromObservedAt; },
    'SEMANTIC_SOURCE_CLOCK_MISMATCH');
  rejected(fixture, candidate => { candidate.pool.source.retrievedAt = candidate.sidecar.source.retrievedAt; refreshPool(candidate); },
    'SEMANTIC_SOURCE_CLOCK_MISMATCH');
  rejected(fixture, candidate => { candidate.sidecar.source.retrievedAt = '2026-10-27T02:05:00.000001Z'; },
    'SEMANTIC_SOURCE_CLOCK_MISMATCH');
});

test('CAS rejects concurrently changed quote facts or observation clocks and never overwrites them', () => {
  const fixture = fixtures.normal, report = copy(fixture.report);
  report.strategyEvidencePool.source.retrievedAt = '2026-10-26T19:10:00.123457+00:00';
  const before = copy(report);
  assert.throws(() => apply(report, fixture.candidate, bindings(fixture)), { code: 'SEMANTIC_CONCURRENT_POOL_CHANGED' });
  assert.deepEqual(report, before);
  const detachedPool = copy(fixture.report); detachedPool.strategyEvidencePool = copy(fixture.candidate.pool);
  assert.throws(() => apply(detachedPool, fixture.candidate, bindings(fixture)), { code: 'SEMANTIC_CONCURRENT_POOL_CHANGED' });
});

test('changing or removing an old quote is rejected even after attacker recomputes pool digests', () => {
  const fixture = fixtures.normal;
  rejected(fixture, candidate => {
    candidate.pool.quotes[0].text = '原有摘录不能替换。';
    const quote = candidate.pool.quotes[0]; quote.textHash = sha(quote.text);
    quote.id = 'q' + sha(`${candidate.pool.source.sourceTextHash}|${quote.textHash}|${quote.pages.join(',')}`);
    refreshPool(candidate);
  }, 'SEMANTIC_BASELINE_PREFIX_CHANGED');
  rejected(fixture, candidate => { candidate.pool.quotes.shift(); refreshPool(candidate); }, 'SEMANTIC_BASELINE_PREFIX_CHANGED');
  rejected(fixture, candidate => { candidate.pool.quotes.reverse(); refreshPool(candidate); }, 'SEMANTIC_UNREFERENCED_NEW_QUOTE');
});

test('sentence coverage, selected quote map and sentence ranges cannot be relabeled', () => {
  const fixture = fixtures.normal;
  rejected(fixture, candidate => { candidate.sentenceStatuses[0].textHash = 'a'.repeat(64); }, 'SEMANTIC_SENTENCE_MAP_INVALID');
  rejected(fixture, candidate => { candidate.sentenceStatuses[0].pages = [9]; }, 'SEMANTIC_SENTENCE_MAP_INVALID');
  rejected(fixture, candidate => { candidate.coverage.unknown++; }, 'SEMANTIC_COVERAGE_INVALID');
  rejected(fixture, candidate => { candidate.claims[0].sentenceRange.start++; }, 'SEMANTIC_CLAIM_INVALID');
  rejected(fixture, candidate => { candidate.evidenceIdMap[0].semanticEvidenceId = 'q-' + 'a'.repeat(24); }, 'SEMANTIC_EVIDENCE_MAP_INVALID');
  rejected(fixture, candidate => { candidate.claims[0].evidencePoolId = candidate.pool.quotes[0].id; }, 'SEMANTIC_EVIDENCE_MISMATCH');
});

test('usage retains failed and unknown attempts, disallows reset, and checks token arithmetic', () => {
  const fixture = fixtures.normal;
  assert.equal(validate(fixture.report, fixture.candidate, bindings(fixture)).candidate.usageHistory.length, 6);
  rejected(fixture, candidate => { candidate.usageHistory.shift(); }, 'SEMANTIC_HISTORY_BINDING_INVALID');
  const candidate = mutate(fixture, value => {
    value.usageHistory[0].outcome = 'failed'; value.usageHistory[0].responseDigest = null; value.usageHistory[0].usage = null;
    value.usageAccounting.historyDigest = digest(value.usageHistory); value.usageAccounting.unknownUsageAttempts = 1;
    value.usageAccounting.knownTotalTokens -= 1000;
  });
  assert.equal(validate(fixture.report, candidate, bindings(fixture, candidate)).status, 'validated');
  candidate.usageHistory[1].usage.total_tokens++;
  candidate.usageAccounting.historyDigest = digest(candidate.usageHistory); rehash(candidate);
  assert.throws(() => validate(fixture.report, candidate, bindings(fixture, candidate)), { code: 'SEMANTIC_USAGE_INVALID' });
});

test('successive candidate preserves the accepted usage prefix and verifies the stored sidecar', () => {
  const fixture = fixtures.normal, next = fixtures.nextCandidate;
  const first = apply(fixture.report, fixture.candidate, bindings(fixture)).report;
  const accepted = apply(first, next, bindings(fixture, next));
  assert.equal(accepted.status, 'applied-in-memory');
  assert.equal(accepted.report.strategySemantics.candidate.usageHistory.length, 7);
  assert.deepEqual(accepted.report.strategySemantics.candidate.usageHistory.slice(0, 6), fixture.candidate.usageHistory);
  assert.deepEqual(accepted.report.strategyEvidencePool, first.strategyEvidencePool);
  assert.equal(apply(accepted.report, next, bindings(fixture, next)).status, 'already-applied');

  const reset = copy(next); reset.usageHistory.shift();
  reset.usageAccounting.historyDigest = digest(reset.usageHistory);
  reset.usageAccounting.attemptCount--;
  reset.usageAccounting.knownTotalTokens -= 1000; rehash(reset);
  assert.throws(() => apply(first, reset, bindings(fixture, reset)), { code: 'SEMANTIC_HISTORY_RESET' });

  const altered = copy(first);
  altered.strategySemantics.candidate.claims[0].polarity = 'negative';
  assert.throws(() => apply(altered, next, bindings(fixture, next)), { code: 'SEMANTIC_RETAINED_SIDECAR_INVALID' });
  assert.throws(() => apply(first, fixture.candidate, { ...bindings(fixture), expectedCandidateDigest: '0'.repeat(64) }),
    { code: 'SEMANTIC_CANDIDATE_BINDING_INVALID' });
});

test('self-rehashing a hidden sentence cannot bypass the trusted candidate receipt', () => {
  const fixture = fixtures.normal;
  const changed = mutate(fixture, candidate => {
    const row = candidate.sentenceStatuses[1]; row.textHash = sha('另一句话。');
    const sourceDigest = digest(omit(candidate.sidecar.source, ['retrievedAt']));
    row.sentence_id = 's-' + sha(`${sourceDigest}:${row.index}:${row.textHash}`).slice(0, 24);
  });
  assert.throws(() => apply(fixture.report, changed, bindings(fixture)), { code: 'SEMANTIC_CANDIDATE_BINDING_INVALID' });
});

test('a newly invented quote cannot enter through internally consistent self-signed hashes', () => {
  const fixture = fixtures.normal;
  const changed = mutate(fixture, candidate => {
    const quote = candidate.pool.quotes.at(-1), sentence = candidate.sentenceStatuses[0], claim = candidate.claims[0];
    quote.text = quote.text.replace('科技', '医药'); quote.textHash = sha(quote.text);
    quote.id = 'q' + sha(`${candidate.pool.source.sourceTextHash}|${quote.textHash}|${quote.pages.join(',')}`);
    const sourceDigest = digest(omit(candidate.sidecar.source, ['retrievedAt']));
    sentence.textHash = quote.textHash;
    sentence.sentence_id = 's-' + sha(`${sourceDigest}:${sentence.index}:${sentence.textHash}`).slice(0, 24);
    claim.sentence_id = sentence.sentence_id; claim.evidencePoolId = quote.id;
    claim.sentenceRange.textHash = quote.textHash; claim.object.span.textHash = sha('医药行业');
    candidate.evidenceIdMap = [{ semanticEvidenceId: 'q-' + sha(`${candidate.sidecar.source.sectionContentHash}:${sentence.start}:${sentence.end}:${quote.text}`).slice(0, 24), evidencePoolId: quote.id }];
    refreshPool(candidate);
  });
  assert.equal(changed.inputDigest, fixture.candidate.inputDigest);
  assert.equal(changed.usageAccounting.historyDigest, fixture.candidate.usageAccounting.historyDigest);
  assert.throws(() => apply(fixture.report, changed, bindings(fixture)), { code: 'SEMANTIC_CANDIDATE_BINDING_INVALID' });
});

test('one shared evidence cap also applies to newly appended semantic quotes', () => {
  const fixture = fixtures.normal;
  rejected(fixture, candidate => {
    const quote = candidate.pool.quotes.at(-1);
    quote.text = '字'.repeat(161); quote.textHash = sha(quote.text);
    quote.id = 'q' + sha(`${candidate.pool.source.sourceTextHash}|${quote.textHash}|${quote.pages.join(',')}`);
    refreshPool(candidate);
  }, 'SEMANTIC_RESULT_POOL_INVALID');
  rejected(fixture, candidate => { candidate.pool.maxQuotes = 9; refreshPool(candidate); }, 'SEMANTIC_RESULT_POOL_INVALID');
});

test('failed or pending final receipt cannot become an admitted result', () => {
  const fixture = fixtures.normal;
  for (const outcome of ['unknown', 'failed', 'rejected', 'started']) {
    const candidate = mutate(fixture, value => {
      value.usageHistory.at(-1).outcome = outcome;
      if (outcome === 'started') value.usageHistory.at(-1).finishedAt = null;
      value.sidecar.modelObservation.observation = copy(value.usageHistory.at(-1));
      value.usageAccounting.historyDigest = digest(value.usageHistory);
    });
    assert.throws(() => validate(fixture.report, candidate, bindings(fixture, candidate)), { code: 'SEMANTIC_MODEL_OBSERVATION_MISMATCH' });
  }
});

test('negation, conditional, future and literal causal scope survive the independent boundary', () => {
  for (const name of ['future', 'negative', 'condition', 'reason']) {
    const fixture = fixtures[name]; assert.equal(validate(fixture.report, fixture.candidate, bindings(fixture)).status, 'validated');
  }
  rejected(fixtures.future, candidate => { candidate.claims[0].time = 'past'; }, 'SEMANTIC_QUALIFIER_CHANGED');
  rejected(fixtures.negative, candidate => { candidate.claims[0].negation = 'affirmed'; }, 'SEMANTIC_QUALIFIER_CHANGED');
  rejected(fixtures.condition, candidate => { candidate.claims[0].condition_spans = []; }, 'SEMANTIC_QUALIFIER_CHANGED');
  rejected(fixtures.reason, candidate => { candidate.claims[0].object.type = 'security-mention'; }, 'SEMANTIC_CAUSAL_SCOPE_UNPROVEN');
  rejected(fixtures.reason, candidate => { candidate.claims[0].reason.status = 'unknown'; }, 'SEMANTIC_REASON_INVALID');
});

test('legacy unpooled evidence blocks admission instead of being silently fixed or erased', () => {
  const fixture = fixtures.normal, report = copy(fixture.report);
  report.strategyExcerpts[0].text = 'unrelated';
  assert.throws(() => apply(report, fixture.candidate, bindings(fixture)), { code: 'SEMANTIC_BASELINE_BUDGET_INVALID' });
});
