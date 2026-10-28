"""Private semantic output to bounded canonical quote-pool candidates.

No provider, credential, filesystem or network access. This is deliberately not
a production admission path. The caller keeps the resolved model JSON in memory
and supplies the complete pre-existing usage history with its expected digest.
Only selected quotations occur in the returned pool; claim spans carry offsets
and hashes, never another copy of their text. Machine checks do not prove truth.
"""
from __future__ import annotations

import copy
import datetime as dt
import re

import strategy_semantics as semantic
import strategy_evidence_pool as pool_api
from portfolio_identity import normalize_portfolio_identity
from strategy_sections import normalize_strategy_section_coverage

METHOD = 'semantic-canonical-pool-candidate-v1'
MAX_HISTORY = 64
HASH = re.compile(r'[a-f0-9]{64}\Z')
HISTORY_FIELDS = {'requestDigest', 'inputDigest', 'attemptedAt', 'finishedAt', 'outcome', 'responseDigest', 'usage'}
MODEL_FIELDS = {'provider', 'model', 'responseModel', 'promptVersion', 'boundResponseDigest', 'observation'}


class MappingError(ValueError):
    pass


def _fail(code):
    raise MappingError(code)


def canonical_digest(value):
    return semantic._hash(semantic._canonical(value))


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _same_keys(value, keys):
    return type(value) is dict and set(value) == set(keys)


def _locations(text, quote, positions, pages):
    """Count overlapping exact occurrences too; never choose the first match."""
    result, offset = [], 0
    while True:
        start = text.find(quote, offset)
        if start < 0:
            break
        if list(dict.fromkeys(positions[start:start + len(quote)])) == pages:
            result.append(start)
            if len(result) > 1:
                break
        offset = start + 1
    return result


def _semantic_id(source, start, end, text):
    return 'q-' + semantic._hash(f"{source['sectionContentHash']}:{start}:{end}:{text}")[:24]


