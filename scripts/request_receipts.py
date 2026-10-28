"""Bounded, public-safe price request reservations, independent of raw snapshots.

The unit is a reservation immediately before one Session.get invocation, NOT a
wire request. Requests may follow redirects; redirects are not observed here.
A reservation surviving interruption remains unknown and must not be replayed
as an unattempted request. This journal does not grant retries or alter budgets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

MAX_LOGICAL = 2000
# CN/HK: 3 fallbacks, each attempts_override=1. US: 2, each=1.
# A future provider change exceeding this bound fails BEFORE the next call.
MAX_SESSION_GET = 6000
MAX_RECORD_BYTES = 8192
MAX_ARTIFACT_BYTES = 16_000_000
SOURCES = {'eastmoney', 'yahoo-query1', 'yahoo-query2', 'tencent'}
ERRORS = {'TIMEOUT', 'TLS_FAILED', 'HTTP_ERROR', 'HTTP_REJECTED', 'NETWORK_ERROR',
          'RESPONSE_TOO_LARGE', 'EMPTY_RESPONSE', 'TRANSPORT_FAILED'}
HEX = r'[a-f0-9]{64}'


class ReceiptError(RuntimeError):
    """Deliberately not DataError: a provider fallback must never swallow it."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def instant(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None: raise ValueError()
        return result.astimezone(dt.timezone.utc)
    except ValueError:
        raise ReceiptError('REQUEST_RECEIPTS_INVALID') from None


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ReceiptError('REQUEST_RECEIPTS_INVALID')


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def config_binding(config, config_bytes, env):
    phase = {key: config.get(key) for key in ('targetPeriodEnd', 'baselinePeriodEnd')}
    if phase not in ({'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'},
                     {'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30'}):
        raise ReceiptError('REQUEST_RECEIPTS_INVALID_CONFIG')
    traversal = config.get('traversal') or {}
    batch = traversal.get('batchId')
    planned = traversal.get('plannedAt')
    instant(planned)
    if (not isinstance(batch, str) or not re.fullmatch(r'[0-9]{1,3}', batch)
            or env.get('RECEIPT_BATCH_ID') != batch
            or not re.fullmatch(r'[1-9][0-9]{0,19}', env.get('GITHUB_RUN_ID', ''))
            or not re.fullmatch(r'[1-9][0-9]{0,5}', env.get('GITHUB_RUN_ATTEMPT', ''))
            or not re.fullmatch(r'[a-f0-9]{40}', env.get('GITHUB_SHA', ''))):
        raise ReceiptError('REQUEST_RECEIPTS_INVALID_CONFIG')
    return {'runId': env['GITHUB_RUN_ID'], 'runAttempt': int(env['GITHUB_RUN_ATTEMPT']),
            'sourceCommit': env['GITHUB_SHA'], 'batchId': batch, 'phase': phase,
            'plannedAt': planned, 'configSha256': hashlib.sha256(config_bytes).hexdigest()}


def logical_identity(key, args, phase):
    if not isinstance(args, (tuple, list)) or len(args) != 4:
        raise ReceiptError('REQUEST_RECEIPTS_INVALID_IDENTITY')
    code, market, start, end = args
    pattern = {'CN': r'[0-9]{6}', 'HK': r'[0-9]{5}', 'US': r'[A-Z][A-Z0-9]{0,9}(?:[.][A-Z0-9]{1,3})?'}.get(market)
    if not pattern or not isinstance(code, str) or not re.fullmatch(pattern, code) or key != market + ':' + code:
        raise ReceiptError('REQUEST_RECEIPTS_INVALID_IDENTITY')
    expected_start = (dt.date.fromisoformat(phase['baselinePeriodEnd']) + dt.timedelta(days=1)).isoformat()
    if start != expected_start or end != phase['targetPeriodEnd']:
        raise ReceiptError('REQUEST_RECEIPTS_INVALID_IDENTITY')
    return {'stockKey': key, 'periodStart': start, 'periodEnd': end,
            'currency': {'CN': 'CNY', 'HK': 'HKD', 'US': 'USD'}[market], 'adjustment': 'unadjusted'}


