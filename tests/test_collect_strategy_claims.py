"""Real synthetic extract→source patch→JS build, with no live requests."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parents[1] / 'scripts'), str(Path(__file__).parent)]
import collect as collector
import collect_strategy_claims as claims_collector
from providers import DataError, PublicProvider
from source_dates import china_today
from test_strategy_claim_patches import task, PHASE
from test_strategy_evidence_pool import extracted, REPORT


def config(codes=('000001',)):
    return {**PHASE, 'funds': [{'code': code, 'strategyClaimsOnly': task(code)} for code in codes],
            'traversal': {'plannedAt': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),
                          'strategyClaimsCodes': list(codes)}, 'maxWorkers': 2}


def provider_factory(*, failure=False, second_request=False, before_get=None, unknown=False, no_request=False):
    calls, instances = [], []
    # Extract once in the main test thread: pypdf's mocked page callback is not
    # global-patched concurrently. Each fake PDF observation gets its own time.
    template = extracted()
    class Provider:
        def __init__(self): instances.append(self)
        def get(self, url, **kwargs):
            if before_get: before_get(url)
            calls.append((url, kwargs))
            if unknown: raise KeyboardInterrupt()
            if failure: raise DataError('TIMEOUT')
            return b'%PDF synthetic source'
        def report_details(self, document, max_pages=300):
            if not no_request: self.get(document['sourceUrl'], binary=True)
            if second_request: self.get(document['sourceUrl'], binary=True)
            value = copy.deepcopy(template)
            at = dt.datetime.now(dt.timezone.utc).isoformat()
            value['strategyEvidenceRetrievedAt'] = at
            for field in ('strategyEvidencePool', 'strategyClaims'):
                value[field]['source']['retrievedAt'] = at
            value['strategyClaimsIdentityEvidence']['retrievedAt'] = at
            value['portfolioIdentityEvidence']['retrievedAt'] = at
            return value
        def overview(self, *_): raise AssertionError('unexpected overview')
        def announcements(self, *_): raise AssertionError('unexpected announcements')
        def holdings(self, *_): raise AssertionError('unexpected holdings')
        def price_bars(self, *_): raise AssertionError('unexpected prices')
    return Provider, calls, instances


class StrategyClaimCollectionTests(unittest.TestCase):
    def test_single_request_started_checkpoint_precedes_transport_and_parsed_is_only_metadata(self):
        snapshots = []
        def before_get(_):
            self.assertEqual(snapshots[-1]['strategyClaimsReceipts'][0]['outcome'], 'started')
        factory, calls, _ = provider_factory(before_get=before_get)
        value = claims_collector.collect(config(), china_today(), lambda x: snapshots.append(copy.deepcopy(x)), factory)
        self.assertEqual(len(calls), 1); self.assertEqual(calls[0][1]['attempts_override'], 1)
        self.assertLessEqual(calls[0][1]['timeout_seconds'], 18)
        self.assertEqual(value['funds'], []); self.assertEqual(value['barsByStock'], {})
        self.assertEqual(value['strategyClaimsReceipts'][0]['outcome'], 'parsed')
        self.assertEqual(len(value['strategyClaimsPatches']), 1)
        self.assertNotEqual(value['strategyClaimsPatches'][0]['report']['strategyEvidenceRetrievedAt'], REPORT['retrievedAt'])

    def test_failed_or_interrupted_request_retains_atomic_started_receipt_without_fresh_fund(self):
        for interrupted in (False, True):
            factory, calls, _ = provider_factory(failure=not interrupted, unknown=interrupted)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'checkpoint.json'
                write = lambda value: collector.write_checkpoint(path, value)
                if interrupted:
                    with self.assertRaises(KeyboardInterrupt): claims_collector.collect(config(), china_today(), write, factory)
                else: claims_collector.collect(config(), china_today(), write, factory)
                value = json.loads(path.read_text())
            self.assertEqual(len(calls), 1); self.assertEqual(value['funds'], [])
            self.assertEqual(value['strategyClaimsPatches'], [])
            self.assertEqual(value['strategyClaimsReceipts'][0]['outcome'], 'started' if interrupted else 'failed')

    def test_deferred_invalid_or_no_transport_never_creates_receipt(self):
        for reason in ('deferred', 'invalid', 'no-request'):
            fixture = config(); factory, calls, _ = provider_factory(no_request=reason == 'no-request')
            if reason == 'invalid': fixture['funds'][0]['strategyClaimsOnly']['taskDigest'] = '0' * 64
            value = claims_collector.collect(fixture, china_today(), provider_factory=factory,
                deadline=time.monotonic() - 1 if reason == 'deferred' else None)
            self.assertEqual(calls, []); self.assertEqual(value['strategyClaimsReceipts'], [])
            self.assertEqual(value['strategyClaimsPatches'], [])
            self.assertEqual(len(value['errors']), 1)

    def test_second_pdf_request_is_blocked_and_cannot_return_success(self):
        factory, calls, _ = provider_factory(second_request=True)
        value = claims_collector.collect(config(), china_today(), provider_factory=factory)
        self.assertEqual(len(calls), 1); self.assertEqual(value['strategyClaimsPatches'], [])
        self.assertEqual(value['strategyClaimsReceipts'][0]['errorCode'], 'UNEXPECTED_REQUEST')

    def test_underlying_http_redirect_is_disabled_and_rejected_without_response_details(self):
        factory, _, _ = provider_factory()
        http_calls = []
        class Response:
            status_code = 302
            closed = False
            def close(self): self.closed = True
            @property
            def headers(self): raise AssertionError('headers must remain private')
            @property
            def content(self): raise AssertionError('body must remain private')
        response = Response()
        class Session:
            def get(self, url, **kwargs): http_calls.append((url, kwargs)); return response
            def close(self): pass
        class HTTPProvider(factory):
            get = PublicProvider.get
            def __init__(self): self.session = Session(); self.timeout = 18; self.attempts = 2
        value = claims_collector.collect(config(), china_today(), provider_factory=HTTPProvider)
        self.assertEqual(len(http_calls), 1); self.assertFalse(http_calls[0][1]['allow_redirects'])
        self.assertTrue(response.closed)
        self.assertEqual(value['strategyClaimsReceipts'][0]['errorCode'], 'HTTP_REJECTED')
        self.assertEqual(value['strategyClaimsPatches'], [])
        self.assertNotIn('Location', json.dumps(value))

    def test_changed_original_source_is_receipt_only_and_never_changes_task_binding(self):
        factory, calls, _ = provider_factory()
        original = factory.report_details
        def details(self, document, **kwargs):
            value = original(self, document, **kwargs)
            value['strategySectionCoverage']['sourceTextHash'] = '0' * 64
            return value
        factory.report_details = details
        fixture = config(); before = copy.deepcopy(fixture)
        value = claims_collector.collect(fixture, china_today(), provider_factory=factory)
        self.assertEqual(len(calls), 1); self.assertEqual(fixture, before)
        self.assertEqual(value['strategyClaimsPatches'], [])
        self.assertEqual(value['strategyClaimsReceipts'][0]['errorCode'], 'STRATEGY_CLAIMS_SOURCE_CHANGED')

    def test_two_parallel_requests_and_one_failure_preserve_receipts_without_duplicate_funds(self):
        barrier = threading.Barrier(2)
        factory, calls, _ = provider_factory(before_get=lambda _: barrier.wait(timeout=5))
        original = factory.report_details
        def details(self, document, **kwargs):
            if document['fundCode'] == '000002':
                self.get(document['sourceUrl'], binary=True); raise DataError('PDF_PARSE_FAILED')
            return original(self, document, **kwargs)
        factory.report_details = details
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'checkpoint.json'
            value = claims_collector.collect(config(('000001', '000002')), china_today(),
                lambda x: collector.write_checkpoint(path, x), factory)
            persisted = json.loads(path.read_text())
        self.assertEqual(len(calls), 2); self.assertEqual(persisted, value)
        self.assertEqual(len(value['strategyClaimsReceipts']), 2); self.assertEqual(len(value['strategyClaimsPatches']), 1)
        self.assertEqual(value['funds'], [])
        self.assertEqual({r['code']: r['outcome'] for r in value['strategyClaimsReceipts']}, {'000001': 'parsed', '000002': 'failed'})

    def test_main_route_mixed_and_strategy_only_preserves_ordinary_fund(self):
        for mixed in (False, True):
            fixture = config(); factory, calls, _ = provider_factory()
            ordinary = {'code': '000003', 'reports': [], 'retrievedAt': REPORT['retrievedAt']}
            if mixed: fixture['funds'].append({'code': '000003'})
            with patch.object(collector, 'PublicProvider', factory), patch.object(collector, 'collect_fund', return_value=(ordinary, [])) as normal:
                value = collector.collect(fixture, china_today())
            self.assertEqual(normal.call_count, int(mixed)); self.assertEqual(len(calls), 1)
            self.assertEqual(value['funds'], [ordinary] if mixed else [])
            self.assertEqual(value['strategyClaimsReceipts'][0]['outcome'], 'parsed')

    def test_duplicate_or_two_lane_binding_refused_before_request(self):
        factory, calls, _ = provider_factory()
        for change in ('duplicate', 'priceOnly', 'reportLocator', 'reportLocatorOnly', 'qualityCodes'):
            fixture = config()
            if change == 'duplicate': fixture['funds'].append(copy.deepcopy(fixture['funds'][0]))
            elif change == 'qualityCodes': fixture['traversal'][change] = ['000001']
            else: fixture['funds'][0][change] = {}
            with patch.object(collector, 'PublicProvider', factory), self.assertRaises(DataError):
                collector.collect(fixture, china_today())
        self.assertEqual(calls, [])

    def test_real_collector_result_builds_zero_fresh_funds_and_strictly_bound_patch(self):
        fixture = config(); factory, _, _ = provider_factory()
        raw = claims_collector.collect(fixture, china_today(), provider_factory=factory)
        script = """const fs=require('fs'),{buildSnapshot}=require('./scripts/build-data');
