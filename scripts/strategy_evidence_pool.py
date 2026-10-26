"""One source-bound quote pool for all public strategy projections.

Complete sections stay private. Repeated text projections refer to the same
pool entry; the budget counts unique source quotations, not serialized bytes.
Legacy reports are diagnosed, never silently rewritten by the validator.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re

from source_dates import china_date
from strategy_claims import _instant, _mapped_sentences
from strategy_sections import normalize_strategy_section_coverage

METHOD = 'shared-strategy-evidence-pool-v1'
ACCOUNTING = 'unique-source-quote-pool-not-serialized-bytes'
SOURCE_FIELDS = ('sourceUrl', 'publishedAt', 'periodStart', 'periodEnd', 'kind', 'sourceTextHash', 'retrievedAt')
LIMITS = {'maxQuotes': 8, 'maxQuoteChars': 160, 'maxTotalQuoteChars': 1280}
POOL_FIELDS = {'schemaVersion', 'method', 'status', 'source', 'quotes', 'digest', 'usedQuotes', 'usedQuoteChars',
               'accounting', 'semanticAnalysisComplete', *LIMITS}
QUOTE_FIELDS = {'id', 'text', 'textHash', 'page', 'pages', 'section'}


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _quote_id(source_hash, text_hash, pages):
    return 'q' + _hash(source_hash + '|' + text_hash + '|' + ','.join(map(str, pages)))


def _digest(source, quotes):
    return _hash(_canonical({'schemaVersion': 1, 'source': {key: value for key, value in source.items() if key != 'retrievedAt'}, 'quotes': quotes}))


def _current(now):
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    value = _instant(now) if isinstance(now, str) else now
    return value if isinstance(value, dt.datetime) and value.tzinfo is not None else None


def normalize_strategy_evidence_pool(raw, coverage, expected_source=None, now=None):
    """Validate public structure/source binding; no claim of semantic truth."""
    proof = normalize_strategy_section_coverage(coverage)
    if (not proof or not proof['complete'] or proof['kind'] != 'quarterly'
            or not isinstance(raw, dict) or set(raw) != POOL_FIELDS
            or type(raw.get('schemaVersion')) is not int or raw['schemaVersion'] != 1
            or raw.get('method') != METHOD or raw.get('status') != 'verified'
            or raw.get('accounting') != ACCOUNTING or raw.get('semanticAnalysisComplete') is not False
            or any(type(raw.get(key)) is not int or raw[key] != value for key, value in LIMITS.items())):
        return None
    source = raw.get('source')
    if (not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS)
            or any(source.get(key) != proof.get(key) for key in SOURCE_FIELDS if key != 'retrievedAt')
            or any(source.get(key) != value for key, value in (expected_source or {}).items() if key in SOURCE_FIELDS)):
        return None
    observed, current = _instant(source.get('retrievedAt')), _current(now)
    if (not observed or not current or observed > current + dt.timedelta(minutes=5)
            or source['publishedAt'] > china_date(observed).isoformat()):
        return None
    values = raw.get('quotes')
    if not isinstance(values, list) or len(values) > LIMITS['maxQuotes']:
        return None
    quotes, seen = [], set()
    section = proof['sections'][0]
    for item in values:
        if not isinstance(item, dict) or set(item) != QUOTE_FIELDS:
            return None
        text, pages = item.get('text'), item.get('pages')
        if (not isinstance(text, str) or not text.strip() or len(text) > LIMITS['maxQuoteChars']
                or re.search(r'[\ud800-\udfff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', text)
                or not isinstance(pages, list) or not pages or len(pages) > 16
                or any(type(page) is not int or page not in section['pages'] for page in pages)
                or pages != list(range(pages[0], pages[0] + len(pages)))
                or type(item.get('page')) is not int or item['page'] != pages[0]
                or item.get('section') != section['section'] or item.get('textHash') != _hash(text)
                or item.get('id') != _quote_id(source['sourceTextHash'], _hash(text), pages)
                or item['id'] in seen):
            return None
        seen.add(item['id']); quotes.append(copy.deepcopy(item))
    chars = sum(len(item['text']) for item in quotes)
    if (type(raw.get('usedQuotes')) is not int or raw['usedQuotes'] != len(quotes)
            or type(raw.get('usedQuoteChars')) is not int or raw['usedQuoteChars'] != chars
            or chars > LIMITS['maxTotalQuoteChars'] or raw.get('digest') != _digest(source, quotes)):
        return None
    return copy.deepcopy(raw)


def _budget(status, reasons, pool=None, projection=False):
    return {'schemaVersion': 1, 'status': status, 'sourceBound': bool(pool), 'poolVerified': bool(pool),
            'projectionVerified': projection, 'usedQuotes': pool['usedQuotes'] if pool else None,
            'usedQuoteChars': pool['usedQuoteChars'] if pool else None, **LIMITS, 'accounting': ACCOUNTING,
            'semanticAnalysisComplete': False, 'reasonCodes': reasons}


def strategy_evidence_budget_for(report, now=None):
    """Check every public field against the pool, preserving legacy content."""
    prior_partial = isinstance(report, dict) and isinstance(report.get('strategyEvidenceBudget'), dict) and report['strategyEvidenceBudget'].get('status') == 'partial'
    if not isinstance(report, dict) or report.get('strategyEvidencePool') is None:
        if prior_partial:
            return _budget('partial', ['PRIOR_QUOTE_BUDGET_REJECTED'])
        return _budget('legacy-unbudgeted', ['EVIDENCE_POOL_MIGRATION_REQUIRED'])
    original = report.get('strategyEvidenceRetrievedAt')
    derived = report.get('strategyClaimsDerivedFromObservedAt')
    observed = (report['strategyEvidencePool'].get('source') or {}).get('retrievedAt') if isinstance(report['strategyEvidencePool'], dict) else None
    if 'strategyClaimsDerivedFromObservedAt' in report:
        old_time, new_time = _instant(original), _instant(observed)
        if derived != original or not old_time or not new_time or old_time > new_time:
            return _budget('partial', ['STRATEGY_SOURCE_OBSERVATION_MISMATCH'])
    else:
        observed = original
    pool = normalize_strategy_evidence_pool(report['strategyEvidencePool'], report.get('strategySectionCoverage'),
        {'retrievedAt': observed}, now)
    if not pool:
        return _budget('partial', ['INVALID_EVIDENCE_POOL'])
    if prior_partial:
        return _budget('partial', ['PRIOR_QUOTE_BUDGET_REJECTED'], pool)
    by_id, source = {item['id']: item for item in pool['quotes']}, pool['source']
    values = []
    narrative = report.get('narrative')
    if narrative is not None:
        if not isinstance(narrative, dict):
            return _budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool)
        if narrative.get('text') is not None:
            values.append(narrative)
    for container, key in ((report, 'strategyExcerpts'), (report.get('strategyAnalysis'), 'evidence'),
                           (report.get('strategyClaims'), 'evidence')):
        if container is None:
            continue
        if not isinstance(container, dict) or key in container and not isinstance(container[key], list):
            return _budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool)
        rows = container.get(key, [])
        if len(rows) > 8:
            return _budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool)
        values.extend(rows)
    claims = report.get('strategyClaims')
    if claims and (not isinstance(claims.get('source'), dict)
                   or any(claims['source'].get(key) != value for key, value in source.items())):
        return _budget('partial', ['STRATEGY_CLAIMS_SOURCE_MISMATCH'], pool)
    for item in values:
        quote = by_id.get(item.get('evidencePoolId')) if isinstance(item, dict) else None
        if (not quote or item.get('text') != quote['text'] or item.get('sourceUrl') != source['sourceUrl']
                or type(item.get('page')) is not int or item['page'] != quote['page']
                or any(item.get(key) != source[key] for key in ('periodStart', 'periodEnd'))
                or 'pages' in item and item['pages'] != quote['pages']
                or 'textHash' in item and item['textHash'] != quote['textHash']):
            return _budget('partial', ['UNPOOLED_STRATEGY_PROJECTION'], pool)
    return _budget('verified', [], pool, True)


def apply_strategy_evidence_budget(details, pages, report, *, sections_result, page_numbers=None, now=None):
    """Return a fresh extraction patch; never migrate retained legacy reports."""
    proof = normalize_strategy_section_coverage(sections_result.get('strategySectionCoverage'))
    numbers = list(page_numbers) if page_numbers is not None else list(range(1, len(pages) + 1))
    source = {key: proof.get(key) for key in SOURCE_FIELDS if key != 'retrievedAt'} if proof else {}
    source['retrievedAt'] = report.get('strategyEvidenceRetrievedAt') or report.get('retrievedAt')
    if (not proof or not proof['complete'] or proof['kind'] != 'quarterly'
            or any(proof.get(key) != report.get(key) for key in ('sourceUrl', 'publishedAt', 'periodEnd', 'kind'))
            or proof['sourceTextHash'] != _hash('\n\f\n'.join(pages))):
        return {'strategyEvidenceBudget': _budget('partial', ['UNVERIFIED_QUARTER_STRATEGY_SOURCE'])}
    sentences = _mapped_sentences(pages, numbers, sections_result, proof)
    if sentences is None:
        return {'strategyEvidenceBudget': _budget('partial', ['UNVERIFIED_QUARTER_STRATEGY_SOURCE'])}
    quotes, by_id = [], {}

    def add(item):
        if not isinstance(item, dict):
            return None
        text, index = item.get('text'), item.get('sentenceIndex')
        if (not isinstance(text, str) or not text or len(text) > 160 or type(index) is not int
                or not 0 <= index < len(sentences)
                or any(item.get(key) != source[key] for key in ('sourceUrl', 'periodStart', 'periodEnd'))):
            return None
        sentence = sentences[index]
        starts = [match.start() for match in re.finditer(re.escape(text), sentence['text'])]
        if not any(list(dict.fromkeys(sentence['positions'][start:start + len(text)])) == item.get('pages') for start in starts):
            return None
        pages_used = item['pages']
        value = {'id': _quote_id(source['sourceTextHash'], _hash(text), pages_used), 'text': text, 'textHash': _hash(text),
                 'page': pages_used[0], 'pages': list(pages_used), 'section': proof['sections'][0]['section']}
        if value['id'] not in by_id:
            if len(quotes) >= 8 or sum(len(row['text']) for row in quotes) + len(text) > 1280:
                return None
            by_id[value['id']] = value; quotes.append(value)
        return {**copy.deepcopy(item), 'evidencePoolId': value['id']}

    patch = {}
    claims = details.get('strategyClaims')
    if isinstance(claims, dict) and claims.get('ruleScanComplete') is True:
        projected = [add(item) for item in claims.get('evidence', [])]
        if any(item is None for item in projected):
            return {'strategyEvidenceBudget': _budget('partial', ['UNVERIFIED_CLAIM_PROJECTION'])}
        patch['strategyClaims'] = {**copy.deepcopy(claims), 'evidence': projected}
    analysis = copy.deepcopy(details.get('strategyAnalysis') or {})
    projected = [value for item in analysis.get('evidence', []) if (value := add(item)) is not None]
    analysis.update({'evidence': projected, 'selectedSentenceCount': len(projected),
                     'omittedSentenceCount': analysis.get('sentenceCount', 0) - len(projected)})
    if not projected:
        analysis['status'] = 'missing'
    pool = {'schemaVersion': 1, 'method': METHOD, 'status': 'verified', 'source': source, 'quotes': quotes,
            'digest': _digest(source, quotes), 'usedQuotes': len(quotes), 'usedQuoteChars': sum(len(item['text']) for item in quotes),
            **LIMITS, 'accounting': ACCOUNTING, 'semanticAnalysisComplete': False}
    if not normalize_strategy_evidence_pool(pool, proof, now=now):
        return {'strategyEvidenceBudget': _budget('partial', ['UNVERIFIED_QUARTER_STRATEGY_SOURCE'])}
    excerpts = [{**{key: item[key] for key in ('text', 'page', 'pages', 'section')},
                 'sourceUrl': source['sourceUrl'], 'periodStart': source['periodStart'], 'periodEnd': source['periodEnd'],
                 'evidencePoolId': item['id']} for item in quotes]
    narrative = {**excerpts[0], 'attribution': 'report', 'isInference': False, 'truncated': True,
                 'excerptSelection': 'shared-pool-selected-quote', 'reportKind': 'quarterly'} if excerpts else None
    patch.update({'narrative': narrative, 'strategyExcerpts': excerpts, 'strategyAnalysis': analysis,
                  'strategyEvidencePool': pool, 'strategyEvidenceRetrievedAt': source['retrievedAt']})
    if 'strategyClaims' not in patch and 'strategyClaims' in details:
        patch['strategyClaims'] = copy.deepcopy(details['strategyClaims'])
    fresh = {**details, **patch}
    fresh.pop('strategyEvidenceBudget', None)
    patch['strategyEvidenceBudget'] = strategy_evidence_budget_for(fresh, now)
    return patch


__all__ = ['apply_strategy_evidence_budget', 'normalize_strategy_evidence_pool', 'strategy_evidence_budget_for']
