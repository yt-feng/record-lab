"""Bounded diagnostic OHLC comparison. No transport or price adjudication."""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
import math
import re
import struct
from zoneinfo import ZoneInfo
from price_coverage import CALENDAR, expected_sessions, date_set_hash
from providers import stock_identifiers

FIELDS = ('open', 'high', 'low', 'close')
MAX_DAYS, MAX_BYTES = 90, 65536
ERRORS = {None, 'DAILY_SOURCE_UNSUPPORTED', 'DAILY_IDENTITY_UNVERIFIED', 'DAILY_RESPONSE_INVALID',
          'DAILY_ROW_LIMIT', 'DAILY_RESPONSE_UNAVAILABLE'}


class DailyEvidenceError(ValueError):
    pass


def fail():
    raise DailyEvidenceError('CONFLICT_DAILY_EVIDENCE_INVALID')


def encoded(value):
    try: return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError): fail()


def f32(value):
    try: return struct.unpack('!f', struct.pack('!f', value))[0]
    except (OverflowError, struct.error): return None


def number(value):
    if type(value) not in (int, float, str): return None
    if isinstance(value, str) and not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value): return None
    try: result = float(value)
    except (ValueError, OverflowError): return None
    return result if math.isfinite(result) and result > 0 else None


def valid_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\d', value): return False
    try: return dt.date.fromisoformat(value).isoformat() == value
    except ValueError: return False


def metadata_for(row, provider):
    if row.get('market') != 'CN' or row.get('currency') != 'CNY' or row.get('priceBasis') != 'unadjusted': return None
    try: _, _, symbol, _, _, venue = stock_identifiers(row['stockCode'], 'CN')
    except Exception: return None
    if venue not in ('SH', 'SZ'): return None
    if provider == 'eastmoney': return {'code': row['stockCode'], 'market': 0 if venue == 'SZ' else 1, 'venue': venue}
    if provider == 'yahoo':
        return {'symbol': symbol, 'currency': 'CNY', 'exchangeName': {'SH': 'SHH', 'SZ': 'SHZ'}[venue],
                'exchangeTimezoneName': 'Asia/Shanghai', 'instrumentType': 'EQUITY'}
    return None


def empty_series(provider, error):
    return {'provider': provider, 'metadata': None, 'rows': [], 'rejectedRowCount': 0,
            'duplicateDateCount': 0, 'ignoredPostQuarterCount': 0, 'errorCode': error}


