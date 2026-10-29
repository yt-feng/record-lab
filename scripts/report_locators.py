"""Public report-location candidates; only a separately verified PDF proves identity."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

from cninfo_discovery import (ENDPOINT, ENTRY, ActionsTransport, DiscoveryError, discover_period,
                              pagination_probe, report_title)
from publication_guard import inspect_content
from source_dates import china_date

PERIODS = {'2026Q2': '2026-06-30', '2026Q3': '2026-09-30'}
MAX_BYTES = 16_000_000
MAX_RECORDS = 20000
STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def instant(value):
    try:
        if isinstance(value, dt.datetime): return value if value.tzinfo is not None else None
        if not isinstance(value, str) or not STAMP.fullmatch(value): return None
        if not value.endswith('Z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59): return None
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: return None


def current(now=None):
    value = instant(now if now is not None else dt.datetime.now(dt.timezone.utc))
    if value is None: raise ValueError('INVALID_LOCATOR_TIME')
    return value


def date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) and re.fullmatch(r'2026-\d{2}-\d{2}', value) else None
    except ValueError: return None


def code_value(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{6}', value) is not None and value != '000000'


def candidate_id(value):
    stable = {key: value[key] for key in ('announcementId', 'title', 'publishedAt', 'sourceUrl', 'targetPeriod', 'correctionLabel')}
    stable['codes'] = sorted(value['codes'])
    return hashlib.sha256(canonical(stable)).hexdigest()


def normalize_candidate(value, target_period_end, *, now=None, code=None):
    if not isinstance(value, dict): return None
    quarter = value.get('targetPeriod')
    if PERIODS.get(quarter) != target_period_end: return None
    aid, title, codes = value.get('announcementId'), value.get('title'), value.get('codes')
    if (not isinstance(aid, str) or not re.fullmatch(r'[0-9]{1,20}', aid)
            or not isinstance(title, str) or not 0 < len(title) <= 300 or not report_title(title, int(quarter[-1]))
            or re.search(r'摘要|[<>\x00-\x1f\x7f\ud800-\udfff]', title)
            or not isinstance(codes, list) or not 1 <= len(codes) <= 100 or not all(code_value(c) for c in codes)
            or len(codes) != len(set(codes)) or code is not None and code not in codes): return None
    label = value.get('label')
    if label is not None and (not isinstance(label, str) or not 0 < len(label) <= 200 or re.search(r'[<>\x00-\x1f\x7f\ud800-\udfff]', label)): return None
    correction = value.get('correctionLabel')
    if type(correction) is not bool or correction != bool(re.search(r'更正|修订', title)) or value.get('pdfVerified') is not False: return None
    published, observed = date(value.get('publishedAt')), instant(value.get('retrievedAt'))
    if (not published or published < date(target_period_end) or not observed or observed > current(now) + dt.timedelta(minutes=5)
            or published > china_date(observed)): return None
    match = re.fullmatch(r'https://static\.cninfo\.com\.cn/finalpage/(2026-\d{2}-\d{2})/([0-9]{1,20})\.(?:PDF|pdf)', str(value.get('sourceUrl', '')))
    if not match or match[1] != value['publishedAt'] or match[2] != aid or value.get('discoverySourceUrl') != ENDPOINT: return None
    page = value.get('sourcePage')
    if not isinstance(page, dict): return None
    first, last = date(page.get('start')), date(page.get('end'))
    if (not first or not last or not first <= published <= last <= china_date(observed)
            or type(page.get('page')) is not int or not 1 <= page['page'] <= 32
            or not isinstance(page.get('contentHash'), str) or not HASH.fullmatch(page['contentHash'])
            or type(page.get('splitProbe')) is not bool): return None
    clean = {'announcementId': aid, 'codes': sorted(codes), 'label': label, 'title': title,
             'publishedAt': value['publishedAt'], 'sourceUrl': f'https://static.cninfo.com.cn/finalpage/{match[1]}/{aid}.PDF',
             'targetPeriod': quarter, 'correctionLabel': correction, 'pdfVerified': False,
             'retrievedAt': value['retrievedAt'], 'discoverySourceUrl': ENDPOINT,
             'sourcePage': {key: page[key] for key in ('start', 'end', 'page', 'contentHash', 'splitProbe')}}
    clean['candidateId'] = candidate_id(clean)
    return clean if value.get('candidateId') == clean['candidateId'] else None


def locator_for(code, target_period_end, candidates):
    chosen = sorted(candidates, key=lambda c: (c['publishedAt'], c['correctionLabel'], c['announcementId'], c['sourceUrl']), reverse=True)[:2]
    stable = {'code': code, 'targetPeriodEnd': target_period_end, 'candidateIds': sorted(c['candidateId'] for c in chosen)}
    return {'schemaVersion': 1, 'code': code, 'targetPeriodEnd': target_period_end,
            'digest': hashlib.sha256(canonical(stable)).hexdigest(), 'candidates': chosen}


def normalize_locator(value, code, target_period_end, now=None):
    if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int or value.get('schemaVersion') != 1 or value.get('code') != code or not code_value(code)
            or value.get('targetPeriodEnd') != target_period_end or target_period_end not in PERIODS.values()
            or not isinstance(value.get('candidates'), list) or not 1 <= len(value['candidates']) <= 2): return None
    candidates = [normalize_candidate(v, target_period_end, now=now, code=code) for v in value['candidates']]
    if not all(candidates) or len({v['candidateId'] for v in candidates}) != len(candidates): return None
    clean = locator_for(code, target_period_end, candidates)
    return clean if value.get('digest') == clean['digest'] else None


def normalize_receipt(value, locator, now=None):
    if not isinstance(value, dict) or not isinstance(locator, dict): return None
    locator = normalize_locator(locator, locator.get('code'), locator.get('targetPeriodEnd'), now)
    if not locator or type(value.get('schemaVersion')) is not int or value.get('schemaVersion') != 1 or any(value.get(k) != locator[k] for k in ('code', 'targetPeriodEnd', 'digest')): return None
    attempted = instant(value.get('attemptedAt')); ids = value.get('attemptedCandidateIds')
    if (not attempted or attempted > current(now) + dt.timedelta(minutes=5) or not isinstance(ids, list) or not 1 <= len(ids) <= 2
            or not all(isinstance(v, str) for v in ids) or len(set(ids)) != len(ids)
            or not set(ids) <= {v['candidateId'] for v in locator['candidates']}): return None
    if any(instant(v['retrievedAt']) > attempted for v in locator['candidates'] if v['candidateId'] in ids): return None
    return {**{key: value[key] for key in ('schemaVersion', 'code', 'targetPeriodEnd', 'digest', 'attemptedAt')}, 'attemptedCandidateIds': sorted(ids)}


def empty_registry(quarter):
    return {'schemaVersion': 1, 'kind': 'report-locator-registry', 'targetPeriod': quarter, 'targetPeriodEnd': PERIODS[quarter],
            'sourceUrl': ENDPOINT, 'historicalUniverseVerified': False, 'allQuarterReportsVerified': False,
            'records': [], 'lastAttemptAt': None, 'asOf': None, 'refreshStatus': 'unavailable', 'errorCode': None,
            'windowEnumerationComplete': False}


def normalize_registry(value, quarter, *, now=None):
    if (quarter not in PERIODS or not isinstance(value, dict) or value.get('schemaVersion') != 1
            or value.get('kind') != 'report-locator-registry' or value.get('targetPeriod') != quarter
            or value.get('targetPeriodEnd') != PERIODS[quarter] or value.get('sourceUrl') != ENDPOINT
            or value.get('historicalUniverseVerified') is not False or value.get('allQuarterReportsVerified') is not False
            or not isinstance(value.get('records'), list) or len(value['records']) > MAX_RECORDS): raise ValueError('INVALID_LOCATOR_REGISTRY')
    records = [normalize_candidate(v, PERIODS[quarter], now=now) for v in value['records']]
    if not all(records) or len({v['candidateId'] for v in records}) != len(records): raise ValueError('INVALID_LOCATOR_REGISTRY')
    attempt = value.get('lastAttemptAt')
    if attempt is not None and (not instant(attempt) or instant(attempt) > current(now) + dt.timedelta(minutes=5)): raise ValueError('INVALID_LOCATOR_REGISTRY')
    as_of = value.get('asOf')
    if as_of is not None and (not date(as_of) or date(as_of) > china_date(current(now))): raise ValueError('INVALID_LOCATOR_REGISTRY')
    if value.get('refreshStatus') not in {'unavailable', 'current', 'partial', 'cached'} or type(value.get('windowEnumerationComplete')) is not bool: raise ValueError('INVALID_LOCATOR_REGISTRY')
    error = value.get('errorCode')
    if error is not None and (not isinstance(error, str) or not re.fullmatch(r'[A-Z_]{1,80}', error)): raise ValueError('INVALID_LOCATOR_REGISTRY')
    return {**empty_registry(quarter), 'records': records, 'lastAttemptAt': attempt, 'asOf': as_of,
            'refreshStatus': value['refreshStatus'], 'errorCode': error, 'windowEnumerationComplete': value['windowEnumerationComplete']}


def read_registry(file, quarter, *, now=None):
    file = Path(file)
    if not file.exists(): return empty_registry(quarter)
    if file.is_symlink() or file.parent.is_symlink() or not file.is_file() or file.stat().st_size > MAX_BYTES: raise ValueError('INVALID_LOCATOR_REGISTRY')
    raw = file.read_bytes()
    if inspect_content(raw, MAX_BYTES): raise ValueError('INVALID_LOCATOR_REGISTRY')
    return normalize_registry(json.loads(raw), quarter, now=now)


def candidates_from_result(result, *, now=None):
    quarter = result.get('targetPeriod') if isinstance(result, dict) else None
    proof = result.get('paginationVerification', {}) if isinstance(result, dict) else {}
    if (quarter not in PERIODS or result.get('sourceUrl') != ENDPOINT
            or result.get('query') != {'column': 'fund', 'tabName': 'fulltext', 'category': 'category_jdbg_jjgg', 'searchkey': '', 'pageSize': 30}
            or proof.get('status') != 'verified-pagination' or proof.get('pageSize') != 30
            or proof.get('requiredProfiles') != ['q2-empty-30', 'q3-empty-30']
            or not instant(proof.get('observedAt')) or instant(proof['observedAt']) > current(now) + dt.timedelta(minutes=5)
            or not isinstance(result.get('announcements'), list) or len(result['announcements']) > MAX_RECORDS): raise ValueError('INVALID_LOCATOR_DISCOVERY')
    pages = {(p.get('start'), p.get('end'), p.get('page'), p.get('contentHash'), p.get('retrievedAt'))
             for p in result.get('pages', []) if isinstance(p, dict)}
    records = []
    for row in result['announcements']:
        value = {**row, 'discoverySourceUrl': ENDPOINT}
        if isinstance(value.get('sourceUrl'), str) and value['sourceUrl'].endswith('.pdf'):
            value['sourceUrl'] = value['sourceUrl'][:-4] + '.PDF'
        try: value['candidateId'] = candidate_id(value)
        except (KeyError, TypeError, ValueError): raise ValueError('INVALID_LOCATOR_DISCOVERY') from None
        candidate = normalize_candidate(value, PERIODS[quarter], now=now)
        page = (candidate or {}).get('sourcePage', {})
        if not candidate or (page.get('start'), page.get('end'), page.get('page'), page.get('contentHash'), candidate['retrievedAt']) not in pages:
            raise ValueError('INVALID_LOCATOR_DISCOVERY')
        records.append(candidate)
    return records


def fetch_results(quarters, as_of, *, now=None, max_seconds=600):
    # One shared deadline/request budget covers pagination proof and all quarters.
    transport = ActionsTransport(max_seconds=max_seconds)
    verification = pagination_probe(fetch_page=transport, observed_at=current(now), as_of=as_of, controls=False)
    if not verification['mayEnumerate']: raise DiscoveryError('PAGINATION_NOT_VERIFIED')
    results = {}
    for quarter in quarters:
        result = discover_period(quarter, as_of=as_of, fetch_page=transport, observed_at=dt.datetime.now(dt.timezone.utc), include_q2_late=True)
        result['paginationVerification'] = {'status': verification['status'], 'pageSize': verification['verifiedPageSize'],
            'observedAt': verification['observedAt'], 'requiredProfiles': ['q2-empty-30', 'q3-empty-30']}
        results[quarter] = result
    return results


def refresh_registries(root, quarters, as_of, *, now=None, fetcher=None, allow_fetch=True):
    at = current(now); result = {}; pending = []
    if not date(as_of) or date(as_of) > china_date(at): raise ValueError('INVALID_LOCATOR_TIME')
    for quarter in quarters:
        try: registry = read_registry(Path(root) / 'periods' / quarter / 'report-locators.json', quarter, now=at)
        except (ValueError, OSError): registry = empty_registry(quarter)
        result[quarter] = registry
        if registry['asOf'] != as_of or china_date(registry['lastAttemptAt']) != china_date(at): pending.append(quarter)
    if not pending or not allow_fetch or fetcher is None and os.environ.get('GITHUB_ACTIONS') != 'true': return result
    try:
        fetched = (fetcher or fetch_results)(pending, as_of, now=at)
        for quarter in pending:
            old = result[quarter]; fresh = fetched.get(quarter)
            try:
                records = candidates_from_result(fresh, now=current(now) if now is not None else current())
                combined = {v['candidateId']: v for v in old['records']}
                # An unchanged locator keeps its first source observation. Clock
                # refreshes cannot re-arm a failed PDF candidate's retry budget.
                for value in records: combined.setdefault(value['candidateId'], value)
                if len(combined) > MAX_RECORDS: raise ValueError('LOCATOR_RECORD_LIMIT')
                updated = {**old, 'records': sorted(combined.values(), key=lambda v: v['candidateId']),
                    'lastAttemptAt': at.isoformat(), 'asOf': as_of, 'refreshStatus': 'current' if fresh.get('windowEnumerationComplete') is True else 'partial',
                    'windowEnumerationComplete': fresh.get('windowEnumerationComplete') is True, 'errorCode': None}
                if len(canonical(updated)) > MAX_BYTES: raise ValueError('LOCATOR_RECORD_LIMIT')
                result[quarter] = updated
            except (ValueError, TypeError, AttributeError):
                result[quarter] = {**old, 'lastAttemptAt': at.isoformat(), 'asOf': as_of,
                    'refreshStatus': 'cached' if old['records'] else 'unavailable', 'errorCode': 'LOCATOR_REFRESH_FAILED', 'windowEnumerationComplete': False}
    except Exception as error:
        error_code = error.code if isinstance(error, DiscoveryError) else 'LOCATOR_REFRESH_FAILED'
        for quarter in pending:
            old = result[quarter]
            result[quarter] = {**old, 'lastAttemptAt': at.isoformat(), 'asOf': as_of,
                'refreshStatus': 'cached' if old['records'] else 'unavailable', 'errorCode': error_code, 'windowEnumerationComplete': False}
    return result


def registry_locators(registry, codes, *, now=None, as_of=None):
    grouped = {}; allowed = set(codes)
    cutoff = as_of or registry.get('asOf') or china_date(current(now)).isoformat()
    for candidate in registry['records']:
        if candidate['publishedAt'] > cutoff: continue
        for code in candidate['codes']:
            if code in allowed: grouped.setdefault(code, []).append(candidate)
    return {code: locator_for(code, registry['targetPeriodEnd'], candidates) for code, candidates in grouped.items()}


def freeze_registry(registry, quarter, base='.cache/campaign', *, now=None):
    clean = normalize_registry(registry, quarter, now=now); raw = canonical(clean) + b'\n'
    if len(raw) > MAX_BYTES or inspect_content(raw, MAX_BYTES): raise ValueError('LOCATOR_PUBLICATION_REJECTED')
    file = Path(base) / 'locators' / (quarter + '.json'); file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(raw)
    return {'schemaVersion': 1, 'path': 'locators/' + quarter + '.json', 'sha256': hashlib.sha256(raw).hexdigest(), 'byteCount': len(raw)}


def validate_registry_lock(binding, quarter, base='.cache/campaign', *, now=None):
    if (quarter not in PERIODS or not isinstance(binding, dict) or binding.get('schemaVersion') != 1 or binding.get('path') != 'locators/' + quarter + '.json'
            or type(binding.get('byteCount')) is not int or not 0 < binding['byteCount'] <= MAX_BYTES): raise ValueError('INVALID_LOCATOR_LOCK')
    file = Path(base) / binding['path']; clean = read_registry(file, quarter, now=now)
    if not file.is_file(): raise ValueError('INVALID_LOCATOR_LOCK')
    raw = file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding.get('sha256') or len(raw) != binding['byteCount']: raise ValueError('INVALID_LOCATOR_LOCK')
    return file, clean


def expand_config(config, base='.cache/campaign', *, now=None):
    """Load one locked registry in the planner/merger, never copy it into batches."""
    if not isinstance(config, dict): return config
    if not {'reportLocatorUniverseCodes', 'reportLocatorRegistry'}.intersection(config): return config
    if not {'reportLocatorUniverseCodes', 'reportLocatorRegistry'} <= config.keys(): raise ValueError('INVALID_LOCATOR_LOCK')
    codes = config['reportLocatorUniverseCodes']; quarter = next((q for q,p in PERIODS.items() if p == config.get('targetPeriodEnd')), None)
    if (not quarter or not isinstance(codes, list) or len(codes) > 150000 or not all(code_value(c) for c in codes)
            or len(set(codes)) != len(codes) or 'reportLocatorCandidates' in config): raise ValueError('INVALID_LOCATOR_LOCK')
    _, registry = validate_registry_lock(config.get('reportLocatorRegistry'), quarter, base, now=now)
    candidates = registry_locators(registry, codes, now=now, as_of=config.get('asOf')) if config.get('reportLocatorEnabled') is True else {}
    return {**config, 'reportLocatorCandidates': candidates}
