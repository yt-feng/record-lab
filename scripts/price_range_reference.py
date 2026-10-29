"""Stored quarter-extrema compatibility, never a daily-series recheck."""
import copy
import datetime as dt
import json
import re
import struct
from functools import reduce
from urllib.parse import urlparse, parse_qsl

import price_consistency as raw
from price_coverage import valid_complete_price

METHOD = 'stored-range-endpoints-exact-or-yahoo-binary32-eastmoney-v1'


def preferred_fact(a, b):
    left, right = raw._instant(a['fetchedAt']), raw._instant(b['fetchedAt'])
    if left != right: return a if left > right else b
    stable = lambda f: json.dumps([f['sourceUrl'], f['sourceUrls'], f['fetchedAt']], separators=(',', ':'), ensure_ascii=False)
    return a if stable(a) <= stable(b) else b


def key_for(identity):
    return '|'.join(str(identity.get(k)) for k in ('market', 'stockCode', 'currency')) + '|unadjusted|' + '|'.join(
        str(identity.get(k)) for k in ('rangeStart', 'rangeEnd', 'calendarId'))


def provider_for(fact, identity):
    try:
        code, market = identity['stockCode'], identity['market']
        if market == 'HK' and re.fullmatch(r'\d{5}', code): secid, symbol = '116.' + code, f'{int(code):04d}.HK'
        elif market == 'CN' and re.fullmatch(r'\d{6}', code):
            if code.startswith('6'): secid, symbol = '1.' + code, code + '.SS'
            elif code.startswith(('0', '3')): secid, symbol = '0.' + code, code + '.SZ'
            elif code.startswith(('4', '8', '92')): secid, symbol = '0.' + code, code + '.BJ'
            else: return None
        else: return None
        providers = []
        for value in [fact['sourceUrl'], *fact['sourceUrls']]:
            url = urlparse(value); pairs = parse_qsl(url.query, keep_blank_values=True); q = dict(pairs)
            if url.scheme != 'https' or url.port or url.username or url.password or url.fragment or len(pairs) != len(q): return None
            if (url.hostname == 'push2his.eastmoney.com' and url.path == '/api/qt/stock/kline/get'
                    and set(q) == {'secid', 'klt', 'fqt', 'beg', 'end', 'fields1', 'fields2'}
                    and q['secid'] == secid and q['klt'] == '101' and q['fqt'] == '0'
                    and q['beg'] == identity['rangeStart'].replace('-', '') and q['end'] == identity['rangeEnd'].replace('-', '')
                    and q['fields1'] == 'f1,f2,f3,f4,f5,f6' and q['fields2'] == 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'):
                providers.append('eastmoney')
            elif (url.hostname in {'query1.finance.yahoo.com', 'query2.finance.yahoo.com'} and url.path == '/v8/finance/chart/' + symbol
                    and set(q) == {'period1', 'period2', 'interval', 'events', 'includeAdjustedClose'}
                    and q['period1'] == str(int(dt.datetime.fromisoformat(identity['rangeStart']).replace(tzinfo=dt.timezone.utc).timestamp()))
                    and re.fullmatch(r'\d{1,12}', q['period2'])
                    and dt.datetime.fromisoformat(identity['rangeEnd']).replace(tzinfo=dt.timezone.utc).timestamp() + 86400 <= int(q['period2'])
                    <= raw._instant(fact['fetchedAt']).timestamp() + 172800
                    and q['interval'] == '1d' and q['events'] == 'splits' and q['includeAdjustedClose'] == 'false'):
                providers.append('yahoo')
            else: return None
        return providers[0] if providers and len(set(providers)) == 1 else None
    except (KeyError, ValueError, TypeError, AttributeError): return None


def build_reference(facts, identity, now=None):
    try:
        current = now or dt.datetime.now(dt.timezone.utc)
        if not isinstance(facts, list) or len(facts) > 4096: return None
        row = raw._identity(identity)
        if row is None: return None
        by_id = {}
        for value in facts:
            fact = raw._fact(value, row, current)
            if fact is None or not valid_complete_price(fact['price'], row['market'], row['stockCode'], row['rangeStart'], row['rangeEnd']): return None
            previous = by_id.get(fact['factId'])
            if previous is None or preferred_fact(previous, fact) is fact:
                by_id[fact['factId']] = fact
        normalized = sorted(by_id.values(), key=lambda f: f['factId'])
        if not 2 <= len(normalized) <= 128: return None
        eastmoney = [f for f in normalized if provider_for(f, row) == 'eastmoney']
        if not eastmoney: return None
        selected = reduce(preferred_fact, eastmoney)
        exact = lambda f: (f['price']['low'], f['price']['high']) == (selected['price']['low'], selected['price']['high'])
        if not all(exact(f) for f in eastmoney) or all(exact(f) for f in normalized): return None
        f32 = lambda value: struct.unpack('!f', struct.pack('!f', value))[0]
        if not all(exact(f) or provider_for(f, row) == 'yahoo' and f['price']['low'] == f32(selected['price']['low'])
                   and f['price']['high'] == f32(selected['price']['high']) for f in normalized): return None
        return {'schemaVersion': 1, 'kind': 'stored-range-reference', 'method': METHOD, 'scope': 'quarter-extrema-only',
                'key': key_for(row), 'facts': normalized, 'referenceFactId': selected['factId'],
                'factSetDigest': raw.canonical_digest([f['factId'] for f in normalized]),
                'marketDataRequests': 0, 'dailySeriesReverified': False, 'originalResponsesReverified': False}
    except (KeyError, TypeError, ValueError, OverflowError, struct.error): return None


def normalize_reference(value, identity, now=None):
    try:
        rebuilt = build_reference(value.get('facts'), identity, now)
        return rebuilt if rebuilt is not None and raw._encoded(rebuilt) == raw._encoded(value) else None
    except (AttributeError, TypeError, ValueError): return None


def effective_reference(price, identity=None, now=None):
    if not isinstance(price, dict): return None
    identity = price if identity is None else identity
    identity = raw._identity(identity)
    if identity is None: return None
    current = now or dt.datetime.now(dt.timezone.utc)
    ref = normalize_reference(price.get('rangeReference'), identity, current)
    if ref is None: return None
    selected = next(f for f in ref['facts'] if f['factId'] == ref['referenceFactId'])
    identities = {f['factId'] for f in ref['facts']}
    if 'globalPriceConflict' in price:
        witness = raw.normalize_price_conflict(price['globalPriceConflict'], identity['market'], identity['stockCode'],
                                               identity['rangeStart'], identity['rangeEnd'], current)
        if witness is None or any(f['factId'] not in identities for f in witness['facts']): return None
    if price.get('status') == 'observed':
        fact = raw._fact({'price': price, 'sourceUrl': price.get('sourceUrl'),
            'sourceUrls': price.get('sourceUrls', [price.get('sourceUrl')]), 'fetchedAt': price.get('fetchedAt')}, identity, current)
        if fact is None: return None
        if price.get('completeness') == 'complete' and fact['factId'] not in identities: return None
        if price.get('completeness') != 'complete' and (price['low'] < selected['price']['low'] or price['high'] > selected['price']['high']): return None
    return {**copy.deepcopy(selected['price']), 'sourceUrl': selected['sourceUrl'], 'sourceUrls': selected['sourceUrls'][:],
            'fetchedAt': selected['fetchedAt'], 'rangeReference': ref, 'referenceOnly': True}


def effective_price_range(price, identity=None, now=None):
    return effective_reference(price, identity, now) or (price if isinstance(price, dict) and 'globalPriceConflict' not in price else None)


def unresolved_conflict(price, identity=None, now=None):
    identity = raw._identity(price if identity is None else identity)
    return bool(isinstance(price, dict) and identity and raw.normalize_price_conflict(price.get('globalPriceConflict'),
        identity['market'], identity['stockCode'], identity['rangeStart'], identity['rangeEnd'], now)
        and effective_reference(price, identity, now) is None)


def resolution_for(price, identity=None, now=None):
    identity = price if identity is None else identity
    effective = effective_reference(price, identity, now)
    if effective is None or not isinstance(price.get('globalPriceConflict'), dict): return None
    return {'market': identity['market'], 'stockCode': identity['stockCode'],
            'reference': copy.deepcopy(effective['rangeReference']), 'rawWitness': copy.deepcopy(price['globalPriceConflict'])}


def valid_resolution(value):
    try:
        if set(value) != {'market', 'stockCode', 'reference', 'rawWitness'}: return False
        reference = value['reference']; first = reference['facts'][0]['price']
        if first['market'] != value['market'] or first['stockCode'] != value['stockCode']: return False
        # Rebuild the mathematical facts and validate the exact old raw witness.
        price = {**first, 'status': 'missing', 'rangeReference': reference, 'globalPriceConflict': value['rawWitness']}
        return effective_reference(price, first) is not None
    except (KeyError, TypeError, ValueError, IndexError): return False