def source_for(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.port:
        raise ReceiptError('REQUEST_RECEIPTS_SOURCE_UNSUPPORTED')
    if parsed.hostname == 'push2his.eastmoney.com' and parsed.path == '/api/qt/stock/kline/get': return 'eastmoney'
    if parsed.hostname == 'web.ifzq.gtimg.cn' and parsed.path == '/appstock/app/fqkline/get': return 'tencent'
    if parsed.hostname in {'query1.finance.yahoo.com', 'query2.finance.yahoo.com'} and re.fullmatch(r'/v8/finance/chart/[A-Za-z0-9.^=-]{1,24}', parsed.path):
        return 'yahoo-query1' if parsed.hostname.startswith('query1.') else 'yahoo-query2'
    raise ReceiptError('REQUEST_RECEIPTS_SOURCE_UNSUPPORTED')


def write_atomic(path, value):
    data = encoded(value)
    if len(data) > MAX_RECORD_BYTES: raise ReceiptError('REQUEST_RECEIPTS_LIMIT')
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


class RequestJournal:
    def __init__(self, root, config, config_bytes, env=None):
        self.root = Path(root)
        self.binding = config_binding(config, config_bytes, os.environ if env is None else env)
        self.lock = threading.RLock()
        self.rows, self.call_count, self.failed, self.closed = {}, 0, False, False
        self.header = {'schemaVersion': 1, 'kind': 'market-request-journal', 'binding': self.binding,
                       'requestUnit': 'session.get-reservation', 'redirectAccounting': 'not-observed',
                       'createdAt': stamp(), 'closedAt': None, 'collectorCompleted': False,
                       'logicalCount': None, 'sessionGetCount': None}
        # Refuse reuse: an earlier attempt can never be overwritten as zero.
        if self.root.exists(): raise ReceiptError('REQUEST_RECEIPTS_ALREADY_EXISTS')
        try:
            self.root.mkdir(parents=True)
            self._write(self.root / 'header.json', self.header)
        except Exception:
            self.failed = True
            raise ReceiptError('REQUEST_RECEIPTS_WRITE_FAILED') from None

    def _write(self, path, value):
        try:
            write_atomic(path, value)
        except Exception:
            self.failed = True
            raise ReceiptError('REQUEST_RECEIPTS_WRITE_FAILED') from None

    def assert_healthy(self):
        if self.failed: raise ReceiptError('REQUEST_RECEIPTS_WRITE_FAILED')

    def observer(self, key, args):
        # Identity is checked before reservation; invalid securities make no GET.
        identity = logical_identity(key, args, self.binding['phase'])
        return PriceObserver(self, identity)

    def begin(self, identity, url):
        with self.lock:
            self.assert_healthy()
            if self.closed: raise ReceiptError('REQUEST_RECEIPTS_CLOSED')
            try: source = source_for(url)
            except Exception:
                self.failed = True
                raise ReceiptError('REQUEST_RECEIPTS_SOURCE_UNSUPPORTED') from None
            logical_id = digest({'binding': self.binding, 'identity': identity})
            row = self.rows.get(logical_id)
            if (row is None and len(self.rows) >= MAX_LOGICAL) or self.call_count >= MAX_SESSION_GET or (row and len(row['calls']) >= 3):
                self.failed = True
                raise ReceiptError('REQUEST_RECEIPTS_LIMIT')
            now = stamp()
            if row is None:
                row = {'schemaVersion': 1, 'logicalId': logical_id, 'identity': identity, 'reservedAt': now, 'calls': []}
            ordinal = len(row['calls']) + 1
            call_id = digest({'logicalId': logical_id, 'ordinal': ordinal})
            call = {'callId': call_id, 'ordinal': ordinal, 'source': source, 'reservedAt': now,
                    'finishedAt': None, 'outcome': 'unknown', 'errorCode': None}
            # Copy-on-write: a failed replacement cannot masquerade as persisted.
            updated = {**row, 'calls': [*row['calls'], call]}
            self._write(self.root / (logical_id + '.json'), updated)
            self.rows[logical_id] = updated
            self.call_count += 1
            return logical_id, call_id

    def finish(self, token, outcome, error_code=None):
        with self.lock:
            self.assert_healthy()
            logical_id, call_id = token
            row = self.rows[logical_id]
            matching = [call for call in row['calls'] if call['callId'] == call_id]
            if len(matching) != 1 or matching[0]['outcome'] != 'unknown':
                self.failed = True
                raise ReceiptError('REQUEST_RECEIPTS_INVALID')
            if outcome not in {'returned', 'failed'} or (outcome == 'failed' and error_code not in ERRORS) or (outcome == 'returned' and error_code is not None):
                self.failed = True
                raise ReceiptError('REQUEST_RECEIPTS_INVALID')
            calls = [{**call, 'finishedAt': stamp(), 'outcome': outcome, 'errorCode': error_code}
                     if call['callId'] == call_id else call for call in row['calls']]
            updated = {**row, 'calls': calls}
            self._write(self.root / (logical_id + '.json'), updated)
            self.rows[logical_id] = updated

    def close(self, completed):
        with self.lock:
            self.assert_healthy()
            if self.closed: raise ReceiptError('REQUEST_RECEIPTS_CLOSED')
            header = {**self.header, 'closedAt': stamp(), 'collectorCompleted': completed is True,
                      'logicalCount': len(self.rows), 'sessionGetCount': self.call_count}
            self._write(self.root / 'header.json', header)
            self.header, self.closed = header, True


class PriceObserver:
    def __init__(self, journal, identity):
        self.journal, self.identity = journal, identity

    def begin(self, url): return self.journal.begin(self.identity, url)
    def finish(self, token, outcome, error_code=None): return self.journal.finish(token, outcome, error_code)


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    def unique(items):
        result = {}
        for key, value in items:
            if key in result: raise ReceiptError('REQUEST_RECEIPTS_INVALID')
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ReceiptError('REQUEST_RECEIPTS_INVALID')))