const x=JSON.parse(fs.readFileSync(0,'utf8')); const s=buildSnapshot(x.raw,{config:x.config,now:x.raw.retrievedAt});
console.log(JSON.stringify({funds:s.funds.length, fresh:s.coverage.batchFreshFundCount,
receipts:s.strategyClaimsReceipts.length,patches:s.strategyClaimsPatches.length}));"""
        out = subprocess.run(['node', '-e', script], cwd=Path(__file__).parents[1], input=json.dumps({'raw': raw, 'config': fixture}),
            capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(out.stdout), {'funds': 0, 'fresh': 0, 'receipts': 1, 'patches': 1})

    def test_real_failed_and_deferred_collector_outputs_build_status_without_spending_other_lanes(self):
        import traversal
        script = """const fs=require('fs'),{buildSnapshot}=require('./scripts/build-data');
const x=JSON.parse(fs.readFileSync(0,'utf8'));console.log(JSON.stringify(buildSnapshot(x.raw,{config:x.config,now:x.raw.retrievedAt})));"""
        for deferred in (False, True):
            fixture = config(); factory, _, _ = provider_factory(failure=True)
            raw = claims_collector.collect(fixture, china_today(), provider_factory=factory,
                deadline=time.monotonic() - 1 if deferred else None)
            output = subprocess.run(['node', '-e', script], cwd=Path(__file__).parents[1],
                input=json.dumps({'raw': raw, 'config': fixture}), capture_output=True, text=True, timeout=30, check=True)
            snapshot = json.loads(output.stdout)
            status = traversal.build_status(fixture, snapshot, snapshot['retrievedAt'])
            self.assertTrue(traversal.valid_status(status, fixture, snapshot['retrievedAt']))
            row = status['funds'][0]
            self.assertEqual(row['strategyClaimsAttempt'], not deferred)
            self.assertEqual(snapshot['coverage']['batchFreshFundCount'], 0)
            self.assertEqual(snapshot['funds'], [])
            self.assertEqual(row['attempted'], not deferred)
            for key in ('qualityAttempt', 'managerAttempt', 'strategyAttempt', 'enrichmentAttempt'):
                self.assertFalse(row[key])


if __name__ == '__main__': unittest.main()
