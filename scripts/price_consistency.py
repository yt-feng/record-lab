"""Two-observation witnesses of unresolved security-quarter price conflicts."""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
import math
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from price_coverage import CALENDAR, expected_sessions, date_set_hash, valid_complete_price
from price_patches import public_url, stock_key

PRICE_FIELDS = ('status', 'low', 'high', 'market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd',
                'coverageVersion', 'calendarId', 'sessionScope', 'completeness', 'observedCount', 'observedStart', 'observedEnd',
                'expectedSessionCount', 'expectedDateSetHash', 'observedDateSetHash', 'missingSessionCount', 'missingSessionDates',
                'unexpectedSessionCount', 'unexpectedSessionDates', 'rejectedRowCount', 'conflictingDateCount')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()


def canonical_digest(value):
    """Exactly the registry's float.hex numeric canonicalization."""
    def canonical(item):
        if type(item) in (int, float):
            if not math.isfinite(item): raise ValueError('invalid number')
            return {'number': float(item).hex()}
        if isinstance(item, list): return [canonical(row) for row in item]
        if isinstance(item, dict): return {key: canonical(row) for key, row in item.items()}
        return item
    return hashlib.sha256(_encoded(canonical(value))).hexdigest()


def source_identity(source):
    if (not public_url(source) or len(source) > 2048 or re.search(r'[\s\\#\x7f\ud800-\udfff]', source)):
        raise ValueError('invalid source')
    url = urlparse(source)
    pairs = parse_qsl(url.query, keep_blank_values=True)
    if url.hostname in {'query1.finance.yahoo.com', 'query2.finance.yahoo.com'}:
        url = url._replace(netloc='query1.finance.yahoo.com')
        pairs = [(key, value) for key, value in pairs if key != 'period2']
    return urlunparse(url._replace(query=urlencode(sorted(pairs))))


def _date(value):
    try:
        return isinstance(value, str) and bool(re.fullmatch(r'\d{4}-\d\d-\d\d', value)) and dt.date.fromisoformat(value).isoformat() == value
    except ValueError: return False


def _instant(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)', value): return None
    try:
        if not _date(value[:10]) or int(value[11:13]) > 23 or int(value[14:16]) > 59 or int(value[17:19]) > 59: return None
        if not value.endswith('Z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59): return None
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: return None


def _identity(value):
    if not isinstance(value, dict) or not stock_key(value): return None
    row = {key: value.get(key) for key in ('market', 'stockCode', 'currency', 'rangeStart', 'rangeEnd', 'calendarId')}
    if row['currency'] != CALENDAR['markets'][row['market']]['currency'] or row['calendarId'] != CALENDAR['id']: return None
    start, end = row['rangeStart'], row['rangeEnd']
    if not _date(start) or not _date(end): return None
    day = dt.date.fromisoformat(end)
    if (day.month, day.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)) or start != f'{day.year:04d}-{day.month - 2:02d}-01': return None
    if not expected_sessions(row['market'], start, end): return None
    return row


def _price(value, row):
    if not isinstance(value, dict) or any(key not in value for key in PRICE_FIELDS): return None
    price = {key: copy.deepcopy(value[key]) for key in PRICE_FIELDS}
    if (any(price[key] != row[key] for key in row) or price['status'] != 'observed' or price['priceBasis'] != 'unadjusted'
            or price['coverageVersion'] != 2 or type(price['coverageVersion']) is not int
            or price['sessionScope'] != CALENDAR['sessionScope'] or price['completeness'] not in ('complete', 'partial')
            or any(type(price[key]) not in (int, float) or not math.isfinite(price[key]) for key in ('low', 'high'))
            or not 0 < price['low'] <= price['high']): return None
    for key in ('observedCount', 'expectedSessionCount', 'missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount'):
        if type(price[key]) is not int or not 0 <= price[key] <= 100000: return None
    if price['rejectedRowCount'] or price['conflictingDateCount']: return None
    expected = expected_sessions(row['market'], row['rangeStart'], row['rangeEnd'])
    expected_set = set(expected)
    for key in ('missingSessionDates', 'unexpectedSessionDates'):
        dates = price[key]
        if (not isinstance(dates, list) or len(dates) > 100 or any(not _date(day) or not row['rangeStart'] <= day <= row['rangeEnd'] for day in dates)
                or dates != sorted(set(dates))): return None
    missing, unexpected = price['missingSessionDates'], price['unexpectedSessionDates']
    if not set(missing) <= expected_set or set(unexpected) & expected_set: return None
    observed = sorted((expected_set - set(missing)) | set(unexpected))
    if (not observed or price['expectedSessionCount'] != len(expected) or price['expectedDateSetHash'] != date_set_hash(expected)
            or price['missingSessionCount'] != len(missing) or price['unexpectedSessionCount'] != len(unexpected)
            or price['observedCount'] != len(observed) or price['observedStart'] != observed[0] or price['observedEnd'] != observed[-1]
            or price['observedDateSetHash'] != date_set_hash(observed)): return None
    complete = valid_complete_price(price, row['market'], row['stockCode'], row['rangeStart'], row['rangeEnd'])
    if price['completeness'] == 'complete' and not complete: return None
    if price['completeness'] == 'partial' and not missing and not unexpected: return None
    return price


