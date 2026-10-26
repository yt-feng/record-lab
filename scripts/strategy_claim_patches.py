"""Frozen, source-bound tasks for independent finite strategy backfill."""
from __future__ import annotations
import copy
import calendar
import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit, parse_qsl

from portfolio_identity import normalize_portfolio_identity
from price_patches import date
from source_dates import china_date
from strategy_claims import METHOD, _instant
from strategy_evidence_pool import strategy_evidence_budget_for
from strategy_sections import normalize_strategy_section_coverage

SOURCE_FIELDS = ('sourceUrl', 'publishedAt', 'kind', 'periodStart', 'periodEnd', 'sourceTextHash', 'section', 'sectionContentHash', 'pages')
TASK_FIELDS = {'schemaVersion', 'code', 'targetPeriodEnd', 'method', 'source', 'analysisDigest', 'taskDigest', 'retainedStrategyObservedAt'}
RECEIPT_FIELDS = {'schemaVersion', 'code', 'targetPeriodEnd', 'taskDigest', 'analysisDigest', 'sourceUrl', 'attemptedAt', 'outcome', 'errorCode'}
REPORT_FIELDS = ('fundCode', 'periodEnd', 'publishedAt', 'kind', 'sourceUrl', 'strategySectionCoverage', 'strategyClaims',
                 'strategyEvidencePool', 'strategyEvidenceBudget', 'strategyEvidenceRetrievedAt', 'strategyClaimsIdentityEvidence',
                 'strategyAnalysis', 'strategyExcerpts', 'strategyThemes', 'narrative')
ERRORS = {'STRATEGY_CLAIMS_BINDING_INVALID', 'STRATEGY_CLAIMS_BUDGET_REACHED', 'STRATEGY_CLAIMS_SOURCE_CHANGED',
          'STRATEGY_CLAIMS_IDENTITY_MISMATCH', 'STRATEGY_CLAIMS_INCOMPLETE', 'STRATEGY_CLAIMS_INTERRUPTED',
          'STRATEGY_CLAIMS_REQUEST_NOT_STARTED', 'STRATEGY_CLAIMS_FAILED', 'INVALID_URL', 'UNEXPECTED_REQUEST',
          'HTTP_REJECTED', 'HTTP_ERROR', 'TLS_FAILED', 'TIMEOUT', 'NETWORK_ERROR', 'EMPTY_RESPONSE', 'RESPONSE_TOO_LARGE',
          'INVALID_PDF', 'PDF_PARSE_FAILED', 'PDF_TEXT_EMPTY'}
NO_REQUEST_ERRORS = {'STRATEGY_CLAIMS_BINDING_INVALID', 'STRATEGY_CLAIMS_BUDGET_REACHED', 'STRATEGY_CLAIMS_REQUEST_NOT_STARTED'}


class StrategyClaimPatchError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'STRATEGY_CLAIMS_FAILED'
        super().__init__(self.code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def current(now=None):
    value = dt.datetime.now(dt.timezone.utc) if now is None else _instant(now) if isinstance(now, str) else now
    return value if isinstance(value, dt.datetime) and value.tzinfo is not None else None


def valid_code(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9]{6}', value)) and value != '000000'


def public_source(value):
    if (not isinstance(value, str) or len(value) > 2048 or re.search(r'[\x00-\x20\x7f\\#\ud800-\udfff]', value)
            or not re.match(r'^https://[A-Za-z0-9.-]+(?::443)?(?:/|$)', value)):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z][a-z0-9-]*', host))
                and not host.endswith(('.local', '.localhost', '.internal'))
                and not any(re.search(r'token|secret|password|signature|authorization|credential|key', key, re.I)
                            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)))
    except ValueError:
        return False


