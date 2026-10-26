"""Actions-only, bounded, read-only quote conflict diagnostics."""
from __future__ import annotations
import argparse
import datetime as dt
import errno
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from urllib3 import exceptions as urllib3_errors
from providers import (DataError, stock_identifiers, parse_eastmoney_prices,
                       parse_yahoo_prices, parse_tencent_prices)
from foreign_prices import us_identifiers, parse_us_yahoo_prices
from price_coverage import price_series
from publication_guard import inspect_content

MAX_OUTPUT = 65536
MAX_RESPONSE = 2_000_000
NETWORK_CLASS_BY_CODE = {
    'REQUERY_HTTP_REJECTED': frozenset({'HTTP_4XX', 'HTTP_429', 'HTTP_5XX', 'OTHER'}),
    'REQUERY_TIMEOUT': frozenset({'TIMEOUT'}),
    'REQUERY_NETWORK_ERROR': frozenset({'TIMEOUT', 'TLS', 'CONNECTION_RESET', 'DISCONNECTED', 'DNS', 'OTHER'}),
}


def valid_network_error_class(code, category):
    return isinstance(code, str) and isinstance(category, str) and category in NETWORK_CLASS_BY_CODE.get(code, ())


class DiagnosticError(Exception):
    def __init__(self, code, *, network_error_class=None):
        super().__init__(code)
        if network_error_class is not None and not valid_network_error_class(code, network_error_class):
            raise ValueError('INVALID_NETWORK_ERROR_CLASS')
        self.network_error_class = network_error_class


def network_error_class(error):
    """Bounded typed exception traversal. Never inspect exception messages."""
    pending, seen = [error], set()
    dns_types = (socket.gaierror,) + tuple(value for value in [getattr(urllib3_errors, 'NameResolutionError', None)] if isinstance(value, type))
    for _ in range(32):
        if not pending: break
        current = pending.pop(0)
        if id(current) in seen: continue
        seen.add(id(current))
        if isinstance(current, (tuple, list)):
            pending.extend(current[:8]); pending = pending[:32]; continue
        if not isinstance(current, BaseException): continue
        if isinstance(current, (requests.exceptions.Timeout, TimeoutError, urllib3_errors.TimeoutError)): return 'TIMEOUT'
        if isinstance(current, (requests.exceptions.SSLError, ssl.SSLError, urllib3_errors.SSLError)): return 'TLS'
        if isinstance(current, (http.client.RemoteDisconnected, http.client.IncompleteRead)): return 'DISCONNECTED'
        if isinstance(current, dns_types): return 'DNS'
        if isinstance(current, ConnectionResetError) or isinstance(current, OSError) and current.errno == errno.ECONNRESET:
            return 'CONNECTION_RESET'
        if isinstance(current, (BrokenPipeError, ConnectionAbortedError)) or isinstance(current, OSError) and current.errno in (errno.EPIPE, errno.ECONNABORTED):
            return 'DISCONNECTED'
        pending.extend(value for value in (current.__cause__, current.__context__, vars(current).get('reason'), current.args)
                       if isinstance(value, (BaseException, tuple, list)))
        pending = pending[:32]
    return 'OTHER'