def _fact(value, row, now):
    if not isinstance(value, dict): return None
    price = _price(value.get('price'), row)
    urls = value.get('sourceUrls')
    observed = _instant(value.get('fetchedAt'))
    if (price is None or not isinstance(urls, list) or not 1 <= len(urls) <= 8 or not observed
            or not dt.datetime.fromisoformat(row['rangeEnd']).replace(tzinfo=dt.timezone.utc) <= observed <= now + dt.timedelta(minutes=5)):
        return None
    identities = sorted(set(source_identity(source) for source in [value.get('sourceUrl'), *urls]))
    return {'factId': canonical_digest({'sourceIdentity': identities, 'price': price}), 'price': price,
            'sourceUrl': value['sourceUrl'], 'sourceUrls': list(urls), 'fetchedAt': value['fetchedAt']}


def conflict_for(facts, identity, now=None):
    """Build exactly two bounded facts; extra raw price fields are not copied."""
    try:
        current = now if now is not None else dt.datetime.now(dt.timezone.utc)
        if not isinstance(current, dt.datetime) or current.tzinfo is None: return None
        row = _identity(identity)
        if row is None or not isinstance(facts, list) or len(facts) != 2: return None
        clean = [_fact(value, row, current) for value in facts]
        if any(value is None for value in clean): return None
        clean.sort(key=lambda value: value['factId'])
        if clean[0]['factId'] == clean[1]['factId']: return None
        complete = [valid_complete_price(value['price'], row['market'], row['stockCode'], row['rangeStart'], row['rangeEnd']) for value in clean]
        a, b = [value['price'] for value in clean]
        if all(complete) and (a['low'], a['high']) != (b['low'], b['high']): kind = 'complete-source-disagreement'
        elif sum(complete) == 1:
            whole, partial = (a, b) if complete[0] else (b, a)
            if not (partial['low'] < whole['low'] or partial['high'] > whole['high']): return None
            kind = 'partial-extreme'
        else: return None
        key = '|'.join([row['market'], row['stockCode'], row['currency'], 'unadjusted', row['rangeStart'], row['rangeEnd'], row['calendarId']])
        types = [kind]
        result = {'schemaVersion': 1, 'key': key, 'types': types, 'facts': clean,
                  'witnessDigest': canonical_digest({'key': key, 'types': types, 'factIds': [value['factId'] for value in clean]}),
                  'resolution': 'unresolved'}
        return result if len(_encoded(result)) <= 65536 else None
    except (ValueError, TypeError, KeyError, OverflowError): return None


def normalize_price_conflict(value, market, code, start, end, now=None):
    if not isinstance(value, dict) or market not in CALENDAR['markets']: return None
    identity = {'market': market, 'stockCode': code, 'currency': CALENDAR['markets'][market]['currency'],
                'rangeStart': start, 'rangeEnd': end, 'calendarId': CALENDAR['id']}
    result = conflict_for(value.get('facts'), identity, now)
    try:
        return result if result is not None and _encoded(result) == _encoded(value) else None
    except (ValueError, TypeError, OverflowError): return None


__all__ = ['PRICE_FIELDS', 'canonical_digest', 'source_identity', 'conflict_for', 'normalize_price_conflict']
