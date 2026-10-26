"""Use a dated official product-index row only for that product's founding date."""
from __future__ import annotations

import datetime as dt
import re
from source_dates import china_date


def _observed(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})', value):
        raise ValueError("INVALID_OBSERVATION_TIME")
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def profile_source(source, code):
    return isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code) is not None and source == f'https://fundf10.eastmoney.com/jbgk_{code}.html'


def normalize_profile_inception(raw, code, *, now=None):
    if (not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw['schemaVersion'] != 1
            or raw.get('fundCode') != code or not profile_source(raw.get('sourceUrl'), code)):
        return None
    try:
        founded = dt.date.fromisoformat(raw['date'])
        observed = _observed(raw['retrievedAt'])
        current = now or dt.datetime.now(dt.timezone.utc)
        if (founded.isoformat() != raw['date'] or 'T' not in raw['retrievedAt'] or observed.tzinfo is None
                or current.tzinfo is None or founded > china_date(observed) or observed > current + dt.timedelta(minutes=5)):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {key: raw[key] for key in ('schemaVersion', 'fundCode', 'date', 'sourceUrl', 'retrievedAt')}


def normalize_inception_conflict(raw, code, *, now=None):
    if (not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw['schemaVersion'] != 1
            or raw.get('fundCode') != code or raw.get('status') != 'conflicting-sources'):
        return None
    historical = normalize_inception(raw.get('historicalEvidence'), code, raw.get('periodEnd'), now=now)
    profile = normalize_profile_inception(raw.get('profileEvidence'), code, now=now)
    if not historical or not profile or historical['date'] == profile['date']: return None
    return {'schemaVersion': 1, 'fundCode': code, 'periodEnd': historical['periodEnd'], 'status': 'conflicting-sources',
            'historicalEvidence': historical, 'profileEvidence': profile}


def normalize_inception(raw, code, period, *, now=None):
    from historical_universe import SOURCE_ID, SOURCE_URL, SOURCE_AS_OF, SOURCE_HASH, SHEET, EXPECTED_PRODUCTS
    if (not isinstance(raw, dict) or not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)
            or type(raw.get('schemaVersion')) is not int or raw.get('schemaVersion') != 1 or raw.get('fundCode') != code or raw.get('periodEnd') != period
            or period != SOURCE_AS_OF or raw.get('sourceId') != SOURCE_ID or raw.get('sourceUrl') != SOURCE_URL
            or raw.get('contentHash') != SOURCE_HASH or raw.get('sheet') != SHEET
            or type(raw.get('row')) is not int or not 3 <= raw['row'] <= EXPECTED_PRODUCTS + 2):
        return None
    try:
        founded = dt.date.fromisoformat(raw['date'])
        observed = _observed(raw['retrievedAt'])
        current = now or dt.datetime.now(dt.timezone.utc)
        if (founded.isoformat() != raw['date'] or founded > dt.date.fromisoformat(period)
                or 'T' not in raw['retrievedAt'] or observed.tzinfo is None or current.tzinfo is None
                or china_date(observed) < dt.date.fromisoformat(period) or observed > current + dt.timedelta(minutes=5)):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {key: raw[key] for key in ('schemaVersion', 'fundCode', 'periodEnd', 'date', 'sourceId',
                                      'sourceUrl', 'contentHash', 'sheet', 'row', 'retrievedAt')}


def catalog_inception(item, catalog, *, now=None):
    scope = catalog.get('quarterUniverse') or {}
    sources = scope.get('sources')
    refs = item.get('historicalPresenceEvidence')
    period = catalog.get('targetPeriodEnd')
    if (not isinstance(sources, list) or not isinstance(refs, list)
            or scope.get('targetPeriodEnd') != period or item.get('historicalCandidate') is True):
        return None
    for ref in refs:
        if not isinstance(ref, dict): continue
        source = next((row for row in sources if isinstance(row, dict) and row.get('id') == ref.get('sourceId')), None)
        if (not source or source.get('effectiveDate') != period or source.get('format') != 'xlsx'
                or source.get('recordUnit') != 'fund-product-code'
                or source.get('sourceUrl') != item.get('historicalInceptionSourceUrl')):
            continue
        evidence = normalize_inception({'schemaVersion': 1, 'fundCode': item.get('code'), 'periodEnd': period,
            'date': item.get('historicalInceptionDate'), 'sourceId': source.get('id'), 'sourceUrl': source.get('sourceUrl'),
            'contentHash': source.get('contentHash'), 'sheet': ref.get('sheet'), 'row': ref.get('row'),
            'retrievedAt': source.get('retrievedAt')}, item.get('code'), period, now=now)
        if evidence: return evidence
    return None
