"""Bounded synthetic subprocesses exercise interrupted directory recovery."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import campaign
import discover
import report_locators
from traversal import atomic_json

OLD = '2026-10-25T12:00:00.123456+00:00'
NEW = '2026-10-26T12:00:00.654321+00:00'


def catalogs():
    companies = [{'code': f'8000000{i}', 'name': f'合成机构{i}', 'sourceUrl': f'https://fund.eastmoney.com/company/8000000{i}.html'} for i in (1, 2)]
    funds = {f'{i:06d}': {'code': f'{i:06d}', 'name': f'合成基金{i}', 'type': '混合型', 'catalogSourceUrl': discover.FUNDS_URL} for i in (1, 2)}
    mappings = {c['code']: {'funds': [{**funds[f'{i:06d}'], 'companyCode': c['code']}], 'validation': {}} for i, c in enumerate(companies, 1)}
    old = discover.build_catalog(companies, funds, mappings, '2026-10-25', {c['code'] for c in companies})
    old['retrievedAt'] = OLD; old['coverage']['scope'] = 'all-directory-companies'
    old = discover.retain_known_catalog(old, None)
    # The new master temporarily omits an old code. Retention must keep it.
    fresh = discover.build_catalog(companies, {'000001': funds['000001']}, {'80000001': mappings['80000001']},
        '2026-10-26', {c['code'] for c in companies}, attempts={'80000001': {'count': 1, 'status': 'complete',
        'lastAttemptAt': NEW, 'lastFinishedAt': NEW}})
    fresh['coverage']['upstreamFundRows'] = old['coverage']['upstreamFundRows']
    fresh['coverage']['scope'] = 'all-directory-companies'; fresh['retrievedAt'] = NEW
    return old, fresh


class CampaignRefreshBudgetTests(unittest.TestCase):
    def test_real_timed_out_child_is_reaped_and_atomic_partial_checkpoint_resumes(self):
        old, fresh = catalogs()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); data = root / 'web/data'; data.mkdir(parents=True)
            atomic_json(data / 'catalog.json', old)
            (root / 'scripts').mkdir()
            (root / 'scripts/discover.py').write_text("import json,os,pathlib,time\n"
                "root=pathlib.Path.cwd()\n(root/'child.pid').write_text(str(os.getpid()))\n"
                f"value=json.loads({json.dumps(json.dumps(fresh))})\n"
                "p=root/'web/data/catalog.json'\nt=p.with_suffix('.tmp')\nt.write_text(json.dumps(value))\nt.replace(p)\ntime.sleep(60)\n")
            previous = Path.cwd()
            try:
                os.chdir(root); started = time.monotonic()
                result = campaign.refresh_directory(data, '2026-10-26', max_seconds=.3)
            finally: os.chdir(previous)
            self.assertLess(time.monotonic() - started, 5)
            with self.assertRaises(ProcessLookupError): os.kill(int((root / 'child.pid').read_text()), 0)
            saved = json.loads((data / 'catalog.json').read_text())
            self.assertEqual(result['errorCode'], 'DIRECTORY_REFRESH_TIME_LIMIT')
            self.assertEqual(result['status'], 'partial'); self.assertTrue(result['checkpointRetained'])
            self.assertFalse(saved['coverage']['directoryComplete'])
            self.assertEqual(saved['retrievedAt'], NEW)
            self.assertEqual({row['code'] for row in saved['funds']}, {'000001', '000002'})
            stale = next(row for row in saved['companies'] if row['code'] == '80000002')
            self.assertEqual(stale['directoryStatus'], 'stale'); self.assertEqual(stale['directoryAsOf'], '2026-10-25')
            self.assertEqual(stale['directoryAttempts'], 0); self.assertEqual(stale['directoryAttemptStatus'], 'pending')
            self.assertIsNone(stale['lastDirectoryAttemptAt'])
            successful = next(row for row in saved['companies'] if row['code'] == '80000001')
            recovered = discover.restore_company_records(saved, successful)
            self.assertEqual(len(recovered), successful['fundCount'])
            pending = discover.select_pending(saved['companies'], {'80000001': {'funds': recovered}},
                {row['code']: discover.catalog_attempt(row) for row in saved['companies']})
            self.assertEqual([row['code'] for row in pending], ['80000002'])

    def test_timeout_before_checkpoint_keeps_exact_old_partial_bytes(self):
        old, _ = catalogs(); old['coverage']['directoryComplete'] = False
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder); original = json.dumps(old, ensure_ascii=False, indent=3).encode() + b'\n'
            (data / 'catalog.json').write_bytes(original)
            with patch('campaign.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic', 1)):
                result = campaign.refresh_directory(data, '2026-10-26', max_seconds=1)
            self.assertEqual(result['status'], 'cached'); self.assertFalse(result['checkpointRetained'])
            self.assertEqual((data / 'catalog.json').read_bytes(), original)

    def test_invalid_or_regressed_checkpoint_restores_original_without_new_clock(self):
        for mutate in [lambda value: value['coverage'].update(scope='limited-probe'),
                       lambda value: value.update(sourceUrls={}),
                       lambda value: value.update(companies=[], companyCount=0)]:
            old, fresh = catalogs(); mutate(fresh)
            with tempfile.TemporaryDirectory() as folder:
                data = Path(folder); original = json.dumps(old).encode(); (data / 'catalog.json').write_bytes(original)
                def failed(*args, **kwargs):
                    atomic_json(data / 'catalog.json', fresh)
                    raise subprocess.CalledProcessError(1, 'synthetic')
                with patch('campaign.subprocess.run', side_effect=failed):
                    result = campaign.refresh_directory(data, '2026-10-26')
                self.assertEqual(result['status'], 'cached')
                self.assertEqual((data / 'catalog.json').read_bytes(), original)

    def test_locator_transport_receives_phase_budget_and_retains_partial_observations(self):
        from test_report_locators import result, NOW
        partial_result = result(); partial_result['windowEnumerationComplete'] = False
        with patch('report_locators.ActionsTransport') as transport, \
             patch('report_locators.pagination_probe', return_value={'mayEnumerate': True, 'status': 'verified-pagination',
                 'verifiedPageSize': 30, 'observedAt': NOW}), \
             patch('report_locators.discover_period', return_value=partial_result):
            received = report_locators.fetch_results(['2026Q2'], '2026-10-26', now=NOW, max_seconds=17)
        transport.assert_called_once_with(max_seconds=17)
        with tempfile.TemporaryDirectory() as folder:
            registry = report_locators.refresh_registries(Path(folder), ['2026Q2'], '2026-10-26', now=NOW,
                fetcher=lambda *args, **kwargs: received)['2026Q2']
        self.assertEqual(registry['refreshStatus'], 'partial')
        self.assertFalse(registry['windowEnumerationComplete']); self.assertEqual(len(registry['records']), 1)
        self.assertEqual(registry['records'][0]['retrievedAt'], NOW)

    def test_exhausted_history_budget_uses_cached_evidence_without_request_or_restamp(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
             patch('campaign.fetch_historical_overlay') as fetch:
            overlay, state = campaign.quarter_history(Path(folder), '2026Q2', allow_fetch=False)
        fetch.assert_not_called(); self.assertIsNone(overlay)
        self.assertEqual(state, {'status': 'deferred', 'errorCode': 'HISTORICAL_REFRESH_TIME_BUDGET'})

    def test_real_two_quarter_plan_keeps_full_batch_scope_after_refresh_budget_is_spent(self):
        from test_campaign_history import workspace
        clock = {'at': 0}
        def directory(root, as_of, *, max_seconds):
            self.assertEqual(max_seconds, 600)
            # A small amount of checkpoint recovery work follows the hard wait.
            clock['at'] = 610
            return {'status': 'cached', 'asOf': as_of, 'errorCode': 'DIRECTORY_REFRESH_TIME_LIMIT'}
        def locators(root, quarters, as_of, *, allow_fetch, fetcher):
            self.assertTrue(allow_fetch); self.assertEqual(fetcher.keywords['max_seconds'], 240)
            clock['at'] = 850
            return {quarter: report_locators.empty_registry(quarter) for quarter in quarters}
        real_history = campaign.quarter_history
        with workspace(), patch('campaign.time.monotonic', side_effect=lambda: clock['at']), \
             patch('campaign.refresh_directory', side_effect=directory), \
             patch('campaign.refresh_registries', side_effect=locators), \
             patch('campaign.quarter_history', wraps=real_history) as history, \
             patch('campaign.fetch_historical_overlay') as fetch:
            result = campaign.plan('both', '2026-10-26', historical_overlays={}, source_repair=True)
            matrix = json.loads(Path('.cache/plan/matrix.json').read_text())
            self.assertEqual([row['quarter'] for row in matrix], ['2026Q2', '2026Q3'])
            self.assertEqual(result['batchCount'], 2)
            for item in matrix:
                config = json.loads((Path('.cache/plan') / item['config']).read_text())
                self.assertEqual(len(config['funds']), 1)
                self.assertEqual(config['collectionLane'], 'source-repair-no-market-v1')
                self.assertEqual(config['maxPriceSymbols'], 0)
            self.assertTrue(all(call.kwargs['allow_fetch'] is False for call in history.call_args_list))
            fetch.assert_not_called()


if __name__ == '__main__': unittest.main()
