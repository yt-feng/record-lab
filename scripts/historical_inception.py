"""Use a dated official product-index row only for that product's founding date."""
from __future__ import annotations

import datetime as dt
import re


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
        observed = dt.datetime.fromisoformat(raw['retrievedAt'].replace('Z', '+00:00'))
        current = now or dt.datetime.now(dt.timezone.utc)
        if (founded.isoformat() != raw['date'] or founded > dt.date.fromisoformat(period)
                or 'T' not in raw['retrievedAt'] or observed.tzinfo is None or current.tzinfo is None
                or observed.date() < dt.date.fromisoformat(period) or observed > current + dt.timedelta(minutes=5)):
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