def validate(root, config, config_bytes, env=None, now=None):
    """No salvage of malformed public fields. A valid interrupted journal is safe
    to upload, but accountingCompleted stays false and unknown is never zero."""
    root = Path(root)
    binding = config_binding(config, config_bytes, os.environ if env is None else env)
    current = instant(now or stamp())
    header = read_json(root / 'header.json')
    exact(header, ('schemaVersion', 'kind', 'binding', 'requestUnit', 'redirectAccounting', 'createdAt',
                   'closedAt', 'collectorCompleted', 'logicalCount', 'sessionGetCount'))
    if (type(header['schemaVersion']) is not int or header['schemaVersion'] != 1 or header['kind'] != 'market-request-journal'
            or encoded(header['binding']) != encoded(binding) or header['requestUnit'] != 'session.get-reservation'
            or header['redirectAccounting'] != 'not-observed' or type(header['collectorCompleted']) is not bool):
        raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    planned, created = instant(binding['plannedAt']), instant(header['createdAt'])
    if not planned <= created <= current + dt.timedelta(minutes=5): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    closed = instant(header['closedAt']) if header['closedAt'] is not None else None
    if closed is not None and not created <= closed <= current + dt.timedelta(minutes=5): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    if closed is None and header['collectorCompleted']: raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    if closed is None:
        if header['logicalCount'] is not None or header['sessionGetCount'] is not None: raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    elif not integer(header['logicalCount'], 0, MAX_LOGICAL) or not integer(header['sessionGetCount'], 0, MAX_SESSION_GET):
        raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    files = list(itertools.islice(root.iterdir(), MAX_LOGICAL + 3))
    if root.is_symlink() or len(files) > MAX_LOGICAL + 2: raise ReceiptError('REQUEST_RECEIPTS_LIMIT')
    rows, counts = [], {'logicalReservations': 0, 'sessionGetReservations': 0, 'returned': 0, 'failed': 0, 'unknown': 0}
    interrupted_write = False
    for path in sorted(files):
        if path.name == 'header.json': continue
        if path.suffix == '.tmp' and re.fullmatch(r'(?:header|' + HEX + r')\.tmp', path.name):
            # Never inspect or publish temporary bytes. Their presence prevents
            # a full accounting assertion even if the old JSON is still valid.
            interrupted_write = True
            continue
        if not re.fullmatch(HEX + r'\.json', path.name): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
        row = read_json(path)
        exact(row, ('schemaVersion', 'logicalId', 'identity', 'reservedAt', 'calls'))
        identity = row['identity']
        exact(identity, ('stockKey', 'periodStart', 'periodEnd', 'currency', 'adjustment'))
        key = identity['stockKey']
        if not isinstance(key, str) or ':' not in key: raise ReceiptError('REQUEST_RECEIPTS_INVALID')
        market, code = key.split(':', 1)
        expected = logical_identity(key, (code, market, identity['periodStart'], identity['periodEnd']), binding['phase'])
        logical_id = digest({'binding': binding, 'identity': expected})
        if (type(row['schemaVersion']) is not int or row['schemaVersion'] != 1 or encoded(identity) != encoded(expected)
                or row['logicalId'] != logical_id or path.stem != logical_id or not isinstance(row['calls'], list)
                or not 1 <= len(row['calls']) <= 3): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
        previous = instant(row['reservedAt'])
        if not created <= previous <= (closed or current + dt.timedelta(minutes=5)): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
        for ordinal, call in enumerate(row['calls'], 1):
            exact(call, ('callId', 'ordinal', 'source', 'reservedAt', 'finishedAt', 'outcome', 'errorCode'))
            if (type(call['ordinal']) is not int or call['ordinal'] != ordinal or call['source'] not in SOURCES
                    or call['callId'] != digest({'logicalId': logical_id, 'ordinal': ordinal})
                    or call['outcome'] not in {'returned', 'failed', 'unknown'}): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
            when = instant(call['reservedAt'])
            if not previous <= when <= (closed or current + dt.timedelta(minutes=5)): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
            if ordinal == 1 and call['reservedAt'] != row['reservedAt']: raise ReceiptError('REQUEST_RECEIPTS_INVALID')
            if call['outcome'] == 'unknown':
                if call['finishedAt'] is not None or call['errorCode'] is not None: raise ReceiptError('REQUEST_RECEIPTS_INVALID')
                previous = when
            else:
                finish = instant(call['finishedAt'])
                if not when <= finish <= (closed or current + dt.timedelta(minutes=5)): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
                if (call['outcome'] == 'returned' and call['errorCode'] is not None or
                        call['outcome'] == 'failed' and call['errorCode'] not in ERRORS): raise ReceiptError('REQUEST_RECEIPTS_INVALID')
                previous = finish
            counts[call['outcome']] += 1
            counts['sessionGetReservations'] += 1
        rows.append(row)
    counts['logicalReservations'] = len(rows)
    if len(rows) > MAX_LOGICAL or counts['sessionGetReservations'] > MAX_SESSION_GET: raise ReceiptError('REQUEST_RECEIPTS_LIMIT')
    if closed is not None and (header['logicalCount'] != len(rows) or header['sessionGetCount'] != counts['sessionGetReservations']):
        raise ReceiptError('REQUEST_RECEIPTS_INVALID')
    complete = bool(closed is not None and header['collectorCompleted'] and not interrupted_write and not counts['unknown'])
    result = {'schemaVersion': 1, 'kind': 'market-request-accounting', 'header': header,
              'recordsVerified': True, 'accountingCompleted': complete, 'counts': counts, 'records': rows,
              'limitations': ['market-prices-only', 'reservations-may-not-reach-server', 'redirects-not-observed',
                              'no-retry-or-completeness-authorization']}
    if len(encoded(result)) > MAX_ARTIFACT_BYTES: raise ReceiptError('REQUEST_RECEIPTS_LIMIT')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    raw = Path(args.config).read_bytes()
    if len(raw) > 25_000_000: raise ReceiptError('REQUEST_RECEIPTS_INVALID_CONFIG')
    result = validate(args.journal, json.loads(raw), raw)
    data = encoded(result)
    from publication_guard import inspect_content
    if inspect_content(data): raise ReceiptError('REQUEST_RECEIPTS_PUBLICATION_REJECTED')
    output = Path(args.output)
    if output.exists(): raise ReceiptError('REQUEST_RECEIPTS_ALREADY_EXISTS')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            handle.write('completed=' + ('true' if result['accountingCompleted'] else 'false') + '\n')
    print(json.dumps({'recordsVerified': True, 'accountingCompleted': result['accountingCompleted'], 'counts': result['counts']}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"status":"failed","errorCode":"REQUEST_RECEIPTS_UNVERIFIED"}')
        raise SystemExit(1)