def validate_usage_history(history, expected_digest, now, *, allow_empty=False):
    """Validate preserved accounting before another explicit bounded attempt."""
    if (type(history) is not list or not (0 if allow_empty else 1) <= len(history) <= MAX_HISTORY
            or not _hash(expected_digest) or canonical_digest(history) != expected_digest):
        _fail('MODEL_HISTORY_BINDING_INVALID')
    seen, previous, tokens, unknown = set(), None, 0, 0
    for row in history:
        if not _same_keys(row, HISTORY_FIELDS):
            _fail('MODEL_HISTORY_INVALID')
        start, end = semantic._instant(row['attemptedAt']), semantic._instant(row['finishedAt'])
        if (not _hash(row['requestDigest']) or not _hash(row['inputDigest']) or not start
                or start > now + dt.timedelta(minutes=5) or previous is not None and start < previous
                or row['outcome'] not in {'started', 'unknown', 'failed', 'rejected', 'validated'}
                or row['outcome'] == 'started' and row['finishedAt'] is not None
                or row['outcome'] != 'started' and (not end or not start <= end <= now + dt.timedelta(minutes=5))
                or row['responseDigest'] is not None and not _hash(row['responseDigest'])):
            _fail('MODEL_HISTORY_INVALID')
        key = (row['requestDigest'], start)
        if key in seen:
            _fail('MODEL_HISTORY_DUPLICATE_ATTEMPT')
        seen.add(key); previous = start
        usage = row['usage']
        if usage is None:
            unknown += 1
        elif (not _same_keys(usage, {'prompt_tokens', 'completion_tokens', 'total_tokens'})
                or any(type(value) is not int or not 0 <= value <= 2_000_000 for value in usage.values())
                or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']):
            _fail('MODEL_USAGE_INVALID')
        else:
            tokens += usage['total_tokens']
    return {'historyDigest': expected_digest, 'attemptCount': len(history), 'knownTotalTokens': tokens,
            'unknownUsageAttempts': unknown, 'accounting': 'read-only-supplied-history-no-reset',
            'externalReceiptsIndependentlyVerified': False}


def _validate_history(history, expected_digest, model, packet, now):
    usage = validate_usage_history(history, expected_digest, now)
    if (not _same_keys(model, MODEL_FIELDS)
            or model['provider'] != 'deepseek' or model['model'] != 'deepseek-flash'
            or model['responseModel'] != model['model'] or not _hash(model['boundResponseDigest'])
            or not isinstance(model['promptVersion'], str)
            or not re.fullmatch(r'source-bound-deepseek-v[1-9][0-9]{0,2}', model['promptVersion'])):
        _fail('MODEL_HISTORY_BINDING_INVALID')
    current = model['observation']
    if (current != history[-1] or current['outcome'] != 'validated' or not _hash(current['responseDigest'])
            or current['inputDigest'] != packet['inputDigest']
            or semantic._instant(current['attemptedAt']) < semantic._instant(packet['source']['retrievedAt'])):
        _fail('MODEL_OBSERVATION_MISMATCH')
    return usage


def _validate_source(report, packet, identity, code, now):
    if (not isinstance(report, dict) or not isinstance(packet, semantic.PrivateSemanticPacket)
            or dict(packet) != packet._manifest or not packet.get('sourceSectionComplete')
            or not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)):
        _fail('PRIVATE_SOURCE_REQUIRED')
    proof = normalize_strategy_section_coverage(report.get('strategySectionCoverage'))
    if (not proof or not proof['complete'] or proof['kind'] != 'quarterly'
            or pool_api.strategy_evidence_budget_for(report, now)['status'] != 'verified'):
        _fail('BASELINE_POOL_MIGRATION_REQUIRED')
    pool = pool_api.normalize_strategy_evidence_pool(report['strategyEvidencePool'], proof, now=now)
    source, section = packet['source'], proof['sections'][0]
    if (any(source.get(key) != pool['source'][key] for key in pool_api.SOURCE_FIELDS if key != 'retrievedAt')
            or source.get('sectionContentHash') != section['contentHash'] or source.get('section') != section['section']
            or source.get('pages') != section['pages'] or semantic._hash(packet._text) != section['contentHash']
            or len(packet._positions) != len(packet._text) or len(packet._sentences) != proof['sentenceCount']
            or any(type(page) is not int or page not in section['pages'] for page in packet._positions)):
        _fail('COMPLETE_SECTION_BINDING_MISMATCH')
    old_time = semantic._instant(pool['source']['retrievedAt'])
    new_time = semantic._instant(source.get('retrievedAt'))
    if not old_time or not new_time or not old_time <= new_time <= now + dt.timedelta(minutes=5):
        _fail('SOURCE_OBSERVATION_MISMATCH')
    clean_identity = normalize_portfolio_identity(identity, {**source, 'fundCode': code}, now=now)
    if not clean_identity or clean_identity['retrievedAt'] != source['retrievedAt']:
        _fail('QUARTER_PDF_IDENTITY_REQUIRED')
    # These hashes have different join conventions; compare each only to its
    # own proof role. Never compare identity.sourceTextHash with section hash.
    old_identity = report.get('strategyClaimsIdentityEvidence')
    old = normalize_portfolio_identity(old_identity, {**source, 'fundCode': code}, now=now)
    if not old:
        _fail('BASELINE_QUARTER_IDENTITY_MIGRATION_REQUIRED')
    if (old['retrievedAt'] != pool['source']['retrievedAt']
            or old['sourceTextHash'] != clean_identity['sourceTextHash'] or old['pdfSha256'] != clean_identity['pdfSha256']):
        _fail('PDF_IDENTITY_CHANGED')
    contexts = semantic._quote_context(packet._text)
    for sentence in packet._sentences:
        start, end = sentence['start'], sentence['end']
        if (packet._text[start:end] != sentence['text'] or sentence['textHash'] != semantic._hash(sentence['text'])
                or sentence['pages'] != list(dict.fromkeys(packet._positions[start:end]))
                or sentence['quoted_context'] != [row for row in contexts if row['start'] < end and row['end'] > start]):
            _fail('PRIVATE_SENTENCE_BINDING_MISMATCH')
    return pool, proof, clean_identity


def _existing_map(pool, packet):
    existing = {row['id']: row for row in packet._existing}
    if len(existing) != len(packet._existing) or len(existing) != len(pool['quotes']):
        _fail('BASELINE_QUOTE_SET_MISMATCH')
    mapping = {}
    for quote in pool['quotes']:
        found = _locations(packet._text, quote['text'], packet._positions, quote['pages'])
        if len(found) != 1:
            _fail('BASELINE_LITERAL_AMBIGUOUS_OR_MISSING')
        start, end = found[0], found[0] + len(quote['text'])
        key = _semantic_id(packet['source'], start, end, quote['text'])
        expected = {'id': key, 'text': quote['text'], 'textHash': quote['textHash'], 'start': start, 'end': end, 'pages': quote['pages']}
        if existing.get(key) != expected:
            _fail('BASELINE_QUOTE_SET_MISMATCH')
        mapping[key] = quote['id']
    if set(mapping) != set(existing):
        _fail('BASELINE_QUOTE_SET_MISMATCH')
    return mapping