def extract_daily(payload, provider, row):
    """Called only after the existing raw-price parser/endpoint guard succeeds.

    Preserve numeric OHLC without rounding. Post-quarter Yahoo bars are ignored
    only here; the existing parser still checks all requested split events.
    """
    result = empty_series(provider, None)
    expected_meta = metadata_for(row, provider)
    if expected_meta is None: return empty_series(provider, 'DAILY_SOURCE_UNSUPPORTED')
    try:
        if provider == 'eastmoney':
            data = payload['data']
            if (data.get('code') != row['stockCode'] or type(data.get('market')) is not int
                    or data['market'] != expected_meta['market']): return empty_series(provider, 'DAILY_IDENTITY_UNVERIFIED')
            raw = data['klines']
            if not isinstance(raw, list) or len(raw) > 512: return empty_series(provider, 'DAILY_ROW_LIMIT')
            values = []
            for line in raw:
                cells = line.split(',') if isinstance(line, str) else []
                values.append({'date': cells[0], 'open': cells[1], 'close': cells[2], 'high': cells[3], 'low': cells[4]} if len(cells) >= 5 else {})
        else:
            data = payload['chart']['result'][0]
            if any(data.get('meta', {}).get(k) != v for k, v in expected_meta.items()) or data.get('events', {}).get('splits'):
                return empty_series(provider, 'DAILY_IDENTITY_UNVERIFIED')
            raw, quotes = data['timestamp'], data['indicators']['quote']
            if not isinstance(raw, list) or len(raw) > 512: return empty_series(provider, 'DAILY_ROW_LIMIT')
            if not isinstance(quotes, list) or len(quotes) != 1: return empty_series(provider, 'DAILY_RESPONSE_INVALID')
            quote = quotes[0]
            if any(not isinstance(quote.get(k), list) or len(quote[k]) != len(raw) for k in FIELDS):
                return empty_series(provider, 'DAILY_RESPONSE_INVALID')
            values = []
            for index, stamp in enumerate(raw):
                day = None
                if type(stamp) in (int, float) and math.isfinite(stamp):
                    try: day = dt.datetime.fromtimestamp(stamp, ZoneInfo('Asia/Shanghai')).date().isoformat()
                    except (OverflowError, ValueError, OSError): pass
                values.append({'date': day, **{k: quote[k][index] for k in FIELDS}})
        result['metadata'] = expected_meta
        seen = set()
        for value in values:
            day = value.get('date')
            if not valid_day(day): result['rejectedRowCount'] += 1; continue
            if day > row['rangeEnd'] and provider == 'yahoo': result['ignoredPostQuarterCount'] += 1; continue
            if not row['rangeStart'] <= day <= row['rangeEnd']: result['rejectedRowCount'] += 1; continue
            if day in seen: result['duplicateDateCount'] += 1; continue
            seen.add(day)
            nums = {k: number(value.get(k)) for k in FIELDS}
            if (any(v is None for v in nums.values()) or not nums['low'] <= nums['open'] <= nums['high']
                    or not nums['low'] <= nums['close'] <= nums['high']): result['rejectedRowCount'] += 1; continue
            result['rows'].append({'date': day, **nums})
        if len(result['rows']) > MAX_DAYS: return empty_series(provider, 'DAILY_ROW_LIMIT')
        result['rows'].sort(key=lambda v: v['date'])
        return result
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return empty_series(provider, 'DAILY_RESPONSE_INVALID')


def normalize_series(value, provider, row):
    if (not isinstance(value, dict) or set(value) != set(empty_series(provider, None)) or value.get('provider') != provider
            or value.get('errorCode') is not None and not isinstance(value.get('errorCode'), str)
            or value.get('errorCode') not in ERRORS or not isinstance(value.get('rows'), list) or len(value['rows']) > MAX_DAYS): fail()
    for key in ('rejectedRowCount', 'duplicateDateCount', 'ignoredPostQuarterCount'):
        if type(value.get(key)) is not int or not 0 <= value[key] <= 512: fail()
    if value['errorCode'] is not None:
        if value != empty_series(provider, value['errorCode']): fail()
        return copy.deepcopy(value)
    if value['metadata'] is None or encoded(value['metadata']) != encoded(metadata_for(row, provider)): fail()
    days = []
    for item in value['rows']:
        if (not isinstance(item, dict) or set(item) != {'date', *FIELDS} or not valid_day(item['date'])
                or not row['rangeStart'] <= item['date'] <= row['rangeEnd']
                or any(type(item[k]) not in (int, float) or number(item[k]) is None for k in FIELDS)
                or not item['low'] <= item['open'] <= item['high'] or not item['low'] <= item['close'] <= item['high']): fail()
        days.append(item['date'])
    if days != sorted(set(days)): fail()
    if provider != 'yahoo' and value['ignoredPostQuarterCount']: fail()
    return copy.deepcopy(value)


