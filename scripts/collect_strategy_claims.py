"""One original-PDF request per independent strategy backfill task."""
from __future__ import annotations
import concurrent.futures
import copy
import datetime as dt
import os
import threading
import time

from collect_prices import bounded
from providers import PublicProvider, DataError
from strategy_claim_patches import (ERRORS, NO_REQUEST_ERRORS, StrategyClaimPatchError, normalize_task, normalize_receipt,
                                   report_for, current)
from strategy_claims import _instant


def validate_items(config):
    """A strategy request cannot spend a second lane's budget."""
    items = config.get('funds', [])
    codes = [item.get('code') for item in items]
    if len(set(codes)) != len(codes):
        raise DataError('STRATEGY_CLAIMS_BINDING_INVALID')
    traversal = config.get('traversal', {})
    other_codes = set()
    for field in ('qualityCodes', 'managerCodes', 'strategyCodes', 'enrichmentCodes', 'reportLocatorCodes'):
        other_codes.update(traversal.get(field, []))
    for item in items:
        if 'strategyClaimsOnly' in item and (any(key in item for key in ('priceOnly', 'reportLocator', 'reportLocatorOnly'))
                                            or item['code'] in other_codes):
            raise DataError('STRATEGY_CLAIMS_BINDING_INVALID')


def execute(items, config, deadline, workers, on_update, provider_factory=PublicProvider):
    """Checkpoint entry into the request call, not presumed HTTP delivery."""
    if provider_factory is PublicProvider and os.environ.get('GITHUB_ACTIONS') != 'true':
        raise DataError('STRATEGY_CLAIMS_BINDING_INVALID')
    validate_items(config)
    planned = config.get('traversal', {}).get('plannedAt')
    phase = config.get('targetPeriodEnd')

    def fetch(item):
        code = item.get('code')
        task = normalize_task(item.get('strategyClaimsOnly'), code, phase)
        if not task or not _instant(planned) or _instant(planned) > current():
            on_update(code, None, None, 'STRATEGY_CLAIMS_BINDING_INVALID'); return
        if time.monotonic() >= deadline:
            on_update(code, None, None, 'STRATEGY_CLAIMS_BUDGET_REACHED'); return
        receipt, provider, original_get, original_session_get, called = None, None, None, None, False
        try:
            provider = provider_factory()
            original_get = provider.get
            session = getattr(provider, 'session', None)
            if session is not None:
                original_session_get = session.get
                http_called = False
                def one_http_get(url, **kwargs):
                    nonlocal http_called
                    if http_called or url != task['source']['sourceUrl']:
                        raise DataError('UNEXPECTED_REQUEST')
                    http_called = True
                    kwargs['allow_redirects'] = False
                    response = original_session_get(url, **kwargs)
                    if 300 <= response.status_code < 400:
                        response.close()
                        raise DataError('HTTP_REJECTED')
                    return response
                # This task-local provider is never shared with other lanes.
                # Do not silently turn one source request into redirect GETs.
                session.get = one_http_get

            def only_get(url, **kwargs):
                nonlocal receipt, called
                if called or url != task['source']['sourceUrl'] or kwargs.get('binary') is not True or kwargs.get('params') is not None:
                    raise DataError('UNEXPECTED_REQUEST')
                if time.monotonic() >= deadline:
                    raise DataError('STRATEGY_CLAIMS_BUDGET_REACHED')
                called = True
                receipt = {'schemaVersion': 1, **{key: task[key] for key in ('code', 'targetPeriodEnd', 'taskDigest', 'analysisDigest')},
                           'sourceUrl': task['source']['sourceUrl'], 'attemptedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
                           'outcome': 'started', 'errorCode': None}
                # The caller's existing lock + atomic writer completes before
                # entering this one request. A crash retains a spent attempt.
                on_update(code, copy.deepcopy(receipt), None, None)
                kwargs['attempts_override'] = 1
                kwargs['timeout_seconds'] = min(18, max(.5, deadline - time.monotonic()))
                return original_get(url, **kwargs)

            provider.get = only_get
            document = {key: task['source'][key] for key in ('sourceUrl', 'publishedAt', 'kind', 'periodEnd')}
            document['fundCode'] = code
            details = provider.report_details(document, max_pages=int(bounded(config.get('maxStrategyClaimsPages', 300), 300, 300, 1)))
            if receipt is None:
                raise StrategyClaimPatchError('STRATEGY_CLAIMS_REQUEST_NOT_STARTED')
            observed = dt.datetime.now(dt.timezone.utc).isoformat()
            report = report_for(details, task, receipt['attemptedAt'], observed, now=observed)
            receipt = {**receipt, 'outcome': 'parsed'}
            if not normalize_receipt(receipt, task, planned, observed, now=observed):
                raise StrategyClaimPatchError('STRATEGY_CLAIMS_INCOMPLETE')
            patch = {'schemaVersion': 1, 'code': code, 'binding': task, 'receipt': copy.deepcopy(receipt), 'report': report}
            on_update(code, copy.deepcopy(receipt), patch, None)
        except Exception as error:
            error_code = getattr(error, 'code', None)
            error_code = error_code if error_code in ERRORS else 'STRATEGY_CLAIMS_FAILED'
            if receipt is not None:
                if error_code in NO_REQUEST_ERRORS: error_code = 'STRATEGY_CLAIMS_FAILED'
                receipt = {**receipt, 'outcome': 'failed', 'errorCode': error_code}
            on_update(code, copy.deepcopy(receipt), None, error_code)
        finally:
            if provider is not None:
                if original_get is not None: provider.get = original_get
                session = getattr(provider, 'session', None)
                if session is not None:
                    if original_session_get is not None: session.get = original_session_get
                    session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def collect(config, as_of, checkpoint=None, provider_factory=PublicProvider, deadline=None):
    items = config['funds']
    receipts, patches, errors, lock = {}, {}, {}, threading.RLock()
    deadline = deadline if deadline is not None else time.monotonic() + min(
        bounded(config.get('maxStrategyClaimsSeconds', 300), 300, 600), bounded(config.get('maxBatchSeconds', 1500), 1500, 1800))

    def snapshot():
        deferred = [{'code': code, 'reports': [], 'collectionAttempt': {'attempted': False,
                     'status': 'deferred-budget', 'errorCode': 'BATCH_DEADLINE_REACHED'}}
                    for code, error in errors.items() if error['errorCode'] == 'STRATEGY_CLAIMS_BUDGET_REACHED'
                    and code not in receipts]
        payload = {'asOf': as_of, 'targetPeriodEnd': config['targetPeriodEnd'], 'baselinePeriodEnd': config['baselinePeriodEnd'],
                   'retrievedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'funds': deferred, 'barsByStock': {}, 'pricePatches': [],
                   'strategyClaimsPatches': list(patches.values()), 'strategyClaimsReceipts': list(receipts.values()),
                   'errors': list(errors.values()), 'coverage': {'status': 'partial', 'strategyClaimsRequested': len(items),
                       'strategyClaimsAttempted': len(receipts), 'strategyClaimsParsed': len(patches)}}
        if checkpoint: checkpoint(payload)
        return payload

    def update(code, receipt, patch, error):
        with lock:
            if receipt is not None: receipts[code] = receipt
            if patch is not None: patches[code] = patch
            if error: errors[code] = {'code': code, 'stage': 'strategy-claims', 'errorCode': error}
            snapshot()

    snapshot()
    execute(items, config, deadline, int(bounded(config.get('maxWorkers', 2), 2, 2, 1)), update, provider_factory)
    return snapshot()