def request_for(row, group, now):
    """Requery only a recognized original instrument/window/provider, no fallback."""
    source = group.get('sourceUrl')
    if not isinstance(source, str) or len(source) > 2048:
        raise DiagnosticError('REQUERY_SOURCE_UNSUPPORTED')
    url = urlparse(source)
    if url.scheme != 'https' or url.username or url.password or url.port or url.fragment:
        raise DiagnosticError('REQUERY_SOURCE_UNSUPPORTED')
    pairs = parse_qsl(url.query, keep_blank_values=True)
    if len(pairs) != len(dict(pairs)):
        raise DiagnosticError('REQUERY_SOURCE_UNSUPPORTED')
    query = dict(pairs)
    code, market, start, end = (row[key] for key in ('stockCode', 'market', 'rangeStart', 'rangeEnd'))
    if market == 'US':
        info = us_identifiers(code); symbol, currency, secid, tencent, venue = info['yahooSymbol'], 'USD', None, None, None
    else:
        _, secid, symbol, tencent, currency, venue = stock_identifiers(code, market)
    if row.get('currency') != currency:
        raise DiagnosticError('REQUERY_CURRENCY_MISMATCH')
    if url.netloc == 'push2his.eastmoney.com' and url.path == '/api/qt/stock/kline/get':
        if (set(query) - {'secid', 'klt', 'fqt', 'beg', 'end', 'fields1', 'fields2'} or market == 'US'
                or query.get('secid') != secid or query.get('klt') != '101' or query.get('fqt') != '0'
                or query.get('beg') != start.replace('-', '') or query.get('end') != end.replace('-', '')
                or query.get('fields1') != 'f1,f2,f3,f4,f5,f6'
                or query.get('fields2') != 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'):
            raise DiagnosticError('REQUERY_REQUEST_SCOPE_MISMATCH')
        return source, 'eastmoney', lambda body: parse_eastmoney_prices(body, code, start, end), False
    if url.netloc == 'web.ifzq.gtimg.cn' and url.path == '/appstock/app/fqkline/get':
        if market == 'US' or query != {'param': f'{tencent},day,{start},{end},1000,'}:
            raise DiagnosticError('REQUERY_REQUEST_SCOPE_MISMATCH')
        return source, 'tencent', lambda body: parse_tencent_prices(body, tencent, code, start, end), False
    if url.netloc in {'query1.finance.yahoo.com', 'query2.finance.yahoo.com'} and url.path == '/v8/finance/chart/' + symbol:
        stamp = int(dt.datetime.combine(dt.date.fromisoformat(start), dt.time.min, tzinfo=dt.timezone.utc).timestamp())
        target_end = int(dt.datetime.combine(dt.date.fromisoformat(end) + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc).timestamp())
        if (set(query) != {'period1', 'period2', 'interval', 'events', 'includeAdjustedClose'}
                or query.get('period1') != str(stamp) or not re.fullmatch(r'\d{1,12}', query.get('period2', ''))
                or not target_end <= int(query['period2']) <= int(now.timestamp()) + 172800 or query.get('interval') != '1d'
                or query.get('events') != 'splits' or query.get('includeAdjustedClose') != 'false'):
            raise DiagnosticError('REQUERY_REQUEST_SCOPE_MISMATCH')
        # Retain the original quarter; extend only split-event inspection to
        # retrieval day so a later split cannot silently change historical bars.
        events_end = max(now.date(), dt.date.fromisoformat(end)) + dt.timedelta(days=1)
        query['period2'] = str(int(dt.datetime.combine(events_end, dt.time.min, tzinfo=dt.timezone.utc).timestamp()))
        adjusted = urlunparse(url._replace(query=urlencode(query)))
        parse = (lambda body: parse_us_yahoo_prices(body, code, start, end)) if market == 'US' else (
            lambda body: parse_yahoo_prices(body, symbol, currency, venue, start, end))
        return adjusted, 'yahoo', parse, adjusted != source
    raise DiagnosticError('REQUERY_SOURCE_UNSUPPORTED')


class ActionsTransport:
    def __call__(self, url):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise DiagnosticError('DIAGNOSTIC_REQUIRES_ACTIONS')
        started = time.monotonic()
        try:
            with requests.get(url, timeout=(4, 6), allow_redirects=False, stream=True,
                              headers={'User-Agent': 'Mozilla/5.0'}) as response:
                if response.status_code != 200:
                    status = response.status_code
                    category = 'HTTP_429' if status == 429 else 'HTTP_4XX' if 400 <= status < 500 else 'HTTP_5XX' if 500 <= status < 600 else 'OTHER'
                    raise DiagnosticError('REQUERY_HTTP_REJECTED', network_error_class=category)
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE or time.monotonic() - started > 12:
                        raise DiagnosticError('REQUERY_RESPONSE_LIMIT')
                    chunks.append(chunk)
                raw = b''.join(chunks)
        except requests.exceptions.Timeout:
            raise DiagnosticError('REQUERY_TIMEOUT', network_error_class='TIMEOUT') from None
        except requests.exceptions.RequestException as error:
            raise DiagnosticError('REQUERY_NETWORK_ERROR', network_error_class=network_error_class(error)) from None
        return raw


def evidence_metadata(payload, provider):
    """Only tiny public corporate-action facts, never response bodies."""
    if provider != 'yahoo': return {}
    try:
        result = payload['chart']['result'][0]
        meta, splits = result.get('meta', {}), result.get('events', {}).get('splits', {})
        return {'symbol': meta.get('symbol') if isinstance(meta.get('symbol'), str) and re.fullmatch(r'[A-Z0-9.\-^]{1,24}', meta['symbol']) else None,
                'currency': meta.get('currency') if meta.get('currency') in {'CNY', 'HKD', 'USD'} else None,
                'splitEventCount': len(splits) if isinstance(splits, dict) else None,
                'splitDates': sorted(dt.datetime.fromtimestamp(value['date'], dt.timezone.utc).date().isoformat()
                                     for value in list(splits.values())[:3] if isinstance(value, dict)
                                     and type(value.get('date')) in (int, float) and math.isfinite(value['date'])) if isinstance(splits, dict) else []}
    except (KeyError, TypeError, IndexError, ValueError, OverflowError, OSError):
        return {'metadataStatus': 'unavailable'}


def requery(result, *, fetch=None, now=None):
    if result.get('kind') != 'price-conflict-diagnostic' or len(result.get('quarters', [])) > 2:
        raise DiagnosticError('DIAGNOSTIC_INPUT_INVALID')
    fetch = fetch or ActionsTransport()
    current = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(current, dt.datetime) or current.tzinfo is None:
        raise DiagnosticError('DIAGNOSTIC_TIME_INVALID')
    request_count = 0
    for quarter in result['quarters']:
        if len(quarter.get('selected', [])) > 3: raise DiagnosticError('DIAGNOSTIC_INPUT_INVALID')
        for row in quarter['selected']:
            # Select two genuinely different stored complete ranges first;
            # repeated fund copies and identical original URLs are not requests.
            pair = next(iter(row.get('differences', [])), None)
            indexes = [pair['firstGroup'], pair['secondGroup']] if pair else []
            choices, seen = [], set()
            for index in indexes:
                group = row['groups'][index]
                if group.get('reusableCompleteSource') is True and group['sourceUrl'] not in seen:
                    choices.append((index, group)); seen.add(group['sourceUrl'])
            outputs, daily = [], []
            for index, group in choices[:2]:
                output = {'groupIndex': index, 'requested': False,
                          'observedAt': current.isoformat(), 'status': 'unavailable'}
                outputs.append(output)
                try:
                    source, provider, parse, extended = request_for(row, group, current)
                    output.update({'requested': True, 'splitInspectionExtended': extended})
                    if source != group['sourceUrl']: output['sourceUrl'] = source
                    request_count += 1
                    raw = fetch(source)
                    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE: raise DiagnosticError('REQUERY_RESPONSE_LIMIT')
                    output.update({'responseSha256': hashlib.sha256(raw).hexdigest(), 'responseBytes': len(raw),
                                   'fetchedAt': (now or dt.datetime.now(dt.timezone.utc)).isoformat()})
                    payload = json.loads(raw)
                    output.update(evidence_metadata(payload, provider))
                    values = parse(payload)
                    series = price_series(row['stockCode'], row['market'], row['currency'], provider, source, values, row['rangeStart'], row['rangeEnd'])
                    coverage = series['coverage']
                    output.update({'status': 'observed', 'low': min(value['low'] for value in values), 'high': max(value['high'] for value in values),
                                   'currency': series['currency'], 'priceBasis': series['priceBasis'], 'observedCount': len(values),
                                   **{key: coverage[key] for key in ('completeness', 'expectedSessionCount', 'observedDateSetHash',
                                       'expectedDateSetHash', 'missingSessionCount', 'unexpectedSessionCount', 'rejectedRowCount', 'conflictingDateCount')}})
                    output['storedRangeExactlyReproduced'] = output['low'] == group['low'] and output['high'] == group['high']
                    daily.append((output, {value['date']: value for value in values}))
                except (DiagnosticError, DataError) as error:
                    output.update(status='unavailable', errorCode=str(error))
                    category = getattr(error, 'network_error_class', None)
                    if valid_network_error_class(output['errorCode'], category): output['networkErrorClass'] = category
                except Exception:
                    output.update(status='unavailable', errorCode='REQUERY_PARSE_FAILED')
            row['requotes'] = outputs
            row['requeryComparison'] = {'status': 'insufficient-verified-responses', 'resolution': 'unresolved'}
            if len(daily) == 2:
                (first, a), (second, b) = daily
                shared = sorted(set(a) & set(b))
                differences = [day for day in shared if any(a[day][key] != b[day][key] for key in ('low', 'high', 'close'))]
                complete = first['completeness'] == second['completeness'] == 'complete'
                row['requeryComparison'] = {'status': 'current-responses-agree' if complete and not differences else 'repeatable-source-difference' if complete else 'partial-responses',
                                           'bothCompleteCalendars': complete, 'sharedSessionCount': len(shared), 'strictlyDifferentSessionCount': len(differences),
                                           'firstDifferentDates': differences[:3], 'rangeExactlyEqual': first['low'] == second['low'] and first['high'] == second['high'],
                                           'dailyExamples': [{'date': day, 'first': {key: a[day][key] for key in ('low', 'high', 'close')},
                                                              'second': {key: b[day][key] for key in ('low', 'high', 'close')},
                                                              'absoluteDelta': {key: abs(a[day][key] - b[day][key]) for key in ('low', 'high', 'close')}} for day in differences[:2]],
                                           'resolution': 'unresolved'}
            row['resolution'] = 'unresolved'
    result['requery'] = {'enabled': True, 'requestCount': request_count, 'maxRequestCount': 12, 'productionDataModified': False}
    return result


def write_result(result, file):
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(raw) > MAX_OUTPUT or inspect_content(raw, MAX_OUTPUT): raise DiagnosticError('DIAGNOSTIC_OUTPUT_REJECTED')
    file = Path(file); file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--output', default='.cache/price-conflict-audit/result.json')
    args = parser.parse_args(argv)
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true': raise DiagnosticError('DIAGNOSTIC_REQUIRES_ACTIONS')
        if Path(args.output).resolve().is_relative_to(Path(args.data).resolve()):
            raise DiagnosticError('DIAGNOSTIC_OUTPUT_INSIDE_SOURCE')
        scan = Path(args.output).with_name('scan.json'); scan.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['node', str(Path(__file__).with_name('price-conflict-scan.js')), '--data', args.data, '--output', str(scan)], check=True)
        if scan.is_symlink() or scan.stat().st_size > MAX_OUTPUT: raise DiagnosticError('DIAGNOSTIC_INPUT_INVALID')
        result = json.loads(scan.read_bytes())
        result['requery'] = {'enabled': False, 'requestCount': 0, 'maxRequestCount': 12, 'productionDataModified': False}
        if os.environ.get('PRICE_DIAGNOSTIC_REFETCH') == 'true': requery(result)
        write_result(result, args.output)
        print(json.dumps({'status': 'diagnosed', 'requestCount': result['requery']['requestCount'], 'productionDataModified': False}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, DiagnosticError) and re.fullmatch(r'[A-Z_]{1,80}', str(error)) else 'DIAGNOSTIC_FAILED'
        print(json.dumps({'status': 'failed', 'errorCode': code, 'productionDataModified': False}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
