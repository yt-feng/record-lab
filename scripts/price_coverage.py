"""Exact exchange-session coverage and conservative compatible-source merging."""
import datetime as dt
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

CALENDAR = json.loads((Path(__file__).resolve().parents[1] / 'config/trading-calendars-2026.json').read_text())
VERSION = 2


@lru_cache(maxsize=64)
def expected_sessions(market, start, end):
    if market not in CALENDAR['markets'] or not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        return None
    if first.isoformat() != start or last.isoformat() != end or not CALENDAR['supportedStart'] <= start <= end <= CALENDAR['supportedEnd']:
        return None
    closed = set(CALENDAR['markets'][market]['closedDates'])
    return [(first + dt.timedelta(days=index)).isoformat() for index in range((last - first).days + 1)
            if (first + dt.timedelta(days=index)).weekday() < 5 and (first + dt.timedelta(days=index)).isoformat() not in closed]


def date_set_hash(values):
    return hashlib.sha256('|'.join(sorted(set(values))).encode()).hexdigest()


def series_coverage(series, start, end):
    market = series.get('market')
    expected = expected_sessions(market, start, end)
    observed = sorted({row['date'] for row in series.get('rows', []) if isinstance(row, dict)
                       and isinstance(row.get('date'), str) and start <= row['date'] <= end})
    validation = series.get('sourceValidation') or {}
    rejected = validation.get('rejectedRowCount', 0)
    conflicts = validation.get('conflictingDateCount', 0)
    missing = sorted(set(expected or []) - set(observed))
    unexpected = sorted(set(observed) - set(expected or [])) if expected is not None else []
    identity = (market in CALENDAR['markets'] and series.get('currency') == CALENDAR['markets'][market]['currency']
                and series.get('priceBasis') == 'unadjusted' and series.get('requestedStart') == start and series.get('requestedEnd') == end)
    complete = bool(expected and identity and not rejected and not conflicts and not missing and not unexpected)
    return {'coverageVersion': VERSION, 'calendarId': CALENDAR['id'], 'sessionScope': CALENDAR['sessionScope'],
            'expectedSessionCount': len(expected) if expected is not None else None,
            'expectedDateSetHash': date_set_hash(expected) if expected is not None else None,
            'observedDateSetHash': date_set_hash(observed), 'missingSessionCount': len(missing) if expected is not None else None,
            'missingSessionDates': missing, 'unexpectedSessionCount': len(unexpected), 'unexpectedSessionDates': unexpected,
            'rejectedRowCount': rejected, 'conflictingDateCount': conflicts,
            'completeness': 'complete' if complete else 'partial' if expected is not None else 'calendar-unavailable'}


def valid_complete_price(value, market, code, start, end):
    expected = expected_sessions(market, start, end)
    return bool(isinstance(value, dict) and expected and value.get('coverageVersion') == VERSION
                and value.get('calendarId') == CALENDAR['id'] and value.get('sessionScope') == CALENDAR['sessionScope']
                and value.get('market') == market and value.get('stockCode') == code
                and value.get('rangeStart') == start and value.get('rangeEnd') == end
                and value.get('currency') == CALENDAR['markets'][market]['currency']
                and value.get('priceBasis') == 'unadjusted' and value.get('status') == 'observed'
                and value.get('completeness') == 'complete'
                and type(value.get('low')) in (int, float) and type(value.get('high')) in (int, float)
                and 0 < value['low'] <= value['high'] < float('inf')
                and type(value.get('expectedSessionCount')) is int and value['expectedSessionCount'] == len(expected)
                and type(value.get('observedCount')) is int and value['observedCount'] == len(expected)
                and value.get('observedStart') == expected[0] and value.get('observedEnd') == expected[-1]
                and value.get('expectedDateSetHash') == value.get('observedDateSetHash') == date_set_hash(expected)
                and all(type(value.get(key)) is int and value[key] == 0 for key in
                        ('missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount'))
                and value.get('missingSessionDates') == [] and value.get('unexpectedSessionDates') == [])


def price_series(code, market, currency, provider, source_url, rows, start, end):
    result = {'stockCode': code, 'market': market, 'currency': currency, 'priceBasis': 'unadjusted',
              'provider': provider, 'sourceUrl': source_url, 'sourceUrls': [source_url], 'rows': list(rows),
              'requestedStart': start, 'requestedEnd': end,
              'sourceValidation': {'rejectedRowCount': getattr(rows, 'rejected_rows', 0), 'conflictingDateCount': 0}}
    result['coverage'] = series_coverage(result, start, end)
    return result


def merge_price_series(candidates, start, end):
    """Merge only verified identical instruments/bases without conflicting days."""
    if not candidates:
        return None
    first = candidates[0]
    same = [row for row in candidates if all(row.get(key) == first.get(key) for key in
                                            ('stockCode', 'market', 'currency', 'priceBasis', 'requestedStart', 'requestedEnd'))]
    clean = [row for row in same if not row.get('sourceValidation', {}).get('rejectedRowCount')]
    # An independently complete source does not depend on partial-source rows.
    # Discard failed/partial candidates instead of importing their conflicts.
    complete = next((row for row in clean if series_coverage(row, start, end)['completeness'] == 'complete'), None)
    if complete is not None:
        return {**complete, 'coverage': series_coverage(complete, start, end)}
    pool = clean or same
    best = max(pool, key=lambda row: len(row.get('rows', [])))
    result = {**best, 'sourceValidation': dict(best.get('sourceValidation') or {})}
    merged, conflicts = {}, set()
    for series in clean:
        for row in series['rows']:
            prior = merged.get(row['date'])
            if prior and any(not math.isclose(prior[field], row[field], rel_tol=1e-6, abs_tol=1e-6) for field in ('low', 'high', 'close')):
                conflicts.add(row['date'])
            else:
                merged.setdefault(row['date'], row)
    if clean and not conflicts:
        result['rows'] = [merged[day] for day in sorted(merged)]
        result['sourceUrls'] = sorted({row['sourceUrl'] for row in clean})
        result['sourceValidation'] = {'rejectedRowCount': 0, 'conflictingDateCount': 0}
    elif conflicts:
        result['sourceValidation']['conflictingDateCount'] = len(conflicts)
    result['coverage'] = series_coverage(result, start, end)
    return result
