"""Independent security-quarter conflict ledger; never rewrites a fund or quote.

Production scans and transports require Actions. Pure normalization/planning
functions accept small synthetic fixtures. Each digest has two recheck rounds,
each containing at most two original-source requests. A checkpoint is written
before transport invocation so an interrupted request is retained as unknown.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from price_coverage import CALENDAR, expected_sessions, date_set_hash, valid_complete_price
from price_patches import public_url, stock_key
from publication_guard import inspect_content
import verify_price_conflicts as diagnostic

PHASES = {'2026Q2': ('2026-04-01', '2026-06-30', '2026-03-31'),
          '2026Q3': ('2026-07-01', '2026-09-30', '2026-06-30')}
MAX_BYTES = 24_000_000
MAX_KEYS, MAX_FACTS, MAX_DIGESTS = 5000, 32, 16
MAX_PLAN_KEYS, MAX_REQUESTS, MAX_ATTEMPTS = 20, 40, 2
LEDGER_BOOTSTRAP = {'runId': 40379996941, 'runAttempt': 1, 'artifactId': 12678181967,
                   'sourceCommit': '43c8db4c4b9a80a0beb0b357fddd42d4536e72bf',
                   'dataCommit': '53a62f508fb6a5966a080591a73c6c8fed5f4d3e',
                   'archiveSha256': '1cb23aea236ca5077fb04a40e9b9f5bab645f24a099cd798e228c1421ac45475'}
HASH = re.compile(r'[0-9a-f]{64}')
PRICE_FIELDS = ('status', 'low', 'high', 'market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd',
                'coverageVersion', 'calendarId', 'sessionScope', 'completeness', 'observedCount', 'observedStart', 'observedEnd',
                'expectedSessionCount', 'expectedDateSetHash', 'observedDateSetHash', 'missingSessionCount', 'missingSessionDates',
                'unexpectedSessionCount', 'unexpectedSessionDates', 'rejectedRowCount', 'conflictingDateCount')


class ConflictError(Exception):
    pass


def fail(code):
    raise ConflictError(code)


def instant(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)', value): return None
    try: return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)
    except ValueError: return None


def clock(now=None):
    value = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(value, dt.datetime) or value.tzinfo is None: fail('CONFLICT_TIME_INVALID')
    return value.astimezone(dt.timezone.utc)


def encoded(value):
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError): fail('CONFLICT_INPUT_INVALID')


def digest(value):
    # Numeric facts are compared exactly. 1 and 1.0 are the same JSON number;
    # no decimal rounding or float32 coercion is applied to quote values.
    def canonical(item):
        if type(item) in (int, float): return {'number': float(item).hex()}
        if isinstance(item, list): return [canonical(row) for row in item]
        if isinstance(item, dict): return {key: canonical(row) for key, row in item.items()}
        return item
    return hashlib.sha256(encoded(canonical(value))).hexdigest()


def bounded_int(value, maximum, code='CONFLICT_INPUT_INVALID'):
    if type(value) is not int or not 0 <= value <= maximum: fail(code)
    return value


def source_identity(source):
    if not public_url(source) or len(source) > 2048: fail('CONFLICT_SOURCE_INVALID')
    url = urlparse(source)
    pairs = parse_qsl(url.query, keep_blank_values=True)
    # These parameters describe retrieval timing, not a new price fact. Both
    # Yahoo hosts serve the same source and cannot count as two independent ones.
    if url.hostname in {'query1.finance.yahoo.com', 'query2.finance.yahoo.com'}:
        url = url._replace(netloc='query1.finance.yahoo.com')
        pairs = [(key, value) for key, value in pairs if key != 'period2']
    return urlunparse(url._replace(query=urlencode(sorted(pairs))))


def identity(key, quarter):
    if quarter not in PHASES or not isinstance(key, str): fail('CONFLICT_IDENTITY_INVALID')
    pieces = key.split('|')
    if len(pieces) != 7: fail('CONFLICT_IDENTITY_INVALID')
    market, code, currency, basis, start, end, calendar = pieces
    if (not stock_key({'market': market, 'stockCode': code}) or currency != CALENDAR['markets'][market]['currency']
            or basis != 'unadjusted' or (start, end) != PHASES[quarter][:2] or calendar != CALENDAR['id']):
        fail('CONFLICT_IDENTITY_INVALID')
    return dict(zip(('market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd', 'calendarId'), pieces))


def normalize_group(group, row, now):
    if not isinstance(group, dict) or not isinstance(group.get('price'), dict): fail('CONFLICT_FACT_INVALID')
    source_price = group['price']
    price = {key: source_price[key] for key in PRICE_FIELDS if key in source_price}
    if (any(price.get(key) != row[key] for key in ('market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd'))
            or price.get('status') != 'observed' or any(type(price.get(key)) not in (int, float) or not math.isfinite(price[key]) for key in ('low', 'high'))
            or not 0 < price['low'] <= price['high']): fail('CONFLICT_FACT_INVALID')
    # Bound optional proof fields without upgrading malformed or old coverage.
    for key, value in price.items():
        if key in ('low', 'high'): continue
        if key.endswith('Count') or key == 'coverageVersion':
            if value is not None: bounded_int(value, 100000)
        elif key.endswith('Dates'):
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d\d-\d\d', day) for day in value): fail('CONFLICT_FACT_INVALID')
        elif value is not None and (not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value)):
            fail('CONFLICT_FACT_INVALID')
    observations = group.get('observations')
    if not isinstance(observations, list) or not 1 <= len(observations) <= 4: fail('CONFLICT_EVIDENCE_INVALID')
    evidence, sources = [], None
    for item in observations:
        if not isinstance(item, dict): fail('CONFLICT_EVIDENCE_INVALID')
        urls = item.get('sourceUrls', [item.get('sourceUrl')])
        if not isinstance(urls, list) or not 1 <= len(urls) <= 8: fail('CONFLICT_SOURCE_INVALID')
        canonical = sorted(set(source_identity(url) for url in [item.get('sourceUrl'), *urls]))
        if sources is not None and sources != canonical: fail('CONFLICT_EVIDENCE_INVALID')
        sources = canonical
        fetched = instant(item.get('fetchedAt'))
        if not fetched or not instant(row['rangeEnd'] + 'T00:00:00Z') <= fetched <= now + dt.timedelta(minutes=5): fail('CONFLICT_EVIDENCE_INVALID')
        clean = {'sourceUrl': item['sourceUrl'], 'sourceUrls': list(urls), 'fetchedAt': item['fetchedAt']}
        if clean not in evidence: evidence.append(clean)
    first, last = instant(group.get('firstFetchedAt')), instant(group.get('lastFetchedAt'))
    if (not first or not last or first > min(instant(item['fetchedAt']) for item in evidence) or last < max(instant(item['fetchedAt']) for item in evidence)
        or first < instant(row['rangeEnd'] + 'T00:00:00Z') or last > now + dt.timedelta(minutes=5)):
        fail('CONFLICT_EVIDENCE_INVALID')
    count = bounded_int(group.get('occurrenceCount'), 100000000)
    omitted = bounded_int(group.get('omittedObservationCount', 0), 100000000)
    if count < len(evidence): fail('CONFLICT_EVIDENCE_INVALID')
    return {'factId': digest({'sourceIdentity': sources, 'price': price}), 'sourceIdentity': sources,
            'price': price, 'completeSource': valid_complete_price(price, row['market'], row['stockCode'], row['rangeStart'], row['rangeEnd']),
            'observations': sorted(evidence, key=lambda item: (instant(item['fetchedAt']), item['sourceUrl'])),
            'firstFetchedAt': group['firstFetchedAt'], 'lastFetchedAt': group['lastFetchedAt'],
            'occurrenceCount': count, 'omittedObservationCount': omitted}


def registry_from_observations(value, now=None):
    current = clock(now)
    if (not isinstance(value, dict) or value.get('schemaVersion') != 1 or value.get('kind') != 'price-conflict-observations'
            or value.get('quarter') not in PHASES): fail('CONFLICT_REGISTRY_INVALID')
    quarter = value['quarter']; start, end, baseline = PHASES[quarter]
    if (value.get('targetPeriodEnd') != end or value.get('baselinePeriodEnd') != baseline
            or not instant(value.get('generatedAt')) or instant(value['generatedAt']) > current + dt.timedelta(minutes=5)
            or not isinstance(value.get('manifestContentHash'), str) or not HASH.fullmatch(value['manifestContentHash'])
            or value.get('dataCommit') is not None and not re.fullmatch(r'[a-f0-9]{40}', str(value['dataCommit']))
            or not isinstance(value.get('records'), list) or len(value['records']) > MAX_KEYS): fail('CONFLICT_REGISTRY_INVALID')
    records, seen = [], set()
    for item in value['records']:
        if not isinstance(item, dict): fail('CONFLICT_REGISTRY_INVALID')
        key = item.get('key'); row = identity(key, quarter)
        if key in seen or not isinstance(item.get('groups'), list) or not 1 <= len(item['groups']) <= MAX_FACTS: fail('CONFLICT_REGISTRY_INVALID')
        seen.add(key); unique = {}
        for group in item['groups']:
            fact = normalize_group(group, row, current)
            if fact['factId'] in unique: fail('CONFLICT_DUPLICATE_FACT')
            unique[fact['factId']] = fact
        complete = [fact for fact in unique.values() if fact['completeSource']]
        if not complete: continue
        ranges = {(fact['price']['low'], fact['price']['high']) for fact in complete}
        low, high = min(pair[0] for pair in ranges), max(pair[1] for pair in ranges)
        outliers = [fact for fact in unique.values() if not fact['completeSource'] and (fact['price']['low'] < low or fact['price']['high'] > high)]
        types = (['complete-source-disagreement'] if len(ranges) > 1 else []) + (['partial-extreme'] if outliers else [])
        if not types: continue
        facts = sorted(complete + outliers, key=lambda fact: fact['factId'])
        records.append({'key': key, **row, 'types': types, 'digest': digest({'key': key, 'types': types, 'factIds': [fact['factId'] for fact in facts]}),
                        'facts': facts, 'resolution': 'unresolved'})
    result = {'schemaVersion': 1, 'kind': 'price-conflict-registry', 'quarter': quarter, 'targetPeriodEnd': end,
              'baselinePeriodEnd': baseline, 'generatedAt': value['generatedAt'], 'dataCommit': value.get('dataCommit'),
              'manifestContentHash': value['manifestContentHash'], 'records': sorted(records, key=lambda row: row['key']),
              'scanSummary': {key: bounded_int(value.get(key), 10000000) for key in ('scannedCompanyPages', 'scannedFunds', 'eligiblePriceKeys')},
              'productionDataModified': False, 'automaticResolution': False}
    result['summary'] = {'conflictKeyCount': len(records), 'completeSourceConflictKeyCount': sum('complete-source-disagreement' in row['types'] for row in records),
                         'partialExtremeKeyCount': sum('partial-extreme' in row['types'] for row in records)}
    checked_bytes(result)
    return result


def normalize_registry(value, now=None):
    if not isinstance(value, dict) or value.get('kind') != 'price-conflict-registry' or not isinstance(value.get('records'), list): fail('CONFLICT_REGISTRY_INVALID')
    observation = {**value, **(value.get('scanSummary') or {}), 'kind': 'price-conflict-observations',
                   'records': [{'key': row.get('key'), 'groups': row.get('facts')} for row in value['records'] if isinstance(row, dict)]}
    if len(observation['records']) != len(value['records']): fail('CONFLICT_REGISTRY_INVALID')
    normalized = registry_from_observations(observation, now)
    if normalized != value: fail('CONFLICT_REGISTRY_INVALID')
    return normalized


def checked_bytes(value):
    raw = encoded(value) + b'\n'
    if len(raw) > MAX_BYTES or inspect_content(raw, MAX_BYTES): fail('CONFLICT_OUTPUT_REJECTED')
    return raw


def write_json(value, output):
    raw = checked_bytes(value); path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.conflict-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True); raise
    try: os.replace(temporary, path)
    finally: temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES: fail('CONFLICT_INPUT_INVALID')
    try: return json.loads(path.read_bytes())
    except (ValueError, OSError): fail('CONFLICT_INPUT_INVALID')


def scan_registry(data_root, quarter, now=None):
    if os.environ.get('GITHUB_ACTIONS') != 'true': fail('CONFLICT_REQUIRES_ACTIONS')
    if quarter not in PHASES: fail('CONFLICT_IDENTITY_INVALID')
    with tempfile.TemporaryDirectory(prefix='conflict-scan-') as temporary:
        output = Path(temporary) / 'observations.json'
        result = subprocess.run(['node', str(Path(__file__).with_name('price-conflict-scan.js')), '--data', str(data_root),
                                 '--output', str(output), '--registry-quarter', quarter], capture_output=True, timeout=600)
        if result.returncode: fail('CONFLICT_SCAN_FAILED')
        return registry_from_observations(read_json(output), now)


def empty_ledger(quarter):
    if quarter not in PHASES: fail('CONFLICT_IDENTITY_INVALID')
    return {'schemaVersion': 1, 'kind': 'price-conflict-ledger', 'quarter': quarter, 'entries': [],
            'productionDataModified': False, 'automaticResolution': False}


def normalize_ledger(value, quarter, now=None):
    if value is None: return empty_ledger(quarter)
    current = clock(now)
    if (not isinstance(value, dict) or value.get('schemaVersion') != 1 or value.get('kind') != 'price-conflict-ledger'
            or value.get('quarter') != quarter or not isinstance(value.get('entries'), list) or len(value['entries']) > MAX_KEYS): fail('CONFLICT_LEDGER_INVALID')
    result, seen = empty_ledger(quarter), set()
    for entry in value['entries']:
        if not isinstance(entry, dict): fail('CONFLICT_LEDGER_INVALID')
        identity(entry.get('key'), quarter)
        if entry['key'] in seen or not isinstance(entry.get('digests'), list) or not 1 <= len(entry['digests']) <= MAX_DIGESTS: fail('CONFLICT_LEDGER_INVALID')
        seen.add(entry['key']); histories, hashes = [], set()
        for history in entry['digests']:
            if (not isinstance(history, dict) or not isinstance(history.get('digest'), str) or not HASH.fullmatch(history['digest'])
                    or history['digest'] in hashes or not isinstance(history.get('rounds'), list) or not 1 <= len(history['rounds']) <= MAX_ATTEMPTS): fail('CONFLICT_LEDGER_INVALID')
            hashes.add(history['digest']); rounds, ids = [], set()
            for item in history['rounds']:
                receipt = normalize_receipt(item, now=current)
                if receipt['key'] != entry['key'] or receipt['digest'] != history['digest'] or receipt['quarter'] != quarter or receipt['attemptId'] in ids:
                    fail('CONFLICT_LEDGER_INVALID')
                ids.add(receipt['attemptId']); rounds.append(receipt)
            histories.append({'digest': history['digest'], 'rounds': sorted(rounds, key=lambda row: (instant(row['attemptedAt']), row['attemptId']))})
        result['entries'].append({'key': entry['key'], 'digests': sorted(histories, key=lambda row: row['digest'])})
    result['entries'].sort(key=lambda row: row['key'])
    if result != value: fail('CONFLICT_LEDGER_INVALID')
    checked_bytes(result); return result


def union_ledgers(first, second, quarter, now=None):
    """Monotonic union, including unknown checkpoints; never drop old digests."""
    a, b = (normalize_ledger(value, quarter, now) for value in (first, second))
    result = empty_ledger(quarter); entries = {}
    for ledger in (a, b):
        for entry in ledger['entries']:
            histories = entries.setdefault(entry['key'], {})
            for history in entry['digests']:
                rounds = histories.setdefault(history['digest'], {})
                for receipt in history['rounds']:
                    old = rounds.get(receipt['attemptId'])
                    if old is None: rounds[receipt['attemptId']] = copy.deepcopy(receipt); continue
                    if any(old[key] != receipt[key] for key in ('quarter', 'key', 'digest', 'attemptId', 'attemptedAt')):
                        fail('CONFLICT_LEDGER_HISTORY_MISMATCH')
                    merged = copy.deepcopy(old)
                    for index, event in enumerate(receipt['requests']):
                        if index == len(merged['requests']): merged['requests'].append(copy.deepcopy(event)); continue
                        prior = merged['requests'][index]
                        if any(prior[key] != event[key] for key in ('factId', 'provider', 'sourceUrl', 'startedAt', 'storedLow', 'storedHigh', 'completeSource', 'originalFetchedAt')):
                            fail('CONFLICT_LEDGER_HISTORY_MISMATCH')
                        if prior['outcome'] == 'unknown': merged['requests'][index] = copy.deepcopy(event)
                        elif event['outcome'] != 'unknown' and prior != event: fail('CONFLICT_LEDGER_HISTORY_MISMATCH')
                    merged['verificationStatus'] = verification_status(merged['requests'])
                    rounds[receipt['attemptId']] = merged
    if len(entries) > MAX_KEYS: fail('CONFLICT_LEDGER_LIMIT')
    for key, histories in sorted(entries.items()):
        if len(histories) > MAX_DIGESTS or any(len(rounds) > MAX_ATTEMPTS for rounds in histories.values()): fail('CONFLICT_LEDGER_LIMIT')
        result['entries'].append({'key': key, 'digests': [{'digest': value, 'rounds': sorted(rounds.values(), key=lambda row: (instant(row['attemptedAt']), row['attemptId']))}
                                                        for value, rounds in sorted(histories.items())]})
    return normalize_ledger(result, quarter, now)


def normalize_restored_state(value, now=None):
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'kind', 'repository', 'workflowPath', 'currentRun', 'currentDataCommit', 'restoredFrom', 'ledgers', 'bootstrap'}
            or value.get('schemaVersion') != 1 or value.get('kind') != 'price-conflict-restored-state'
            or value.get('repository') != 'yt-feng/record-lab' or value.get('workflowPath') != '.github/workflows/recheck-price-conflicts.yml'
            or not isinstance(value.get('ledgers'), dict) or set(value['ledgers']) != set(PHASES)
            or any(not isinstance(ledger, dict) for ledger in value['ledgers'].values()) or value.get('bootstrap') != LEDGER_BOOTSTRAP):
        fail('CONFLICT_RESTORED_STATE_INVALID')
    current, prior = value.get('currentRun'), value.get('restoredFrom')
    if (not isinstance(current, dict) or set(current) != {'runId', 'runAttempt', 'sourceCommit'}
            or not isinstance(prior, dict) or set(prior) != {'runId', 'runAttempt', 'sourceCommit', 'dataCommit', 'artifactId', 'archiveSha256', 'conclusion'}):
        fail('CONFLICT_RESTORED_STATE_INVALID')
    for item in (current, prior):
        if any(type(item.get(key)) is not int or item[key] <= 0 for key in ('runId', 'runAttempt')) or not re.fullmatch(r'[a-f0-9]{40}', str(item.get('sourceCommit'))):
            fail('CONFLICT_RESTORED_STATE_INVALID')
    if (current['runAttempt'] != 1 or current['runId'] == prior['runId'] or type(prior['artifactId']) is not int or prior['artifactId'] <= 0
            or not HASH.fullmatch(str(prior['archiveSha256'])) or prior['conclusion'] not in ('success', 'failure', 'cancelled', 'timed_out')
            or not re.fullmatch(r'[a-f0-9]{40}', str(prior.get('dataCommit')))
            or not re.fullmatch(r'[a-f0-9]{40}', str(value.get('currentDataCommit')))):
        fail('CONFLICT_RESTORED_STATE_INVALID')
    clean = copy.deepcopy(value)
    clean['ledgers'] = {quarter: normalize_ledger(value['ledgers'][quarter], quarter, now) for quarter in PHASES}
    checked_bytes(clean); return clean


def request_choices(row, now):
    choices = []
    for fact in row['facts']:
        for evidence in reversed(fact['observations']):
            try:
                _, provider, _, _ = diagnostic.request_for(row, evidence, now)
            except (diagnostic.DiagnosticError, ValueError, KeyError): continue
            choices.append({'factId': fact['factId'], 'provider': provider, 'sourceUrl': evidence['sourceUrl'],
                            'storedLow': fact['price']['low'], 'storedHigh': fact['price']['high'], 'completeSource': fact['completeSource'],
                            'originalFetchedAt': evidence['fetchedAt']})
            break
    # Prefer a differing pair from independent sources; same-provider historical
    # facts remain a one-source verification, never a two-source agreement.
    pairs = [(a, b) for i, a in enumerate(choices) for b in choices[i + 1:]
             if (a['storedLow'], a['storedHigh']) != (b['storedLow'], b['storedHigh'])
             and (a['completeSource'] or b['completeSource'])]
    pairs.sort(key=lambda pair: (pair[0]['provider'] == pair[1]['provider'],
                               not all(item['completeSource'] for item in pair), pair[0]['factId'], pair[1]['factId']))
    if not pairs: return []
    unique = []
    for item in pairs[0]:
        if not any(old['provider'] == item['provider'] and source_identity(old['sourceUrl']) == source_identity(item['sourceUrl']) for old in unique): unique.append(item)
    return unique


def plan_rechecks(registry, previous_ledger=None, *, max_keys=10, max_requests=20, now=None):
    current = clock(now); registry = normalize_registry(registry, current)
    ledger = normalize_ledger(previous_ledger, registry['quarter'], current)
    bounded_int(max_keys, MAX_PLAN_KEYS, 'CONFLICT_LIMIT_INVALID'); bounded_int(max_requests, MAX_REQUESTS, 'CONFLICT_LIMIT_INVALID')
    entries = {entry['key']: entry for entry in ledger['entries']}
    candidates, blocked = [], {'exhausted': 0, 'historyFull': 0, 'sourceUnsupported': 0, 'limited': 0}
    for row in registry['records']:
        histories = entries.get(row['key'], {}).get('digests', [])
        history = next((item for item in histories if item['digest'] == row['digest']), None)
        rounds = history['rounds'] if history else []
        if len(rounds) >= MAX_ATTEMPTS: blocked['exhausted'] += 1; continue
        if not history and len(histories) >= MAX_DIGESTS or row['key'] not in entries and len(entries) >= MAX_KEYS:
            blocked['historyFull'] += 1; continue
        choices = request_choices(row, current)
        if not choices: blocked['sourceUnsupported'] += 1; continue
        candidates.append((len(rounds), max((instant(item['attemptedAt']) for item in rounds), default=dt.datetime.min.replace(tzinfo=dt.timezone.utc)), row, choices))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]['key']))
    plan = {'schemaVersion': 1, 'kind': 'price-conflict-plan', 'quarter': registry['quarter'], 'plannedAt': current.isoformat(),
            'registryManifestContentHash': registry['manifestContentHash'], 'items': [], 'maxAttemptsPerDigest': MAX_ATTEMPTS,
            'maxRequestsPerRound': 2, 'productionDataModified': False, 'automaticResolution': False}
    total = 0
    for attempts, _, row, choices in candidates:
        if len(plan['items']) >= max_keys or total + len(choices) > max_requests: blocked['limited'] += 1; continue
        item = {'key': row['key'], 'digest': row['digest'], 'round': attempts + 1, 'types': row['types'],
                **{key: row[key] for key in ('market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd', 'calendarId')},
                'requests': choices}
        item['attemptId'] = digest({'quarter': plan['quarter'], 'plannedAt': plan['plannedAt'], 'item': item})
        plan['items'].append(item); total += len(choices)
    plan['summary'] = {'plannedKeyCount': len(plan['items']), 'plannedRequestCount': total, 'blocked': blocked}
    checked_bytes(plan); return plan


def validate_plan(plan, registry, now=None):
    current = clock(now); registry = normalize_registry(registry, current)
    if (not isinstance(plan, dict) or plan.get('schemaVersion') != 1 or plan.get('kind') != 'price-conflict-plan'
            or plan.get('quarter') != registry['quarter'] or not instant(plan.get('plannedAt'))
            or instant(plan['plannedAt']) > current + dt.timedelta(minutes=5) or not isinstance(plan.get('items'), list)
            or len(plan['items']) > MAX_PLAN_KEYS or plan.get('maxAttemptsPerDigest') != MAX_ATTEMPTS
            or plan.get('maxRequestsPerRound') != 2 or plan.get('registryManifestContentHash') != registry['manifestContentHash']
            or plan.get('productionDataModified') is not False or plan.get('automaticResolution') is not False): fail('CONFLICT_PLAN_INVALID')
    rows = {row['key']: row for row in registry['records']}; seen = set(); count = 0
    for item in plan['items']:
        if not isinstance(item, dict): fail('CONFLICT_PLAN_INVALID')
        row = rows.get(item.get('key'))
        if not row or item['key'] in seen or item.get('digest') != row['digest']: fail('CONFLICT_PLAN_STALE')
        seen.add(item['key'])
        if (any(item.get(key) != row[key] for key in ('market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd', 'calendarId', 'types'))
                or type(item.get('round')) is not int or not 1 <= item['round'] <= MAX_ATTEMPTS
                or not isinstance(item.get('requests'), list) or not 1 <= len(item['requests']) <= 2): fail('CONFLICT_PLAN_INVALID')
        valid = request_choices(row, instant(plan['plannedAt']))
        if item['requests'] != valid: fail('CONFLICT_PLAN_INVALID')
        raw = {key: value for key, value in item.items() if key != 'attemptId'}
        if item.get('attemptId') != digest({'quarter': plan['quarter'], 'plannedAt': plan['plannedAt'], 'item': raw}): fail('CONFLICT_PLAN_INVALID')
        count += len(valid)
    if count > MAX_REQUESTS: fail('CONFLICT_PLAN_INVALID')
    return plan


def verification_status(requests):
    if any(row['outcome'] == 'unknown' for row in requests): return 'unknown'
    observed = [row for row in requests if row['outcome'] == 'observed']
    if len(observed) != 2: return 'insufficient-verified-responses'
    if len({row['provider'] for row in observed}) != 2: return 'single-source-only'
    if any(row['quote']['completeness'] != 'complete' for row in observed): return 'partial-responses'
    a, b = (row['quote'] for row in observed)
    return 'current-ranges-agree' if (a['low'], a['high']) == (b['low'], b['high']) else 'repeatable-source-difference'


def validate_quote(quote, request, row, now):
    allowed = {'groupIndex', 'requested', 'observedAt', 'status', 'splitInspectionExtended', 'sourceUrl', 'responseSha256',
               'responseBytes', 'fetchedAt', 'symbol', 'currency', 'splitEventCount', 'splitDates', 'metadataStatus',
               'low', 'high', 'priceBasis', 'observedCount', 'completeness', 'expectedSessionCount', 'observedDateSetHash',
               'expectedDateSetHash', 'missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount',
               'storedRangeExactlyReproduced', 'errorCode'}
    if (not isinstance(quote, dict) or set(quote) - allowed or quote.get('groupIndex') != 0 or quote.get('requested') is not True
            or quote.get('status') != request['outcome'] or not instant(quote.get('observedAt'))
            or not instant(request['startedAt']) <= instant(quote['observedAt']) <= now + dt.timedelta(minutes=5)):
        fail('CONFLICT_RECEIPT_INVALID')
    for name, value in quote.items():
        if name.endswith('Url'):
            if not public_url(value) or len(value) > 2048: fail('CONFLICT_RECEIPT_INVALID')
            try: diagnostic.request_for(row, {'sourceUrl': value}, now)
            except Exception: fail('CONFLICT_RECEIPT_INVALID')
            if source_identity(value) != source_identity(request['sourceUrl']): fail('CONFLICT_RECEIPT_INVALID')
        elif isinstance(value, str) and len(value) > 128: fail('CONFLICT_RECEIPT_INVALID')
        elif isinstance(value, dict) or isinstance(value, list) and name != 'splitDates': fail('CONFLICT_RECEIPT_INVALID')
        if name.endswith('Count') or name == 'responseBytes':
            if value is not None: bounded_int(value, diagnostic.MAX_RESPONSE, 'CONFLICT_RECEIPT_INVALID')
    if 'errorCode' in quote and not re.fullmatch(r'[A-Z][A-Z0-9_]{1,79}', str(quote['errorCode'])): fail('CONFLICT_RECEIPT_INVALID')
    if 'responseSha256' in quote and not HASH.fullmatch(str(quote['responseSha256'])): fail('CONFLICT_RECEIPT_INVALID')
    if 'fetchedAt' in quote and (not instant(quote['fetchedAt']) or not instant(request['startedAt']) <= instant(quote['fetchedAt']) <= now + dt.timedelta(minutes=5)): fail('CONFLICT_RECEIPT_INVALID')
    if 'splitDates' in quote and (not isinstance(quote['splitDates'], list) or len(quote['splitDates']) > 3
        or any(not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d\d-\d\d', day) for day in quote['splitDates'])): fail('CONFLICT_RECEIPT_INVALID')
    if request['outcome'] != 'observed': return
    if (quote.get('currency') != row['currency'] or quote.get('priceBasis') != 'unadjusted'
            or any(type(quote.get(key)) not in (int, float) or not math.isfinite(quote[key]) for key in ('low', 'high'))
            or not 0 < quote['low'] <= quote['high'] or quote.get('completeness') not in ('complete', 'partial', 'calendar-unavailable')):
        fail('CONFLICT_RECEIPT_INVALID')
    if quote.get('storedRangeExactlyReproduced') is not ((quote['low'], quote['high']) == (request['storedLow'], request['storedHigh'])):
        fail('CONFLICT_RECEIPT_INVALID')
    if quote['completeness'] == 'complete':
        expected = expected_sessions(row['market'], row['rangeStart'], row['rangeEnd'])
        if (not expected or quote.get('observedCount') != len(expected) or quote.get('expectedSessionCount') != len(expected)
                or quote.get('expectedDateSetHash') != date_set_hash(expected) or quote.get('observedDateSetHash') != date_set_hash(expected)
                or any(type(quote.get(key)) is not int or quote[key] != 0 for key in ('missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount'))):
            fail('CONFLICT_RECEIPT_INVALID')


def normalize_receipt(value, now=None):
    current = clock(now)
    if not isinstance(value, dict) or value.get('schemaVersion') != 1 or value.get('kind') != 'price-conflict-receipt': fail('CONFLICT_RECEIPT_INVALID')
    row = identity(value.get('key'), value.get('quarter'))
    if (any(not isinstance(value.get(key), str) or not HASH.fullmatch(value[key]) for key in ('digest', 'attemptId'))
            or not instant(value.get('attemptedAt')) or instant(value['attemptedAt']) > current + dt.timedelta(minutes=5)
            or value.get('resolution') != 'unresolved' or value.get('automaticResolution') is not False
            or not isinstance(value.get('requests'), list) or not 1 <= len(value['requests']) <= 2): fail('CONFLICT_RECEIPT_INVALID')
    for request in value['requests']:
        if (not isinstance(request, dict) or not isinstance(request.get('factId'), str) or not HASH.fullmatch(request['factId'])
                or not public_url(request.get('sourceUrl')) or not instant(request.get('startedAt'))
                or not instant(value['attemptedAt']) <= instant(request['startedAt']) <= current + dt.timedelta(minutes=5)
                or request.get('outcome') not in ('unknown', 'observed', 'unavailable') or request.get('provider') not in ('eastmoney', 'tencent', 'yahoo')):
            fail('CONFLICT_RECEIPT_INVALID')
        if (not instant(request.get('originalFetchedAt')) or instant(request['originalFetchedAt']) > instant(request['startedAt'])
                or type(request.get('completeSource')) is not bool
                or any(type(request.get(key)) not in (int, float) or not math.isfinite(request[key]) for key in ('storedLow', 'storedHigh'))
                or not 0 < request['storedLow'] <= request['storedHigh']): fail('CONFLICT_RECEIPT_INVALID')
        if request.get('outcome') != 'unknown' and not isinstance(request.get('quote'), dict): fail('CONFLICT_RECEIPT_INVALID')
        if request['outcome'] == 'unknown' and 'quote' in request: fail('CONFLICT_RECEIPT_INVALID')
        try: _, provider, _, _ = diagnostic.request_for(row, request, current)
        except Exception: fail('CONFLICT_RECEIPT_INVALID')
        if provider != request['provider']: fail('CONFLICT_RECEIPT_INVALID')
        if request['outcome'] != 'unknown': validate_quote(request['quote'], request, row, current)
    if value.get('verificationStatus') not in ('unknown', 'insufficient-verified-responses', 'single-source-only', 'partial-responses',
                                                'current-ranges-agree', 'repeatable-source-difference'): fail('CONFLICT_RECEIPT_INVALID')
    # Public receipts are small, explicitly whitelisted diagnostic summaries.
    allowed = {'schemaVersion', 'kind', 'quarter', 'key', 'digest', 'attemptId', 'attemptedAt', 'requests', 'verificationStatus', 'resolution', 'automaticResolution'}
    if set(value) != allowed or any(set(item) - {'factId', 'provider', 'sourceUrl', 'startedAt', 'outcome', 'quote',
                                               'storedLow', 'storedHigh', 'completeSource', 'originalFetchedAt'} for item in value['requests']): fail('CONFLICT_RECEIPT_INVALID')
    if value['verificationStatus'] != verification_status(value['requests']) or len(encoded(value)) > 16000: fail('CONFLICT_RECEIPT_INVALID')
    checked_bytes(value); return copy.deepcopy(value)


def execute_rechecks(plan, registry, previous_ledger=None, *, checkpoint, fetch=None, now=None, should_start=None):
    """Checkpoints are mandatory. A checkpoint failure prevents that request.

    The callback receives the entire tiny receipt list after each request-start
    and result, suitable for atomic write_json. A supplied transport is for
    synthetic tests; the default remains the existing Actions-only transport.
    """
    current = clock(now); validate_plan(plan, registry, current)
    ledger = normalize_ledger(previous_ledger, plan['quarter'], current)
    if not callable(checkpoint): fail('CONFLICT_CHECKPOINT_REQUIRED')
    if fetch is None and os.environ.get('GITHUB_ACTIONS') != 'true': fail('CONFLICT_REQUIRES_ACTIONS')
    transport = fetch or diagnostic.ActionsTransport()
    existing = {(entry['key'], history['digest']): history['rounds'] for entry in ledger['entries'] for history in entry['digests']}
    histories = {entry['key']: entry['digests'] for entry in ledger['entries']}
    receipts, skipped = [], 0
    for item in plan['items']:
        old = existing.get((item['key'], item['digest']), [])
        if (len(old) != item['round'] - 1 or any(row['attemptId'] == item['attemptId'] for row in old)
                or not old and len(histories.get(item['key'], [])) >= MAX_DIGESTS
                or item['key'] not in histories and len(histories) >= MAX_KEYS): skipped += 1; continue
        receipt = None; responses = []
        for request in item['requests']:
            if should_start is not None and not should_start(): break
            started = (clock(now)).isoformat()
            # Validate once more before charging, using the same endpoint guard
            # as the independently deployed diagnostic.
            diagnostic.request_for(item, request, clock(now))
            if receipt is None:
                receipt = {'schemaVersion': 1, 'kind': 'price-conflict-receipt', 'quarter': plan['quarter'],
                           'key': item['key'], 'digest': item['digest'], 'attemptId': item['attemptId'], 'attemptedAt': started,
                           'requests': [], 'verificationStatus': 'unknown', 'resolution': 'unresolved', 'automaticResolution': False}
                receipts.append(receipt)
            event = {**request, 'startedAt': started, 'outcome': 'unknown'}
            receipt['requests'].append(event); receipt['verificationStatus'] = 'unknown'
            checkpoint(copy.deepcopy(receipts))
            # Reuse diagnostic parsing, response limits, split safeguards and
            # strict daily comparisons. Its fetch hook wraps only this request.
            group = {'sourceUrl': request['sourceUrl'], 'low': request['storedLow'], 'high': request['storedHigh'], 'reusableCompleteSource': True}
            sample = {'kind': 'price-conflict-diagnostic', 'quarters': [{'quarter': plan['quarter'], 'selected': [
                {**item, 'groups': [group], 'differences': [{'firstGroup': 0, 'secondGroup': 0}]}]}]}
            diagnostic.requery(sample, fetch=transport, now=now)
            quote = sample['quarters'][0]['selected'][0]['requotes'][0]
            event['outcome'] = quote['status']; event['quote'] = quote
            responses.append(event)
            # Exact quarter extrema only, never a daily agreement proof or
            # authorization to replace any stored quote.
            receipt['verificationStatus'] = verification_status(responses)
            checkpoint(copy.deepcopy(receipts))
    result = {'schemaVersion': 1, 'kind': 'price-conflict-execution', 'quarter': plan['quarter'], 'receipts': receipts,
              'summary': {'attemptedKeyCount': len(receipts), 'attemptedRequestCount': sum(len(row['requests']) for row in receipts), 'skippedKeyCount': skipped},
              'productionDataModified': False, 'automaticResolution': False}
    checked_bytes(result); return result


def merge_receipts(registry, previous_ledger, plan, receipts, *, now=None):
    """Validate against the original frozen registry/plan, including late work.

    Pass the registry used to make the plan, not a newer registry. Its ledger
    can then be reused with any later registry; old digests cannot resolve new
    facts. Request-start and result receipts are monotonic updates of one round.
    """
    current = clock(now); validate_plan(plan, registry, current)
    ledger = normalize_ledger(previous_ledger, plan['quarter'], current)
    if not isinstance(receipts, list) or len(receipts) > MAX_PLAN_KEYS: fail('CONFLICT_RECEIPT_INVALID')
    plans = {item['attemptId']: item for item in plan['items']}
    for raw in receipts:
        receipt = normalize_receipt(raw, current); item = plans.get(receipt['attemptId'])
        if (not item or any(receipt[key] != item[key] for key in ('key', 'digest')) or receipt['quarter'] != plan['quarter']
            or instant(receipt['attemptedAt']) < instant(plan['plannedAt'])): fail('CONFLICT_RECEIPT_INVALID')
        if len(receipt['requests']) > len(item['requests']): fail('CONFLICT_RECEIPT_INVALID')
        for actual, requested in zip(receipt['requests'], item['requests']):
            if any(actual[key] != requested[key] for key in ('factId', 'provider', 'sourceUrl', 'storedLow', 'storedHigh', 'completeSource', 'originalFetchedAt')):
                fail('CONFLICT_RECEIPT_INVALID')
        entry = next((row for row in ledger['entries'] if row['key'] == receipt['key']), None)
        if entry is None:
            if len(ledger['entries']) >= MAX_KEYS: fail('CONFLICT_LEDGER_LIMIT')
            entry = {'key': receipt['key'], 'digests': []}; ledger['entries'].append(entry)
        history = next((row for row in entry['digests'] if row['digest'] == receipt['digest']), None)
        if history is None:
            if len(entry['digests']) >= MAX_DIGESTS: fail('CONFLICT_LEDGER_LIMIT')
            history = {'digest': receipt['digest'], 'rounds': []}; entry['digests'].append(history)
        old = next((row for row in history['rounds'] if row['attemptId'] == receipt['attemptId']), None)
        if old:
            if old['attemptedAt'] != receipt['attemptedAt']: fail('CONFLICT_RECEIPT_REPLAY_INVALID')
            for a, b in zip(old['requests'], receipt['requests']):
                if any(a[key] != b[key] for key in ('factId', 'provider', 'sourceUrl', 'startedAt', 'storedLow', 'storedHigh', 'completeSource', 'originalFetchedAt')):
                    fail('CONFLICT_RECEIPT_REPLAY_INVALID')
                if a['outcome'] != 'unknown' and a != b:
                    if b['outcome'] == 'unknown': break
                    fail('CONFLICT_RECEIPT_REPLAY_INVALID')
            else:
                if len(receipt['requests']) >= len(old['requests']): history['rounds'][history['rounds'].index(old)] = receipt
            continue
        if len(history['rounds']) != item['round'] - 1 or len(history['rounds']) >= MAX_ATTEMPTS: fail('CONFLICT_RECEIPT_BUDGET_MISMATCH')
        history['rounds'].append(receipt)
    for entry in ledger['entries']:
        entry['digests'].sort(key=lambda row: row['digest'])
        for history in entry['digests']: history['rounds'].sort(key=lambda row: (instant(row['attemptedAt']), row['attemptId']))
    ledger['entries'].sort(key=lambda row: row['key'])
    return normalize_ledger(ledger, plan['quarter'], current)


def run(data_root, output, *, quarters='both', max_keys=8, max_requests=16, now=None, fetch=None, restored_state=None):
    """Actions artifact run. The input tree is read-only, including its ledger."""
    if os.environ.get('GITHUB_ACTIONS') != 'true': fail('CONFLICT_REQUIRES_ACTIONS')
    bounded_int(max_keys, MAX_PLAN_KEYS, 'CONFLICT_LIMIT_INVALID'); bounded_int(max_requests, MAX_REQUESTS, 'CONFLICT_LIMIT_INVALID')
    if quarters not in ('both', *PHASES): fail('CONFLICT_ARGUMENTS_INVALID')
    source, destination = Path(data_root).resolve(), Path(output).resolve()
    if destination.is_relative_to(source): fail('CONFLICT_OUTPUT_INSIDE_SOURCE')
    selected = list(PHASES) if quarters == 'both' else [quarters]
    restored = normalize_restored_state(restored_state, now) if restored_state is not None else None
    if fetch is None and max_keys and max_requests:
        if restored is None: fail('CONFLICT_PRIOR_LEDGER_REQUIRED')
        current = restored['currentRun']
        if (os.environ.get('GITHUB_REPOSITORY') != restored['repository']
                or os.environ.get('GITHUB_RUN_ID') != str(current['runId']) or os.environ.get('GITHUB_RUN_ATTEMPT') != str(current['runAttempt'])
                or os.environ.get('GITHUB_SHA') != current['sourceCommit']
                or os.environ.get('PRICE_DIAGNOSTIC_DATA_COMMIT') != restored['currentDataCommit']):
            fail('CONFLICT_CURRENT_RUN_MISMATCH')
    if restored and selected != list(PHASES): fail('CONFLICT_RESTORED_QUARTERS_REQUIRED')
    if (destination / 'provenance.json').exists() or any((destination / quarter / name).exists() for quarter in selected
        for name in ('registry.json', 'plan.json', 'ledger.json', 'input-ledger.json', 'receipts.json')): fail('CONFLICT_OUTPUT_ALREADY_EXISTS')
    inherited = {}
    for quarter in selected:
        published = source / quarter / 'price-conflict-ledger.json'
        ledger = read_json(published) if published.exists() else None
        if published.exists() and not isinstance(ledger, dict): fail('CONFLICT_LEDGER_INVALID')
        inherited[quarter] = union_ledgers(ledger, restored['ledgers'][quarter] if restored else None, quarter, now)
    # Save both quarters before the first scan/request. A failed first quarter
    # must not erase the second quarter's inherited attempt history.
    if restored:
        write_json({'schemaVersion': 1, 'kind': 'price-conflict-provenance', 'repository': restored['repository'],
                    'workflowPath': restored['workflowPath'], **restored['currentRun'], 'dataCommit': restored['currentDataCommit'],
                    'restoredFrom': restored['restoredFrom'], 'bootstrap': restored['bootstrap'],
                    'inputLedgerHashes': {q: hashlib.sha256(encoded(ledger)).hexdigest() for q, ledger in inherited.items()}}, destination / 'provenance.json')
    for quarter, ledger in inherited.items():
        if restored: write_json(ledger, destination / quarter / 'input-ledger.json')
        write_json(ledger, destination / quarter / 'ledger.json')
    result = {'schemaVersion': 1, 'kind': 'price-conflict-run', 'generatedAt': clock(now).isoformat(),
              'status': 'complete', 'quarters': [], 'maxKeys': max_keys, 'maxRequests': max_requests,
              'productionDataModified': False, 'automaticResolution': False}
    remaining_keys, remaining_requests = max_keys, max_requests
    for position, quarter in enumerate(selected):
        registry = scan_registry(source, quarter, now)
        ledger = inherited[quarter]
        directory = destination / quarter
        write_json(registry, directory / 'registry.json')
        left = len(selected) - position
        plan = plan_rechecks(registry, ledger, max_keys=(remaining_keys + left - 1) // left,
                             max_requests=(remaining_requests + left - 1) // left, now=now)
        write_json(plan, directory / 'plan.json'); write_json(ledger, directory / 'ledger.json')
        def checkpoint(receipts):
            nonlocal ledger
            ledger = merge_receipts(registry, ledger, plan, receipts, now=now)
            write_json(receipts, directory / 'receipts.json')
            write_json(ledger, directory / 'ledger.json')
        execution = execute_rechecks(plan, registry, ledger, checkpoint=checkpoint, fetch=fetch, now=now)
        # A zero-request run still provides explicit empty receipts for replay.
        if not execution['receipts']: write_json([], directory / 'receipts.json')
        write_json(execution, directory / 'execution.json')
        remaining_keys -= execution['summary']['attemptedKeyCount']; remaining_requests -= execution['summary']['attemptedRequestCount']
        result['quarters'].append({'quarter': quarter, 'registry': registry['summary'], 'plan': plan['summary'],
                                   'execution': execution['summary'], 'ledgerKeyCount': len(ledger['entries']),
                                   'ledgerRoundCount': sum(len(history['rounds']) for entry in ledger['entries'] for history in entry['digests']),
                                   'verificationCounts': {status: sum(receipt['verificationStatus'] == status for receipt in execution['receipts'])
                                                          for status in sorted({receipt['verificationStatus'] for receipt in execution['receipts']})},
                                   'resolution': 'unresolved'})
    result['summary'] = {'conflictKeyCount': sum(row['registry']['conflictKeyCount'] for row in result['quarters']),
                         'attemptedKeyCount': max_keys - remaining_keys, 'attemptedRequestCount': max_requests - remaining_requests,
                         'quarterCount': len(result['quarters'])}
    if len(encoded(result)) > 65536: fail('CONFLICT_OUTPUT_REJECTED')
    write_json(result, destination / 'result.json')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('run', 'scan', 'plan', 'execute', 'merge'))
    for field in ('data', 'quarter', 'registry', 'ledger', 'plan', 'receipts', 'output', 'checkpoint', 'restore-state'):
        parser.add_argument('--' + field)
    parser.add_argument('--max-keys', type=int, default=10); parser.add_argument('--max-requests', type=int, default=20)
    parser.add_argument('--quarters', choices=('both', *PHASES), default='both')
    args = parser.parse_args(argv)
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true': fail('CONFLICT_REQUIRES_ACTIONS')
        if not args.output: fail('CONFLICT_ARGUMENTS_INVALID')
        targets = [args.output] + ([args.checkpoint] if args.checkpoint else [])
        if args.data and any(Path(target).resolve().is_relative_to(Path(args.data).resolve()) for target in targets): fail('CONFLICT_OUTPUT_INSIDE_SOURCE')
        for source in (args.registry, args.ledger, args.plan, args.receipts, args.restore_state):
            if source and any(Path(source).resolve() == Path(target).resolve() for target in targets): fail('CONFLICT_OUTPUT_OVERWRITES_INPUT')
        if args.command == 'run':
            result = run(args.data, args.output, quarters=args.quarters, max_keys=args.max_keys, max_requests=args.max_requests,
                         restored_state=read_json(args.restore_state) if args.restore_state else None)
        elif args.command == 'scan': result = scan_registry(args.data, args.quarter)
        else:
            registry = normalize_registry(read_json(args.registry))
            ledger = read_json(args.ledger) if args.ledger else None
            if args.command == 'plan': result = plan_rechecks(registry, ledger, max_keys=args.max_keys, max_requests=args.max_requests)
            elif args.command == 'execute':
                if not args.checkpoint or Path(args.checkpoint).resolve() == Path(args.output).resolve(): fail('CONFLICT_ARGUMENTS_INVALID')
                result = execute_rechecks(read_json(args.plan), registry, ledger, checkpoint=lambda receipts: write_json(receipts, args.checkpoint))
            else: result = merge_receipts(registry, ledger, read_json(args.plan), read_json(args.receipts))
        if args.command != 'run': write_json(result, args.output)
        print(json.dumps({'status': 'ok', 'kind': result['kind'], 'summary': result.get('summary'), 'productionDataModified': False}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ConflictError) and re.fullmatch(r'CONFLICT_[A-Z_]+', str(error)) else 'CONFLICT_FAILED'
        print(json.dumps({'status': 'failed', 'errorCode': code, 'productionDataModified': False}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
