"""One reserved production PDF/model attempt; private input stays in memory."""
from __future__ import annotations
import datetime as dt
import concurrent.futures
import json
import os
from pathlib import Path
import re
import requests
import time

from deepseek_strategy import http_transport
from publication_guard import inspect_content
from strategy_semantic_adapter import prepare_candidate
from strategy_semantic_pool import canonical_digest

CACHE = Path('.cache/semantic-production')
MAX_BYTES = 2_000_000


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def receipt_for(task, result):
    adapter_result = result.get('adapter') or {'usageHistory': task['usageHistory']}
    candidate = adapter_result.get('candidate')
    return {'schemaVersion': 1, 'kind': 'validated-semantic-producer-receipt', 'taskDigest': task['taskDigest'],
            'reservationId': task['reservationId'], 'resultDigest': canonical_digest(result),
            'inputDigest': candidate['inputDigest'] if candidate else None,
            'candidateDigest': candidate['candidateDigest'] if candidate else None,
            'usageHistoryDigest': canonical_digest(adapter_result['usageHistory']), 'finishedAt': result['finishedAt']}


def write_public(path, value):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
    if len(raw) > MAX_BYTES or inspect_content(raw, MAX_BYTES):
        raise ValueError('SEMANTIC_PUBLICATION_REJECTED')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(raw); temporary.replace(path)


def fetch_pdf(url):
    # The exact source was validated against the retained source/identity.
    # Never follow redirects into a different report or issue hidden retries.
    if not isinstance(url, str) or not re.match(r'^https://[A-Za-z0-9.-]+/', url):
        raise ValueError('SEMANTIC_SOURCE_URL_INVALID')
    with requests.Session() as session:
        session.headers.update({'User-Agent': 'Public report research/1.0'})
        with session.get(url, timeout=(8, 30), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('SEMANTIC_PDF_HTTP_REJECTED')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 16_000_000:
                    raise ValueError('SEMANTIC_PDF_TOO_LARGE')
                chunks.append(chunk)
            document = b''.join(chunks)
    if not document.startswith(b'%PDF'):
        raise ValueError('SEMANTIC_PDF_INVALID')
    return document


def execute(task, *, credential, checkpoint, pdf_fetch=fetch_pdf, transport=http_transport, adapter=prepare_candidate):
    value = dict(task); expected = value.pop('taskDigest', None)
    if canonical_digest(value) != expected or canonical_digest(task['report']) != task['reportDigest']:
        raise ValueError('SEMANTIC_TASK_CHANGED')
    result = {'schemaVersion': 1, 'kind': 'strategy-semantic-production-output', 'taskDigest': expected,
              'reservationId': task['reservationId'], 'status': 'reserved', 'errorCode': None,
              'pdfRequests': 0, 'pdfReceipt': None, 'adapter': None, 'finishedAt': None}

    def emit():
        checkpoint(result)

    try:
        if not isinstance(credential, str) or not credential.strip():
            raise ValueError('SEMANTIC_CREDENTIALS_UNAVAILABLE')
        result['pdfRequests'] = 1
        result['pdfReceipt'] = {'attemptedAt': stamp(), 'finishedAt': None, 'outcome': 'started'}
        emit()
        document = pdf_fetch(task['report']['strategyEvidencePool']['source']['sourceUrl'])
        observed = stamp()
        result['pdfReceipt'].update(finishedAt=observed, outcome='received')
        emit()

        def model_checkpoint(value):
            # Only the adapter's bounded fields are retained. It never returns
            # the private packet, provider body, reasoning, or full PDF text.
            result['adapter'] = value
            emit()

        outcome = adapter(document, task['report'], code=task['code'], source_observed_at=observed,
            credential=credential, usage_history=task['usageHistory'],
            expected_usage_history_digest=task['expectedUsageHistoryDigest'],
            expected_baseline_digest=task['baselinePoolDigest'], checkpoint=model_checkpoint,
            transport=transport, max_attempts=task['maxAttempts'])
        result.update(adapter=outcome, status=outcome['status'], errorCode=outcome['errorCode'])
    except Exception as error:
        # Preserve a started or unknown model receipt; no automatic re-request.
        model = result.get('adapter') or {}
        unresolved = model.get('usageHistory', [{}])[-1].get('outcome') in {'started', 'unknown'} if model.get('usageHistory') else False
        error_code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'SEMANTIC_[A-Z_]+', str(error)) else 'SEMANTIC_PRODUCER_FAILED'
        result.update(status='unknown' if unresolved else 'rejected', errorCode=error_code)
        if result['pdfReceipt'] and result['pdfReceipt']['outcome'] == 'started':
            result['pdfReceipt'].update(finishedAt=stamp(), outcome='failed')
    if result['adapter'] is None:
        result['adapter'] = {'status': result['status'], 'errorCode': result['errorCode'], 'modelRequests': 0,
                             'usageHistory': task['usageHistory'], 'candidate': None}
    result['finishedAt'] = stamp()
    emit()
    return result, receipt_for(task, result)


