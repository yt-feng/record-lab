"""One reserved production PDF/model attempt; private input stays in memory."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import requests

from deepseek_strategy import http_transport
from publication_guard import inspect_content
from strategy_semantic_adapter import prepare_candidate
from strategy_semantic_pool import canonical_digest

CACHE = Path('.cache/semantic-production')
MAX_BYTES = 2_000_000


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


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
    adapter_result = result['adapter'] or {'usageHistory': task['usageHistory']}
    candidate = adapter_result.get('candidate')
    receipt = {'schemaVersion': 1, 'kind': 'validated-semantic-producer-receipt', 'taskDigest': task['taskDigest'],
               'reservationId': task['reservationId'], 'resultDigest': canonical_digest(result),
               'inputDigest': candidate['inputDigest'] if candidate else None,
               'candidateDigest': candidate['candidateDigest'] if candidate else None,
               'usageHistoryDigest': canonical_digest(adapter_result['usageHistory']), 'finishedAt': result['finishedAt']}
    return result, receipt


def main():
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise ValueError('SEMANTIC_ACTIONS_REQUIRED')
        task = json.loads((CACHE / 'task.json').read_bytes())
        # A remote reservation was persisted by the preceding workflow step.
        ledger = json.loads(Path('web/data/semantic-model-ledger.json').read_bytes())
        attempt = ledger['products'][task['ledgerKey']]['attempts'][-1]
        if attempt['reservationId'] != task['reservationId'] or attempt['status'] != 'reserved':
            raise ValueError('SEMANTIC_RESERVATION_MISMATCH')
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
