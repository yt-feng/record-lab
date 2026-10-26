import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import importlib.util
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import collect
import collect_prices
import price_patches
import traversal
import test_traversal as support
from price_coverage import expected_sessions, price_series
from providers import DataError, checked_price_rows
from publication_guard import inspect_content


class QuoteProvider:
    calls = []
    fail = False
    def overview(self, *args): raise AssertionError('overview must not run')
    def announcements(self, *args): raise AssertionError('announcements must not run')
    def holdings(self, *args): raise AssertionError('holdings must not run')
    def report_details(self, *args): raise AssertionError('PDF must not run')
    def price_bars(self, code, market, start, end):
        self.on_request()
        self.calls.append((code, market, start, end))
        if self.fail: raise DataError('NETWORK_ERROR')
        rows = checked_price_rows([{'date': day, 'low': 1, 'high': 2, 'close': 1.5} for day in expected_sessions(market, start, end)], start, end)
        return price_series(code, market, 'CNY', 'fixture', 'https://example.org/quotes', rows, start, end)


class PricePatchTests(unittest.TestCase):
    def setUp(self):
        QuoteProvider.calls = []; QuoteProvider.fail = False
        self.fund = support.TraversalTests().retained_fund()
        self.fund['rows'][0]['priceRange'] = {'status': 'missing'}
        self.phase = support.PHASE
        self.binding = price_patches.binding_for(self.fund, self.phase)
        self.planned = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=2)).isoformat()
        self.config = {**support.CONFIG, 'requireReportQuality': True,
                       'funds': [{'code': '000001', 'companyCode': 'co1', 'priceOnly': self.binding,
                                  'priceTargets': [{'market': 'CN', 'stockCode': '600001'}]}],
                       'traversal': {'plannedAt': self.planned, 'enrichmentCodes': ['000001'], 'qualityCodes': [], 'managerCodes': [], 'strategyCodes': []}}

    def test_price_only_collect_calls_only_quotes_and_checkpoints_the_actual_start(self):
        snapshots = []
        with patch.object(collect, 'PublicProvider', QuoteProvider), patch.object(collect, 'collect_fund', side_effect=AssertionError('no report collection')):
            raw = collect.collect(self.config, '2026-10-26', checkpoint=lambda value: snapshots.append(copy.deepcopy(value)))
        self.assertEqual(len(QuoteProvider.calls), 1)
        self.assertEqual(raw['funds'], [])
        self.assertEqual(raw['pricePatches'][0]['attemptedStockKeys'], ['CN:600001'])
        started = [s for s in snapshots if s['pricePatches'][0]['attemptedStockKeys']]
        self.assertEqual(started[0]['barsByStock'], {})
        self.assertEqual(len(started[-1]['barsByStock']), 1)
        self.assertFalse(inspect_content(json.dumps(raw).encode()))

    def test_deadline_or_invalid_binding_never_starts_transport(self):
        raw = collect_prices.collect(self.config, '2026-10-26', provider_factory=QuoteProvider, deadline=time.monotonic()-1)
        self.assertEqual(QuoteProvider.calls, [])
        self.assertEqual(raw['pricePatches'][0]['attemptedStockKeys'], [])
        broken = copy.deepcopy(self.config); broken['funds'][0]['priceOnly']['selectedReport']['sourceUrl'] = 'file:///not-public'
        raw = collect_prices.collect(broken, '2026-10-26', provider_factory=QuoteProvider)
        self.assertEqual(raw['pricePatches'][0]['errorCode'], 'PRICE_BINDING_INVALID')
        self.assertEqual(QuoteProvider.calls, [])

    def test_binding_contains_current_and_exact_prior_disclosures_without_text(self):
        f = copy.deepcopy(self.fund)
        f['previousReport']['holdings'].append({'market': 'HK', 'stockCode': '00700'})
        binding = price_patches.binding_for(f, self.phase)
        self.assertEqual(binding['stockKeys'], ['CN:600001', 'HK:00700'])
        self.assertNotIn('narrative', json.dumps(binding))
        f['previousReport']['periodEnd'] = '2025-12-31'
        self.assertIsNone(price_patches.binding_for(f, self.phase))

    def test_malformed_binding_and_non_public_sources_cannot_start_or_consume_requests(self):
        invalid = [None, {}, {**self.binding, 'selectedReport': None}]
        for url in ('https://localhost/report', 'https://127.0.0.1/report', 'https://10.0.0.1/report',
                    'https://example.local/report', 'https://[::1]/report', 'https://example.org/report#secret',
                    'https://example.org/report?token=', 'https://example.org/report?API_KEY=private'):
            invalid.append({**self.binding, 'selectedReport': {**self.binding['selectedReport'], 'sourceUrl': url}})
        for binding in invalid:
            with self.subTest(binding=binding):
                config = copy.deepcopy(self.config); config['funds'][0]['priceOnly'] = binding
                raw = collect_prices.collect(config, '2026-10-26', provider_factory=QuoteProvider)
                row = price_patches.status_for(config['funds'][0], raw['pricePatches'][0], self.phase)
                self.assertEqual(QuoteProvider.calls, [])
                self.assertFalse(row['enrichmentAttempt'])
                self.assertFalse(row['attempted'])
                self.assertEqual(row['errorCode'], 'PRICE_BINDING_INVALID')
        self.assertIsNone(price_patches.stock_key({'market': 'CN', 'stockCode': '00700'}))
        self.assertIsNone(price_patches.stock_key({'market': 'HK', 'stockCode': '600001'}))

    def test_negative_limits_do_not_slice_into_requests(self):
        for field in ('maxPriceSymbols', 'maxPriceSeconds'):
            raw = collect_prices.collect({**self.config, field: -1}, '2026-10-26', provider_factory=QuoteProvider)
            self.assertFalse(raw['pricePatches'][0]['attemptedStockKeys'])
        self.assertEqual(QuoteProvider.calls, [])
        for value in (None, 'bad', float('nan'), float('inf')):
            self.assertEqual(collect_prices.bounded(value, 2, 3), 2)

    def test_mixed_batch_never_fetches_reports_for_price_only_items(self):
        config = copy.deepcopy(self.config)
        config['funds'].append({'code': '000002', 'companyCode': 'co1'})
        ordinary = {'code': '000002', 'reports': [], 'collectionAttempt': {'attempted': False}}
        with patch.object(collect, 'PublicProvider', QuoteProvider), patch.object(collect, 'collect_fund', return_value=(ordinary, [])) as fetch:
            raw = collect.collect(config, '2026-10-26')
        self.assertEqual([call.args[0]['code'] for call in fetch.call_args_list], ['000002'])
        self.assertEqual([f['code'] for f in raw['funds']], ['000002'])
        self.assertEqual(len(QuoteProvider.calls), 1)
        self.assertEqual(raw['pricePatches'][0]['attemptedStockKeys'], ['CN:600001'])

    def test_missing_binding_with_exhausted_report_budget_never_uses_unbound_price_lane(self):
        previous = traversal.merge_progress(support.catalog(), None, self.config, [support.event()], support.NOW, retained=[self.fund])
        state = previous['funds'][0]
        state['quality']['attempts'] = 3
        plan = traversal.plan_batches(support.catalog(), previous, self.config, 1, 3, now=support.NOW)
        self.assertNotIn('000001', [i['code'] for b in plan['batches'].values() for i in b['funds']])

    def test_planner_emits_source_bound_repairs_only_with_retained_identity(self):
        previous = traversal.merge_progress(support.catalog(), None, self.config, [support.event()], support.NOW, retained=[self.fund])
        plan = traversal.plan_batches(support.catalog(), previous, self.config, 1, 3, now=support.NOW, retained=[self.fund])
        items = [i for b in plan['batches'].values() for i in b['funds']]
        selected = next(i for i in items if i['code'] == '000001')
        self.assertEqual(selected['priceOnly'], self.binding)
        self.assertTrue(price_patches.valid_target(selected, self.phase))

    def test_unmapped_directory_company_uses_null_binding_and_can_request_prices(self):
        fund = copy.deepcopy(self.fund); fund['companyId'] = 'unknown'
        binding = price_patches.binding_for(fund, self.phase)
        self.assertIsNone(binding['companyCode'])
        config = copy.deepcopy(self.config)
        config['funds'][0].update({'companyCode': None, 'priceOnly': binding})
        self.assertTrue(price_patches.valid_target(config['funds'][0], self.phase))
        raw = collect_prices.collect(config, '2026-10-26', provider_factory=QuoteProvider)
        self.assertEqual(raw['pricePatches'][0]['attemptedStockKeys'], ['CN:600001'])

    def test_failure_and_unstarted_receipts_do_not_consume_report_manager_or_strategy_budget(self):
        previous = traversal.merge_progress(support.catalog(), None, self.config, [support.event()], support.NOW, retained=[self.fund])
        before = previous['funds'][0]
        QuoteProvider.fail = True
        raw = collect_prices.collect(self.config, '2026-10-26', provider_factory=QuoteProvider)
        public_patch = {**raw['pricePatches'][0], 'schemaVersion': 1, 'priceOnly': True, 'prices': []}
        snapshot = {'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': '2026-06-30', 'funds': [], 'pricePatches': [public_patch]}
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        status = traversal.build_status(self.config, snapshot, now)
        self.assertTrue(traversal.valid_status(status, self.config, now))
        after = traversal.merge_progress(support.catalog(), previous, self.config, [status], now, retained=[self.fund])
        state = after['funds'][0]
        self.assertEqual(state['enrichment']['attempts'], before['enrichment']['attempts']+1)
        for field in ('attempts', 'managerAttempts', 'strategyAttempts'):
            self.assertEqual(state['quality'].get(field), before['quality'].get(field))
        self.assertEqual(state['lastAttemptAt'], before['lastAttemptAt'])
        replay = traversal.merge_progress(support.catalog(), after, self.config, [status], now, retained=[self.fund])
        self.assertEqual(replay['funds'][0]['enrichment']['attempts'], state['enrichment']['attempts'])
        rebuilt = copy.deepcopy(status)
        rebuilt['attemptedAt'] = (dt.datetime.fromisoformat(now) + dt.timedelta(seconds=1)).isoformat()
        replay = traversal.merge_progress(support.catalog(), after, self.config, [rebuilt], rebuilt['attemptedAt'], retained=[self.fund])
        self.assertEqual(replay['funds'][0]['enrichment']['attempts'], state['enrichment']['attempts'])
        public_patch['attemptedStockKeys'] = []; public_patch['attemptedAt'] = None
        deferred = traversal.build_status(self.config, snapshot, now)
        self.assertTrue(traversal.valid_status(deferred, self.config, now))
        unchanged = traversal.merge_progress(support.catalog(), previous, self.config, [deferred], now, retained=[self.fund])
        self.assertEqual(unchanged['funds'][0]['enrichment']['attempts'], before['enrichment']['attempts'])

    def test_three_actual_quote_failures_exhaust_only_enrichment_budget(self):
        previous = traversal.merge_progress(support.catalog(), None, self.config, [support.event()], support.NOW, retained=[self.fund])
        before = copy.deepcopy(previous['funds'][0])
        for index in range(3):
            at = (dt.datetime.fromisoformat(self.planned) + dt.timedelta(seconds=index+3)).isoformat()
            public = {'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': self.phase['targetPeriodEnd'], 'funds': [],
                      'pricePatches': [{'code': '000001', 'binding': self.binding, 'attemptedStockKeys': ['CN:600001'], 'attemptedAt': at}]}
            status = traversal.build_status(self.config, public, at)
            previous = traversal.merge_progress(support.catalog(), previous, self.config, [status], at, retained=[self.fund])
        state = previous['funds'][0]
        self.assertEqual(state['enrichment']['status'], 'exhausted')
        self.assertEqual(state['enrichment']['attempts'], 3)
        self.assertEqual(state['lastAttemptAt'], before['lastAttemptAt'])
        for field in ('attempts', 'managerAttempts', 'strategyAttempts'):
            self.assertEqual(state['quality'].get(field), before['quality'].get(field))

    def test_prior_only_holdings_remain_price_targets_for_no_equities_report(self):
        fund = copy.deepcopy(self.fund)
        fund['status'] = 'no-equities'; fund['selectedReport']['holdings'] = []
        binding = price_patches.binding_for(fund, self.phase)
        self.assertEqual(binding['stockKeys'], ['CN:600001'])
        fund['previousReport']['holdings'] = []
        self.assertIsNone(price_patches.binding_for(fund, self.phase))

    def test_missing_part_recovery_preserves_zero_requests_and_valid_receipt(self):
        spec = importlib.util.spec_from_file_location('price_status_recovery', ROOT / 'scripts/complete-status.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            plan = Path(folder)/'plan'; plan.mkdir(); parts = Path(folder)/'parts'
            (plan/'matrix.json').write_text(json.dumps([{'id': '0', 'config': 'batch-0.json'}]))
            (plan/'batch-0.json').write_text(json.dumps(self.config))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            self.assertEqual(module.reconcile(plan, parts, now)['repairedCount'], 1)
            status = json.loads((parts/'part-0/status.json').read_text())
            self.assertTrue(traversal.valid_status(status, self.config, now))
            self.assertFalse(status['funds'][0]['attempted'])
            self.assertFalse(status['funds'][0]['enrichmentAttempt'])
            self.assertEqual(module.reconcile(plan, parts, now)['preservedCount'], 1)

    def test_changed_identity_requeues_report_without_fabricating_quote_attempt(self):
        previous = traversal.merge_progress(support.catalog(), None, self.config, [support.event()], support.NOW, retained=[self.fund])
        status = traversal.build_status(self.config, None, dt.datetime.now(dt.timezone.utc).isoformat())
        self.assertEqual(status['funds'][0]['errorCode'], 'PRICE_BINDING_INVALID')
        after = traversal.merge_progress(support.catalog(), previous, self.config, [status], retained=[self.fund])
        self.assertTrue(after['funds'][0]['priceBindingNeedsReport'])
        plan = traversal.plan_batches(support.catalog(), after, self.config, 1, 3, retained=[self.fund])
        item = next(i for b in plan['batches'].values() for i in b['funds'] if i['code'] == '000001')
        self.assertNotIn('priceOnly', item)

    def test_python_collector_node_publication_merge_and_python_receipt_round_trip(self):
        raw = collect_prices.collect(self.config, '2026-10-26', provider_factory=QuoteProvider)
        script = r"""
const fs=require('node:fs'); const {buildSnapshot}=require('./scripts/build-data'); const {mergeSnapshots}=require('./scripts/merge-parts');
const x=JSON.parse(fs.readFileSync(0,'utf8')), f=x.fund, phase=x.config;
const catalog={companies:[{code:'co1',name:'合成公司',fundCount:1}],funds:[{code:'000001',companyCode:'co1',companyName:'合成公司'}]};
f.selectedReport.managers=f.reportManagers;f.selectedReport.managerTableCoverage=f.managerTableCoverage;
f.selectedReport.managerSourceUrl=f.managerSourceUrl;f.selectedReport.narrative=f.narrative;
f.reports=[f.selectedReport,f.previousReport];
for(const r of f.reports){r.kind='quarterly';r.coverage='top10';}
const previous=buildSnapshot({asOf:'2026-09-29',retrievedAt:f.retrievedAt,funds:[f],barsByStock:{},errors:[]},
  {config:{...phase,funds:[{code:f.code}]},catalog,now:x.now});
const prior=mergeSnapshots({snapshots:[previous],config:phase,catalog,now:x.now});
const snapshot=buildSnapshot(x.raw,{config:phase,now:x.now});
const merged=mergeSnapshots({snapshots:[snapshot],previousManifest:prior.manifest,previousCompanies:prior.companySnapshots,config:phase,catalog,now:x.now});
process.stdout.write(JSON.stringify({snapshot,receipt:merged.receipt,fund:merged.companySnapshots.co1.funds[0]}));
"""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        result = subprocess.run(['node', '-e', script], cwd=ROOT,
                                input=json.dumps({'raw': raw, 'config': self.config, 'fund': self.fund, 'now': now}),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        published = json.loads(result.stdout)
        self.assertFalse(inspect_content(result.stdout.encode()))
        status = traversal.build_status(self.config, published['snapshot'], now)
        self.assertTrue(traversal.valid_status(status, self.config, now))
        events = traversal.reconcile_observations([status], published['receipt'], self.config, now)
        self.assertTrue(events[0]['funds'][0]['patchAccepted'])
        previous = traversal.merge_progress(support.catalog(), None, self.config, [support.event()], support.NOW, retained=[self.fund])
        updated = traversal.merge_progress(support.catalog(), previous, self.config, events, now, retained=[published['fund']])
        self.assertEqual(updated['funds'][0]['enrichment']['status'], 'complete')
        self.assertEqual(updated['funds'][0]['quality']['fields']['prices'], 'complete')
        self.assertEqual(updated['funds'][0]['enrichment']['attempts'], 1)
        self.assertEqual(updated['funds'][0]['quality']['attempts'], previous['funds'][0]['quality']['attempts'])
        self.assertEqual(published['fund']['retrievedAt'], self.fund['retrievedAt'])


if __name__ == '__main__': unittest.main()