def source_evidence(request, series, row):
    series = normalize_series(series, request['provider'], row)
    quote = request.get('quote', {})
    days = [v['date'] for v in series['rows']]
    expected = expected_sessions(row['market'], row['rangeStart'], row['rangeEnd']) or []
    complete = (request['outcome'] == 'observed' and quote.get('completeness') == 'complete' and expected and days == expected
                and series['errorCode'] is None and not series['rejectedRowCount'] and not series['duplicateDateCount'])
    if series['rows'] and (request['outcome'] != 'observed' or min(v['low'] for v in series['rows']) < quote['low']
                          or max(v['high'] for v in series['rows']) > quote['high']): fail()
    if complete and (min(v['low'] for v in series['rows']) != quote['low'] or max(v['high'] for v in series['rows']) != quote['high']): fail()
    return {**{k: request[k] for k in ('factId', 'provider', 'sourceUrl', 'originalFetchedAt', 'startedAt', 'outcome')},
            **{k: quote.get(k) for k in ('fetchedAt', 'responseSha256', 'responseBytes')}, 'series': series,
            'coverage': {'completeness': 'complete' if complete else 'partial', 'calendarId': CALENDAR['id'],
                         'expectedSessionCount': len(expected), 'observedSessionCount': len(days),
                         'expectedDateSetHash': date_set_hash(expected), 'observedDateSetHash': date_set_hash(days),
                         'missingSessionCount': len(set(expected) - set(days)), 'unexpectedSessionCount': len(set(days) - set(expected))},
            'storedRangeExactlyReproduced': quote.get('storedRangeExactlyReproduced') is True}


def build_evidence(item, receipt, captures):
    """Construct bounded, recomputable diagnostics outside immutable receipts."""
    sources = [source_evidence(request, captures.get(request['factId'], empty_series(request['provider'], 'DAILY_RESPONSE_UNAVAILABLE')), item)
               for request in receipt['requests']]
    paired = len(sources) == 2 and {s['provider'] for s in sources} == {'eastmoney', 'yahoo'}
    complete = paired and all(s['coverage']['completeness'] == 'complete' for s in sources)
    counts = {'comparedCellCount': 0, 'exactCellCount': 0, 'float32OnlyCellCount': 0, 'nonFloat32CellCount': 0}
    examples = []
    status = 'partial-evidence' if paired else 'unsupported-source-pair'
    if complete:
        a = next(s for s in sources if s['provider'] == 'eastmoney')['series']['rows']
        b = next(s for s in sources if s['provider'] == 'yahoo')['series']['rows']
        for first, second in zip(a, b):
            for key in FIELDS:
                x, y = first[key], second[key]; counts['comparedCellCount'] += 1
                category = 'exactCellCount' if x == y else 'float32OnlyCellCount' if f32(x) == y else 'nonFloat32CellCount'
                counts[category] += 1
                if category == 'nonFloat32CellCount' and len(examples) < 4:
                    examples.append({'date': first['date'], 'field': key, 'eastmoney': x, 'yahoo': y})
        status = ('daily-non-float32-difference' if counts['nonFloat32CellCount'] else
                  'daily-float32-compatible' if counts['float32OnlyCellCount'] else 'daily-identical')
        if not all(s['storedRangeExactlyReproduced'] for s in sources): status = 'stored-range-changed'
    result = {'schemaVersion': 1, 'kind': 'price-conflict-daily-evidence', 'quarter': receipt['quarter'],
              **{k: item[k] for k in ('key', 'digest', 'attemptId', 'market', 'stockCode', 'currency', 'priceBasis', 'rangeStart', 'rangeEnd', 'calendarId')},
              'sources': sources, 'comparison': {'status': status, 'bothCompleteCalendars': bool(complete), **counts,
                                                'firstNonFloat32Cells': examples, 'rule': 'exact-or-yahoo-equals-binary32-eastmoney'},
              'resolution': 'unresolved', 'automaticResolution': False, 'productionDataModified': False}
    result['evidenceSha256'] = hashlib.sha256(encoded(result)).hexdigest()
    if len(encoded(result)) > MAX_BYTES: fail()
    return result


def normalize_evidence(value, item, receipt):
    if not isinstance(value, dict) or not isinstance(value.get('sources'), list) or len(value['sources']) != len(receipt['requests']): fail()
    captures = {}
    for source, request in zip(value['sources'], receipt['requests']):
        if not isinstance(source, dict) or source.get('factId') != request['factId']: fail()
        captures[request['factId']] = source.get('series')
    result = build_evidence(item, receipt, captures)
    if encoded(result) != encoded(value) or len(encoded(value)) > MAX_BYTES: fail()
    return result