def normalize_task(value, code=None, period_end=None, now=None):
    if (not isinstance(value, dict) or set(value) != TASK_FIELDS or type(value.get('schemaVersion')) is not int
            or value['schemaVersion'] != 1 or not valid_code(value.get('code')) or value.get('method') != METHOD
            or code is not None and value['code'] != code or period_end is not None and value['targetPeriodEnd'] != period_end):
        return None
    source, end = value.get('source'), date(value.get('targetPeriodEnd'))
    if not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS) or not end or end.month not in (3, 6, 9, 12):
        return None
    start = dt.date(end.year, end.month - 2, 1)
    pages, observed, clock = source.get('pages'), _instant(value.get('retainedStrategyObservedAt')), current(now)
    observed_day = china_date(observed)
    if (end.day != calendar.monthrange(end.year, end.month)[1] or source.get('kind') != 'quarterly'
            or source.get('periodEnd') != end.isoformat() or source.get('periodStart') != start.isoformat()
            or not date(source.get('publishedAt')) or source['publishedAt'] < source['periodEnd']
            or not public_source(source.get('sourceUrl'))
            or source.get('section') not in ('4.4', '4.4.1')
            or any(not isinstance(source.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', source[key]) for key in ('sourceTextHash', 'sectionContentHash'))
            or not isinstance(pages, list) or not 1 <= len(pages) <= 16
            or any(type(page) is not int or not 1 <= page <= 10000 for page in pages)
            or pages != list(range(pages[0], pages[0] + len(pages)))
            or not observed or not observed_day or not clock or observed > clock + dt.timedelta(minutes=5)
            or source['publishedAt'] > observed_day.isoformat()):
        return None
    analysis_digest = digest({**source, 'method': METHOD})
    task_digest = digest({'code': value['code'], 'targetPeriodEnd': value['targetPeriodEnd'], 'analysisDigest': analysis_digest})
    if value.get('analysisDigest') != analysis_digest or value.get('taskDigest') != task_digest:
        return None
    return copy.deepcopy(value)


def binding_for(fund, phase, now=None, code=None):
    if not isinstance(fund, dict) or not isinstance(phase, dict):
        return None
    requested = code if code is not None else fund.get('code')
    report = fund.get('selectedReport')
    if not valid_code(requested) or not isinstance(report, dict) or report.get('periodEnd') != phase.get('targetPeriodEnd'):
        return None
    proof = normalize_strategy_section_coverage(report.get('strategySectionCoverage'), phase.get('targetPeriodEnd'))
    if not proof or not proof['complete'] or proof['kind'] != 'quarterly':
        return None
    section = proof['sections'][0]
    source = {key: proof.get(key) for key in SOURCE_FIELDS if key not in ('section', 'sectionContentHash', 'pages')}
    source.update(section=section['section'], sectionContentHash=section['contentHash'], pages=section['pages'])
    if requested != fund.get('code'):
        evidence = report.get('strategyClaimsIdentityEvidence') or report.get('portfolioIdentityEvidence')
        if not normalize_portfolio_identity(evidence, {**source, 'fundCode': requested}, now=now):
            return None
    analysis_digest = digest({**source, 'method': METHOD})
    value = {'schemaVersion': 1, 'code': requested, 'targetPeriodEnd': phase['targetPeriodEnd'], 'method': METHOD,
             'source': source, 'analysisDigest': analysis_digest,
             'taskDigest': digest({'code': requested, 'targetPeriodEnd': phase['targetPeriodEnd'], 'analysisDigest': analysis_digest}),
             'retainedStrategyObservedAt': report.get('strategyEvidenceRetrievedAt')}
    return normalize_task(value, requested, phase['targetPeriodEnd'], now)


def normalize_receipt(raw, task, planned_at, retrieved_at, now=None):
    task = normalize_task(task, now=now)
    planned, retrieved, clock = _instant(planned_at), _instant(retrieved_at), current(now)
    if not task or not isinstance(raw, dict) or set(raw) != RECEIPT_FIELDS or type(raw.get('schemaVersion')) is not int or raw['schemaVersion'] != 1:
        return None
    attempted = _instant(raw.get('attemptedAt'))
    if (any(raw.get(key) != task[key] for key in ('code', 'targetPeriodEnd', 'analysisDigest', 'taskDigest'))
            or raw.get('sourceUrl') != task['source']['sourceUrl'] or not planned or not attempted or not retrieved or not clock
            or not planned <= attempted <= retrieved <= clock + dt.timedelta(minutes=5)):
        return None
    outcome, error = raw.get('outcome'), raw.get('errorCode')
    if outcome in ('started', 'parsed'):
        if error is not None: return None
    elif outcome == 'unknown':
        if error != 'STRATEGY_CLAIMS_INTERRUPTED': return None
    elif outcome == 'failed':
        if error not in ERRORS - NO_REQUEST_ERRORS: return None
    else:
        return None
    return copy.deepcopy(raw)


def report_for(details, task, attempted_at, retrieved_at, now=None):
    """Validate source/budget/identity before emitting a bounded candidate.

    JS strategyClaimsFor remains the final finite-statement admission gate;
    Python never turns these source checks into full semantic completion.
    """
    task = normalize_task(task, now=now)
    if not task or not isinstance(details, dict): raise StrategyClaimPatchError('STRATEGY_CLAIMS_BINDING_INVALID')
    source = task['source']
    proof = normalize_strategy_section_coverage(details.get('strategySectionCoverage'), task['targetPeriodEnd'])
    if not proof or not proof['complete']: raise StrategyClaimPatchError('STRATEGY_CLAIMS_INCOMPLETE')
    section = proof['sections'][0]
    actual_source = {key: proof.get(key) for key in SOURCE_FIELDS if key not in ('section', 'sectionContentHash', 'pages')}
    actual_source.update(section=section['section'], sectionContentHash=section['contentHash'], pages=section['pages'])
    if actual_source != source: raise StrategyClaimPatchError('STRATEGY_CLAIMS_SOURCE_CHANGED')
    claims = details.get('strategyClaims')
    claim_source = claims.get('source') if isinstance(claims, dict) else None
    observed = _instant(details.get('strategyEvidenceRetrievedAt'))
    attempted, received, old = _instant(attempted_at), _instant(retrieved_at), _instant(task['retainedStrategyObservedAt'])
    if (not isinstance(claim_source, dict) or any(claim_source.get(key) != value for key, value in source.items())
            or claim_source.get('digest') != task['analysisDigest'] or claims.get('method') != METHOD
            or claims.get('sourceSectionComplete') is not True or claims.get('ruleScanComplete') is not True
            or claims.get('semanticAnalysisComplete') is not False or not observed or not attempted or not received
            or observed < attempted or observed < old or observed > received
            or claim_source.get('retrievedAt') != details.get('strategyEvidenceRetrievedAt')):
        raise StrategyClaimPatchError('STRATEGY_CLAIMS_INCOMPLETE')
    identity = normalize_portfolio_identity(details.get('strategyClaimsIdentityEvidence'), {**source, 'fundCode': task['code']}, now=now)
    if not identity or identity['retrievedAt'] != details['strategyEvidenceRetrievedAt']:
        raise StrategyClaimPatchError('STRATEGY_CLAIMS_IDENTITY_MISMATCH')
    report = {key: copy.deepcopy(details[key]) for key in REPORT_FIELDS if key in details}
    report.update(fundCode=task['code'], **{key: source[key] for key in ('periodEnd', 'publishedAt', 'kind', 'sourceUrl')})
    report['strategyClaimsIdentityEvidence'] = identity
    if strategy_evidence_budget_for(report, now)['status'] != 'verified':
        raise StrategyClaimPatchError('STRATEGY_CLAIMS_INCOMPLETE')
    if len(canonical(report).encode()) > 100_000:
        raise StrategyClaimPatchError('STRATEGY_CLAIMS_INCOMPLETE')
    return report


__all__ = ['METHOD', 'ERRORS', 'NO_REQUEST_ERRORS', 'REPORT_FIELDS', 'StrategyClaimPatchError', 'canonical', 'digest',
           'binding_for', 'normalize_task', 'normalize_receipt', 'report_for']