def read_public(path, maximum=MAX_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('SEMANTIC_TASK_CHANGED')
    return json.loads(path.read_bytes())


def reserved(task, root):
    match = re.fullmatch(r'(2026Q[23]):([0-9]{6})', task['ledgerKey'])
    if not match: raise ValueError('SEMANTIC_RESERVATION_MISMATCH')
    expected = f'web/data/periods/{match[1]}/semantic-model-ledgers/{match[2][:2]}.json'
    relative = task.get('ledgerPath', 'web/data/semantic-model-ledger.json')
    if relative not in (expected, 'web/data/semantic-model-ledger.json'):
        raise ValueError('SEMANTIC_RESERVATION_MISMATCH')
    ledger = read_public(Path(root) / relative, 10_000_000)
    entry = ledger['products'][task['ledgerKey']]; attempt = entry['attempts'][-1]
    if (attempt['reservationId'] != task['reservationId'] or attempt['status'] != 'reserved'
            or entry['usageHistory'] != task['usageHistory']
            or entry['usageHistoryDigest'] != task['expectedUsageHistoryDigest']):
        raise ValueError('SEMANTIC_RESERVATION_MISMATCH')


def execute_batch(batch, *, root='.', credential, executor=execute, clock=time.monotonic):
    """Two independent workers; every checkpoint has a bound usage receipt."""
    value = dict(batch); expected = value.pop('batchDigest', None)
    items = batch.get('tasks')
    if (canonical_digest(value) != expected or not isinstance(items, list) or not 1 <= len(items) <= 10
            or type(batch.get('maxWorkers')) is not int or not 1 <= batch['maxWorkers'] <= 2
            or type(batch.get('maxSeconds')) is not int or not 1 <= batch['maxSeconds'] <= 1500
            or len({item.get('code') for item in items}) != len(items)
            or len({item.get('ledgerKey') for item in items}) != len(items)):
        raise ValueError('SEMANTIC_TASK_CHANGED')
    tasks = []
    for item in items:
        if not re.fullmatch(r'[0-9]{6}', item.get('code', '')) or item.get('path') != f"tasks/{item['code']}":
            raise ValueError('SEMANTIC_TASK_CHANGED')
        directory = Path(root) / CACHE / item['path']; task = read_public(directory / 'task.json')
        unsigned = dict(task); unsigned.pop('taskDigest', None)
        if (task['taskDigest'] != item['taskDigest'] or canonical_digest(unsigned) != item['taskDigest']
                or any(task[field] != batch[field] for field in ('quarter', 'runId', 'runAttempt'))
                or task['code'] != item['code'] or task['ledgerKey'] != item['ledgerKey']):
            raise ValueError('SEMANTIC_TASK_CHANGED')
        reserved(task, root); tasks.append((directory, task))
    deadline = clock() + batch['maxSeconds']

    def process(item):
        directory, task = item
        def checkpoint(result):
            write_public(directory / 'result.json', result)
            write_public(directory / 'receipt.json', receipt_for(task, result))
        try:
            if clock() >= deadline:
                result = {'schemaVersion': 1, 'kind': 'strategy-semantic-production-output', 'taskDigest': task['taskDigest'],
                    'reservationId': task['reservationId'], 'status': 'deferred', 'errorCode': 'SEMANTIC_BATCH_DEADLINE',
                    'pdfRequests': 0, 'pdfReceipt': None, 'finishedAt': stamp(),
                    'adapter': {'status': 'deferred', 'errorCode': 'SEMANTIC_BATCH_DEADLINE', 'modelRequests': 0,
                                'usageHistory': task['usageHistory'], 'candidate': None}}
                checkpoint(result)
            else:
                result, receipt = executor(task, credential=credential, checkpoint=checkpoint)
                checkpoint(result)
                if receipt != receipt_for(task, result): raise ValueError('SEMANTIC_PRODUCER_RECEIPT_INVALID')
            return {'code': task['code'], 'status': result['status']}
        except Exception:
            # Keep the last written start/final receipt. A broken task does not
            # erase another product or cause an unrecorded retry.
            return {'code': task['code'], 'status': 'unknown', 'errorCode': 'SEMANTIC_PRODUCER_CHECKPOINT_FAILED'}

    with concurrent.futures.ThreadPoolExecutor(max_workers=batch['maxWorkers']) as pool:
        outcomes = list(pool.map(process, tasks))
    result = {'schemaVersion': 1, 'kind': 'strategy-semantic-batch-execution', 'batchDigest': expected,
              'products': outcomes, 'continueRun': False}
    write_public(Path(root) / CACHE / 'execution.json', result)
    return result


def main():
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise ValueError('SEMANTIC_ACTIONS_REQUIRED')
        if (CACHE / 'batch.json').exists():
            result = execute_batch(read_public(CACHE / 'batch.json', 100_000), credential=os.environ.get('DEEPSEEK_API_KEY', ''))
            print(json.dumps(result)); return 0
        task = json.loads((CACHE / 'task.json').read_bytes())
        # A remote reservation was persisted by the preceding workflow step.
        reserved(task, '.')
        result, receipt = execute(task, credential=os.environ.get('DEEPSEEK_API_KEY', ''),
            checkpoint=lambda value: write_public(CACHE / 'result.json', value))
        write_public(CACHE / 'receipt.json', receipt)
        print(json.dumps({'status': result['status'], 'pdfRequests': result['pdfRequests'],
                          'modelRequests': (result['adapter'] or {}).get('modelRequests', 0), 'errorCode': result['errorCode']}))
        return 0
    except Exception:
        print('{"status":"unknown","errorCode":"SEMANTIC_PRODUCER_CHECKPOINT_FAILED"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
