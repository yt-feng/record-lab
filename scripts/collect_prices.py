"""Price-only request execution with actual-request receipts and checkpoints."""
import concurrent.futures
import datetime as dt
import threading
import time
import math

from providers import PublicProvider, DataError
from price_patches import stock_key, valid_target


def bounded(value, default, upper, lower=0):
    try:
        number = float(value)
        return min(upper, max(lower, number)) if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def plans_for(items, phase):
    plans = []
    for item in items:
        valid = valid_target(item, phase)
        plans.append({'code': item['code'], 'binding': item.get('priceOnly') if valid else None,
                      'targetStockKeys': sorted(stock_key(row) for row in item['priceTargets']) if valid else [],
                      'attemptedStockKeys': [], 'attemptedAt': None,
                      'errorCode': None if valid else 'PRICE_BINDING_INVALID'})
    return plans


def execute(requests, deadline, workers, on_start, on_result, provider_factory=PublicProvider):
    """Only the transport callback marks a request started; queued work is free."""
    def fetch(item):
        key, args = item
        started = False
        def begin():
            nonlocal started
            if not started:
                started = True
                on_start(key, dt.datetime.now(dt.timezone.utc).isoformat())
        if time.monotonic() >= deadline:
            return key, None, 'PRICE_BUDGET_REACHED', False
        provider = provider_factory()
        provider.on_request = begin
        try:
            result = provider.price_bars(*args)
            result['retrievedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
            return key, result, None, started
        except Exception as error:
            return key, None, error.code if isinstance(error, DataError) else 'PARSE_FAILED', started
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, request) for request in requests]
        for future in concurrent.futures.as_completed(futures):
            on_result(*future.result())


def collect(config, as_of, checkpoint=None, provider_factory=PublicProvider, deadline=None):
    """Standalone lightweight entry point used by tests and price-only batches."""
    phase = {key: config[key] for key in ('targetPeriodEnd', 'baselinePeriodEnd')}
    plans = plans_for(config['funds'], phase)
    bars, errors, lock = {}, [], threading.RLock()
    deadline = deadline if deadline is not None else time.monotonic() + min(
        bounded(config.get('maxPriceSeconds', 600), 600, 900), bounded(config.get('maxBatchSeconds', 1500), 1500, 1800))
    start = (dt.date.fromisoformat(phase['baselinePeriodEnd']) + dt.timedelta(days=1)).isoformat()
    requests = {}
    for plan in plans:
        for key in plan['targetStockKeys']:
            market, code = key.split(':', 1)
            requests[key] = (code, market, start, phase['targetPeriodEnd'])
    def snapshot():
        payload = {'asOf': as_of, **phase, 'retrievedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'funds': [], 'barsByStock': bars, 'pricePatches': plans, 'errors': errors,
                   'coverage': {'status': 'partial', 'priceSymbolsRequested': len(requests), 'priceSymbolsFetched': len(bars)}}
        if checkpoint: checkpoint(payload)
        return payload
    def begin(key, at):
        with lock:
            for plan in plans:
                if key in plan['targetStockKeys'] and key not in plan['attemptedStockKeys']:
                    plan['attemptedStockKeys'].append(key); plan['attemptedStockKeys'].sort()
                    plan['attemptedAt'] = plan['attemptedAt'] or at
            snapshot()
    def finish(key, value, error, started):
        with lock:
            if value: bars[key] = value
            if error: errors.append({'code': key.split(':', 1)[1], 'stage': 'prices', 'errorCode': error})
            snapshot()
    snapshot()
    execute(list(requests.items())[:int(bounded(config.get('maxPriceSymbols', 1200), 1200, 2000))], deadline,
            int(bounded(config.get('maxWorkers', 2), 2, 2, 1)), begin, finish, provider_factory)
    return snapshot()
