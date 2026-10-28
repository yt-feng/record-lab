"""Admit independently verified market receipts without replaying requests.

Missing receipts do not reject independently verified report data. They remain
explicit unresolved batch entries and prevent automatic continuation. Complete
receipts from a later run never settle an older unknown reservation.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import request_receipts as receipts
from publication_guard import inspect_content
from traversal import atomic_json

MAX_ENTRIES = 4096
INDEX = 'market-accounting.json'
STATUSES = {'complete', 'incomplete', 'missing', 'invalid'}
ERRORS = {None, 'MARKET_RECEIPT_MISSING', 'MARKET_RECEIPT_INVALID', 'MARKET_ACCOUNTING_INCOMPLETE'}
COUNTERS = {'logicalReservations', 'sessionGetReservations', 'returned', 'failed', 'unknown'}
FIELDS = {'runId', 'runAttempt', 'batchId', 'sourceCommit', 'configSha256', 'plannedAt',
          'observedAt', 'status', 'counts', 'receiptPath', 'receiptSha256', 'errorCode'}


def read_bounded(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('MARKET_ACCOUNTING_INVALID')
    raw = path.read_bytes()
    def unique(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('MARKET_ACCOUNTING_INVALID')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('MARKET_ACCOUNTING_INVALID')))
    return value, raw


def verify_receipt(path, config, config_bytes, env):
    """Reuse the journal validator; never trust producer booleans or counts."""
    value, raw = read_bounded(path, receipts.MAX_ARTIFACT_BYTES)
    if (type(value) is not dict or type(value.get('records')) is not list
            or len(value['records']) > receipts.MAX_LOGICAL
            or type(value.get('accountingCompleted')) is not bool or inspect_content(raw)):
        raise ValueError('MARKET_RECEIPT_INVALID')
    with tempfile.TemporaryDirectory(prefix='accounting-check-') as directory:
        root = Path(directory)
        atomic_json(root / 'header.json', value.get('header'))
        seen = set()
        for row in value['records']:
            identifier = row.get('logicalId') if isinstance(row, dict) else None
            if (not isinstance(identifier, str) or not re.fullmatch(receipts.HEX, identifier)
                    or identifier in seen):
                raise ValueError('MARKET_RECEIPT_INVALID')
            seen.add(identifier)
            atomic_json(root / (identifier + '.json'), row)
        verified = receipts.validate(root, config, config_bytes, env=env)
    # An interrupted temporary write may conservatively mark an otherwise
    # intact journal incomplete. Never upgrade that producer observation.
    if not value['accountingCompleted']:
        verified['accountingCompleted'] = False
    if value != verified:
        raise ValueError('MARKET_RECEIPT_INVALID')
    return value, raw


def key(row):
    return row['runId'], row['runAttempt'], row['batchId']


def load_index(quarter_root, quarter):
    path = Path(quarter_root) / INDEX
    if not path.exists() and not path.is_symlink():
        return {'schemaVersion': 1, 'quarter': quarter, 'entries': []}
    value, _ = read_bounded(path, 8_000_000)
    if (type(value) is not dict or set(value) != {'schemaVersion', 'quarter', 'entries'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['quarter'] != quarter or type(value['entries']) is not list
            or len(value['entries']) > MAX_ENTRIES):
        raise ValueError('MARKET_ACCOUNTING_INDEX_INVALID')
    seen = set()
    for row in value['entries']:
        if (type(row) is not dict or set(row) != FIELDS
                or not isinstance(row['runId'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', row['runId'])
                or type(row['runAttempt']) is not int or not 1 <= row['runAttempt'] <= 999999
                or not isinstance(row['batchId'], str) or not re.fullmatch(r'[0-9]{1,3}', row['batchId'])
                or not isinstance(row['sourceCommit'], str) or not re.fullmatch(r'[a-f0-9]{40}', row['sourceCommit'])
                or not isinstance(row['configSha256'], str) or not re.fullmatch(receipts.HEX, row['configSha256'])
                or row['status'] not in STATUSES or row['errorCode'] not in ERRORS or key(row) in seen):
            raise ValueError('MARKET_ACCOUNTING_INDEX_INVALID')
        receipts.instant(row['plannedAt']); receipts.instant(row['observedAt'])
        seen.add(key(row))
        if row['status'] in {'missing', 'invalid'}:
            if (any(row[field] is not None for field in ('counts', 'receiptPath', 'receiptSha256'))
                    or row['errorCode'] != ('MARKET_RECEIPT_MISSING' if row['status'] == 'missing' else 'MARKET_RECEIPT_INVALID')):
                raise ValueError('MARKET_ACCOUNTING_INDEX_INVALID')
        else:
            expected = f"market-accounting/{row['runId']}-{row['runAttempt']}-{row['batchId']}.json"
            counts = row['counts']
            if (row['receiptPath'] != expected or not isinstance(row['receiptSha256'], str)
                    or not re.fullmatch(receipts.HEX, row['receiptSha256']) or type(counts) is not dict
                    or set(counts) != COUNTERS or any(type(count) is not int or not 0 <= count <= 6000 for count in counts.values())
                    or counts['sessionGetReservations'] != counts['returned'] + counts['failed'] + counts['unknown']
                    or not counts['logicalReservations'] <= counts['sessionGetReservations'] <= 3 * counts['logicalReservations']
                    or row['errorCode'] != (None if row['status'] == 'complete' else 'MARKET_ACCOUNTING_INCOMPLETE')
                    or row['status'] == 'complete' and counts['unknown'] != 0):
                raise ValueError('MARKET_ACCOUNTING_INDEX_INVALID')
            # A complete index is not proof on its own: retain and recheck the
            # exact validated receipt bytes, including interrupted observations.
            path = Path(quarter_root) / expected
            if (path.parent.is_symlink() or path.is_symlink() or not path.is_file()
                    or not 0 < path.stat().st_size <= receipts.MAX_ARTIFACT_BYTES):
                raise ValueError('MARKET_ACCOUNTING_PROOF_MISSING')
            hasher = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(65536), b''):
                    hasher.update(block)
            if hasher.hexdigest() != row['receiptSha256']:
                raise ValueError('MARKET_ACCOUNTING_PROOF_CHANGED')
    return value


def collect_batch_accounting(matrix, plan_root='.cache/plan', accounting_root='.cache/request-accounting', env=None):
    env = os.environ if env is None else env
    by_quarter = {}
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    for item in matrix:
        config, raw = read_bounded(Path(plan_root) / item['config'], 25_000_000)
        binding = receipts.config_binding(config, raw, {**env, 'RECEIPT_BATCH_ID': item['id']})
        if item['quarter'] != config.get('quarter'):
            raise ValueError('MARKET_ACCOUNTING_PHASE_MISMATCH')
        record = {field: binding[field] for field in ('runId', 'runAttempt', 'batchId', 'sourceCommit', 'configSha256', 'plannedAt')}
        record.update(observedAt=observed, status='missing', counts=None, receiptPath=None,
                      receiptSha256=None, errorCode='MARKET_RECEIPT_MISSING')
        path = Path(accounting_root) / f"request-accounting-{binding['runId']}-{binding['runAttempt']}-{item['id']}" / 'receipts.json'
        receipt = None
        if path.exists() or path.is_symlink():
            try:
                receipt, receipt_bytes = verify_receipt(path, config, raw, {**env, 'RECEIPT_BATCH_ID': item['id']})
                complete = receipt['accountingCompleted']
                record.update(status='complete' if complete else 'incomplete', counts=receipt['counts'],
                    receiptPath=f"market-accounting/{binding['runId']}-{binding['runAttempt']}-{item['id']}.json",
                    receiptSha256=hashlib.sha256(receipt_bytes).hexdigest(), errorCode=None if complete else 'MARKET_ACCOUNTING_INCOMPLETE')
                receipt = receipt_bytes
            except Exception:
                record.update(status='invalid', errorCode='MARKET_RECEIPT_INVALID')
                receipt = None
        by_quarter.setdefault(item['quarter'], []).append((record, receipt))
    return by_quarter


def persist_accounting(quarter_root, quarter, records):
    """Append immutable attempts. Repeated publishing cannot erase a hold."""
    root = Path(quarter_root)
    index = load_index(root, quarter)
    known = {key(row): row for row in index['entries']}
    for record, receipt in records:
        previous = known.get(key(record))
        if previous:
            if {k: v for k, v in previous.items() if k != 'observedAt'} != {
                    k: v for k, v in record.items() if k != 'observedAt'}:
                raise ValueError('MARKET_ACCOUNTING_REPLAY_CONFLICT')
            continue
        if len(index['entries']) >= MAX_ENTRIES:
            raise ValueError('MARKET_ACCOUNTING_HISTORY_FULL')
        if receipt is not None:
            # Validated receipt content is bounded public metadata. Original
            # reservation/finish timestamps and unknown outcomes are retained.
            path = root / record['receiptPath']
            if path.exists() or path.is_symlink():
                raise ValueError('MARKET_ACCOUNTING_RECEIPT_EXISTS')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as handle:
                handle.write(receipt); handle.flush(); os.fsync(handle.fileno())
        index['entries'].append(record); known[key(record)] = record
    atomic_json(root / INDEX, index)
    unresolved = [{k: row[k] for k in ('runId', 'runAttempt', 'batchId', 'status', 'errorCode')}
                  for row in index['entries'] if row['status'] != 'complete']
    return {'schemaVersion': 1, 'status': 'unverified' if unresolved else 'verified',
            'requestUnit': 'session.get-reservation', 'continuationAllowed': not unresolved,
            'recordedBatchCount': len(index['entries']), 'unresolvedBatchCount': len(unresolved),
            'unresolvedBatches': unresolved, 'indexPath': INDEX}