def _span_refs(value):
    if isinstance(value, dict):
        if set(value) == {'start', 'end', 'text'}:
            return {'start': value['start'], 'end': value['end'], 'textHash': semantic._hash(value['text'])}
        return {key: _span_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_span_refs(item) for item in value]
    return copy.deepcopy(value)


class SemanticPoolCandidate(dict):
    """Bounded serializable candidate; its in-process attestation is not public."""
    def __init__(self, value):
        super().__init__(value)
        self._manifest = copy.deepcopy(value)


def map_strategy_semantic_pool(retained_report, packet, resolved_response, *, code, source_identity,
                               model_observation, usage_history, expected_baseline_digest,
                               expected_usage_history_digest, now=None):
    """Build, never persist, a shared-pool candidate from a private strict result.

``resolved_response`` is the literal binder's JSON, still in process memory.
It is validated again, never accepted on a public ``status=validated`` flag.
The original report, finite claims, source clocks and usage history are not
mutated. Serialized candidates require a future independent admission path.
"""
    current = semantic._instant(now or dt.datetime.now(dt.timezone.utc))
    if not current:
        _fail('INVALID_CURRENT_TIME')
    pool, proof, identity = _validate_source(retained_report, packet, source_identity, code, current)
    if not _hash(expected_baseline_digest) or pool['digest'] != expected_baseline_digest:
        _fail('BASELINE_POOL_CHANGED')
    old_map = _existing_map(pool, packet)
    usage = _validate_history(usage_history, expected_usage_history_digest, model_observation, packet, current)
    response_digest = canonical_digest(resolved_response)
    if model_observation['boundResponseDigest'] != response_digest:
        _fail('BOUND_RESPONSE_DIGEST_MISMATCH')
    validated = semantic.validate_semantic_response(packet, resolved_response)
    if validated['status'] != 'structure-and-evidence-validated':
        _fail('STRICT_SEMANTIC_RESPONSE_REJECTED')
    new_pool, quotes = copy.deepcopy(pool), copy.deepcopy(pool['quotes'])
    by_id = {row['id']: row for row in quotes}
    sentences = {row['sentence_id']: row for row in packet._sentences}
    public_evidence = {row['id']: row for row in validated['evidence']}
    mapping, published = {}, []
    omitted = {'ambiguousLiteralClaims': 0, 'quoteBudgetClaims': 0}
    for claim in validated['claims']:
        sentence = sentences[claim['sentence_id']]
        text, pages, key = sentence['text'], sentence['pages'], claim['evidenceId']
        expected_id = _semantic_id(packet['source'], sentence['start'], sentence['end'], text)
        if key != expected_id:
            _fail('SEMANTIC_EVIDENCE_BINDING_MISMATCH')
        found = _locations(packet._text, text, packet._positions, pages)
        if found != [sentence['start']]:
            omitted['ambiguousLiteralClaims'] += 1
            continue
        quote_id = pool_api._quote_id(pool['source']['sourceTextHash'], semantic._hash(text), pages)
        quote = {'id': quote_id, 'text': text, 'textHash': semantic._hash(text), 'page': pages[0],
                 'pages': pages, 'section': packet['source']['section']}
        if key in old_map:
            if old_map[key] != quote_id or by_id.get(quote_id) != quote:
                _fail('REUSED_EVIDENCE_BINDING_MISMATCH')
        else:
            evidence = public_evidence.get(key)
            if (not evidence or any(evidence.get(field) != value for field, value in {
                    'text': text, 'textHash': quote['textHash'], 'sentence_id': sentence['sentence_id'],
                    'page': pages[0], 'pages': pages, 'sourceUrl': packet['source']['sourceUrl'],
                    'periodStart': packet['source']['periodStart'], 'periodEnd': packet['source']['periodEnd']}.items())):
                _fail('SEMANTIC_EVIDENCE_BINDING_MISMATCH')
        if quote_id not in by_id:
            if (len(quotes) >= pool_api.LIMITS['maxQuotes'] or len(text) > pool_api.LIMITS['maxQuoteChars']
                    or sum(len(row['text']) for row in quotes) + len(text) > pool_api.LIMITS['maxTotalQuoteChars']):
                omitted['quoteBudgetClaims'] += 1
                continue
            quotes.append(copy.deepcopy(quote)); by_id[quote_id] = quote
        mapping[key] = quote_id
        mapped = _span_refs(claim)
        mapped.pop('evidenceId')
        mapped['evidencePoolId'] = quote_id
        mapped['sentenceRange'] = {field: sentence[field] for field in ('start', 'end', 'pages', 'textHash')}
        published.append(mapped)
    new_pool.update(quotes=quotes, digest=pool_api._digest(pool['source'], quotes), usedQuotes=len(quotes),
                    usedQuoteChars=sum(len(row['text']) for row in quotes))
    if not pool_api.normalize_strategy_evidence_pool(new_pool, proof, now=current):
        _fail('RESULT_POOL_INVALID')
    projections = copy.deepcopy(retained_report); projections['strategyEvidencePool'] = new_pool
    if pool_api.strategy_evidence_budget_for(projections, current)['status'] != 'verified':
        _fail('LEGACY_PROJECTION_CHANGED')
    coverage = {**validated['coverage'], 'mappedClaimCount': len(published),
                'mapperOmittedClaims': omitted, 'newQuoteCount': len(quotes) - len(pool['quotes'])}
    if (len(published) + sum(omitted.values()) != coverage['publishedClaimCount']
            or coverage['publishedClaimCount'] + sum(coverage[key] for key in (
                'longSentenceClaims', 'quoteBudgetClaims', 'claimLimitClaims')) != coverage['validatedClaimCount']):
        _fail('CLAIM_ACCOUNTING_MISMATCH')
    payload = {'schemaVersion': 1, 'kind': 'strategy-semantic-pool-candidate', 'method': METHOD,
        'status': 'candidate-not-integrated', 'code': code, 'sourceSectionComplete': True, 'inputCoverageComplete': True,
        'offsetUnit': 'unicode-code-point', 'spanOrigin': 'sentence-relative', 'sentenceRangeOrigin': 'section-relative',
        'selectedEvidenceOnly': True, 'mappingCompleteForSelectedClaims': not any(omitted.values()),
        'semanticCorrectnessVerified': False, 'semanticAnalysisComplete': False, 'productionReady': False,
        'productionAdmission': 'not-connected', 'managerAttribution': validated['managerAttribution'],
        'explainsIndividualStockTrades': False, 'inputDigest': packet['inputDigest'],
        'baselinePoolDigest': pool['digest'], 'resultPoolDigest': new_pool['digest'],
        'baselineSnapshotDigest': canonical_digest(pool), 'resultSnapshotDigest': canonical_digest(new_pool),
        'pool': new_pool, 'strategySectionCoverage': copy.deepcopy(proof),
        'evidenceIdMap': [{'semanticEvidenceId': key, 'evidencePoolId': value} for key, value in mapping.items()],
        'claims': published, 'sentenceStatuses': copy.deepcopy(validated['sentenceStatuses']), 'coverage': coverage,
        'sidecar': {'derivedFromObservedAt': pool['source']['retrievedAt'],
            'retainedStrategyObservedAt': retained_report.get('strategyEvidenceRetrievedAt'),
            'source': copy.deepcopy(packet['source']), 'sourceIdentity': identity,
            'modelObservation': copy.deepcopy(model_observation)},
        'usageHistory': copy.deepcopy(usage_history), 'usageAccounting': usage}
    payload['candidateDigest'] = canonical_digest(payload)
    return SemanticPoolCandidate(payload)


def apply_candidate_pool(current_pool, candidate):
    """In-memory compare-and-swap only; no report, admission or ledger writes."""
    if not isinstance(candidate, SemanticPoolCandidate) or dict(candidate) != candidate._manifest:
        _fail('UNVERIFIED_POOL_CANDIDATE')
    snapshot = canonical_digest(current_pool)
    if snapshot == candidate['resultSnapshotDigest']:
        return {'status': 'already-applied', 'scope': 'quote-pool-only', 'semanticCandidatePersisted': False,
                'pool': copy.deepcopy(current_pool)}
    if snapshot != candidate['baselineSnapshotDigest'] or current_pool.get('digest') != candidate['baselinePoolDigest']:
        _fail('BASELINE_POOL_CHANGED')
    return {'status': 'candidate-applied-in-memory', 'scope': 'quote-pool-only', 'semanticCandidatePersisted': False,
            'pool': copy.deepcopy(candidate['pool'])}


__all__ = ['map_strategy_semantic_pool', 'apply_candidate_pool', 'canonical_digest', 'MappingError', 'SemanticPoolCandidate']
