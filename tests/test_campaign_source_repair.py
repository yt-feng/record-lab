"""A tiny real source-only publish under an unchanged historical market hold."""
import os
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import campaign
import collect
import campaign_accounting
import request_receipts
import traversal
from test_campaign_history import workspace, current
import test_traversal as traversal_fixtures
CONFIG = traversal_fixtures.CONFIG

PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
LANE = {'collectionLane': traversal.SOURCE_REPAIR_LANE, 'maxPriceSymbols': 0, 'maxPriceSeconds': 0}


def hold():
    return {'schemaVersion': 1, 'quarter': '2026Q2', 'phase': PHASE,
            'continuationHold': {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': '2026Q2',
                'sourceRunId': '123', 'batchIds': ['10', '13'], 'requestAccounting': 'unverified'},
            'batchInputErrors': [{'batchId': value, 'quarter': '2026Q2', 'errorCode': 'BATCH_ARTIFACT_MISSING'}
                                for value in ('10', '13')]}


def fund(item, *args, **kwargs):
    reports = []
    for end, published in [('2026-06-30', '2026-07-20'), ('2026-03-31', '2026-04-20')]:
        reports.append({'title': '合成基金季度报告', 'periodEnd': end, 'kind': 'quarterly',
            'publishedAt': published, 'sourceUrl': 'https://example.org/' + end + '.pdf',
            'coverage': 'top10', 'navYuan': 1000000, 'narrative': None,
            'holdingsSourceUrl': 'https://example.org/' + end + '.pdf',
            'holdings': [{'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN',
                          'weightPct': 5, 'shares': 1000, 'marketValueYuan': 50000}]})
    return {'code': item['code'], 'name': '合成产品', 'companyId': 'co1', 'managers': [], 'reports': reports}, []


class SourceRepairTests(unittest.TestCase):
    def test_collector_retains_reports_without_constructing_any_market_provider(self):
        config = {**CONFIG, **LANE, 'funds': [{'code': '000001'}]}
        with patch('collect.collect_fund', side_effect=fund), patch('collect.PublicProvider') as provider:
            result = collect.collect(config, '2026-10-29')
        provider.assert_not_called()
        self.assertEqual(len(result['funds'][0]['reports']), 2)
        self.assertEqual(result['barsByStock'], {})
        self.assertEqual(result['coverage']['priceSymbolsRequested'], 0)
        self.assertEqual(result['coverage']['priceSymbolsAttempted'], 0)

    def test_source_lane_rejects_mixed_or_tampered_market_configuration_before_calls(self):
        base = {**CONFIG, **LANE, 'funds': [{'code': '000001'}]}
        for changes in ({'maxPriceSymbols': 1}, {'maxPriceSeconds': False}, {'collectionLane': 'unknown'},
                        {'funds': [{'code': '000001', 'priceOnly': {}}]},
                        {'funds': [{'code': '000001', 'priceTargets': []}]},
                        {'traversal': {'enrichmentCodes': ['000001']}}):
            with self.subTest(changes=changes), patch('collect.collect_fund') as source:
                with self.assertRaisesRegex(Exception, 'INVALID_SOURCE_REPAIR_LANE'):
                    collect.collect({**base, **changes}, '2026-10-29')
                source.assert_not_called()

    def test_source_ready_filter_and_progress_exclude_price_only_work(self):
        config = {**CONFIG, **LANE, 'requireReportQuality': True}
        item = traversal_fixtures.TraversalTests().retained_fund()
        item['rows'][0]['priceRange'] = {'status': 'unavailable'}
        directory = traversal_fixtures.catalog()
        directory['funds'] = directory['funds'][:1]; directory['companies'] = directory['companies'][:1]
        directory['companies'][0]['fundCount'] = 1
        progress = traversal.merge_progress(directory, None, config, [traversal_fixtures.event()],
                                             now=traversal_fixtures.NOW, retained=[item])
        self.assertEqual(progress['funds'][0]['quality']['missingFields'], ['prices'])
        self.assertTrue(progress['continueRun'])  # Full research is still incomplete.
        self.assertFalse(progress['sourceRepairContinueRun'])
        self.assertEqual(progress['completionStatus'], 'incomplete-running')
        planned = traversal.plan_batches(directory, progress, config, 1, 1,
                                          now=traversal_fixtures.NOW, retained=[item])
        self.assertEqual(planned['matrix'], [])

    def prepare(self):
        directory = current()
        directory['funds'].append({'code': '000002', 'name': '合成乙', 'companyCode': 'co1'})
        directory['fundCount'] = directory['coverage']['fundCount'] = directory['companies'][0]['fundCount'] = 2
        traversal.atomic_json('web/data/catalog.json', directory)
        original = hold()
        traversal.atomic_json('web/data/periods/2026Q2/audit.json', original)
        # Preserve original uncertain accounting independently of the new run.
        old = {'runId': '123', 'runAttempt': 1, 'batchId': '10', 'sourceCommit': 'b' * 40,
               'configSha256': 'c' * 64, 'plannedAt': '2026-10-26T00:00:00Z',
               'observedAt': '2026-10-26T01:00:00Z', 'status': 'missing', 'counts': None,
               'receiptPath': None, 'receiptSha256': None, 'errorCode': 'MARKET_RECEIPT_MISSING'}
        campaign_accounting.persist_accounting('web/data/periods/2026Q2', '2026Q2', [(old, None)])
        planned = campaign.plan('2026Q2', '2026-10-29', shards=1, batch_size=1, refresh=False,
                                historical_overlays={}, source_repair=True)
        self.assertEqual(planned['batchCount'], 1)
        self.assertEqual(planned['heldQuarters'], ['2026Q2'])
        config = traversal.read_json('.cache/plan/batch-0.json')
        self.assertTrue(traversal.source_repair_mode(config))
        self.assertEqual(config['traversal']['enrichmentCodes'], [])
        with patch('collect.collect_fund', side_effect=fund), patch('collect.PublicProvider') as provider:
            payload = collect.collect(config, '2026-10-29')
        provider.assert_not_called()
        traversal.atomic_json('raw/input.json', payload)
        subprocess.run(['node', 'scripts/build-data.js', '--input', 'raw/input.json', '--output',
                        '.cache/parts/part-0/part.json', '--config', '.cache/plan/batch-0.json'],
                       check=True, capture_output=True)
        return original, old, config

    def receipt(self, config, *, market_request=False):
        raw = Path('.cache/plan/batch-0.json').read_bytes()
        env = {**os.environ, 'RECEIPT_BATCH_ID': '0'}
        journal = request_receipts.RequestJournal('.cache/private-receipt', config, raw, env=env)
        if market_request:
            observer = journal.observer('CN:600001', ('600001', 'CN', '2026-04-01', '2026-06-30'))
            reservation = observer.begin('https://push2his.eastmoney.com/api/qt/stock/kline/get')
            observer.finish(reservation, 'returned')
        journal.close(True)
        value = request_receipts.validate('.cache/private-receipt', config, raw, env=env)
        target = Path('.cache/request-accounting') / f"request-accounting-{env['GITHUB_RUN_ID']}-1-0/receipts.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(request_receipts.encoded(value))

    def test_real_source_publish_advances_data_preserves_hold_and_unknown_and_can_continue_only_source_lane(self):
        with workspace():
            original, old, config = self.prepare()
            self.receipt(config)
            result = campaign.publish()
            self.assertTrue(result['sourceRepair']); self.assertTrue(result['continueRun'])
            audit = traversal.read_json('web/data/periods/2026Q2/audit.json')
            self.assertEqual(audit['continuationHold'], original['continuationHold'])
            self.assertEqual(audit['batchInputErrors'], original['batchInputErrors'])
            self.assertEqual(audit['processedFunds'], 1)
            self.assertEqual(audit['pendingFunds'], 1)
            self.assertFalse(audit['continueRun'])
            self.assertFalse(audit['marketAccounting']['continuationAllowed'])
            self.assertTrue(audit['sourceRepair']['currentMarketAccountingVerifiedZero'])
            ledger = campaign_accounting.load_index('web/data/periods/2026Q2', '2026Q2')
            self.assertEqual(ledger['entries'][0], old)
            self.assertEqual(ledger['entries'][1]['counts']['sessionGetReservations'], 0)
            self.assertEqual(campaign.plan('2026Q2', '2026-10-29', shards=1, batch_size=1,
                refresh=False, historical_overlays={})['status'], 'held')

    def test_missing_receipt_retains_source_data_but_stops_only_source_chain(self):
        with workspace():
            original, old, config = self.prepare()
            result = campaign.publish()
            self.assertFalse(result['continueRun'])
            audit = traversal.read_json('web/data/periods/2026Q2/audit.json')
            self.assertEqual(audit['processedFunds'], 1)
            self.assertEqual(audit['continuationHold'], original['continuationHold'])
            self.assertFalse(audit['sourceRepair']['currentMarketAccountingVerifiedZero'])

    def test_source_pdf_update_through_real_disk_merge_preserves_complete_price_and_original_clock(self):
        with workspace():
            original, old, config = self.prepare()
            self.receipt(config)
            campaign.publish()
            quarter = Path('web/data/periods/2026Q2')
            manifest = traversal.read_json(quarter / 'latest.json')
            self.assertTrue(manifest['companyFiles'])  # Exercise the paged disk path.
            quote = traversal_fixtures.complete_price()
            quote.update(sourceUrl='https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600001&klt=101&fqt=0&beg=20260401&end=20260630',
                         fetchedAt='2026-10-27T01:02:03.123456Z')
            quote['sourceUrls'] = [quote['sourceUrl']]
            expected = copy.deepcopy(quote)
            modified = 0
            for path in (quarter / 'companies').glob('*.json'):
                page = traversal.read_json(path)
                for item in page['funds']:
                    if item['code'] == '000001':
                        item['rows'][0]['priceRange'] = copy.deepcopy(quote); modified += 1
                traversal.atomic_json(path, page)
            self.assertEqual(modified, 1)
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '123457'}):
                campaign.plan('2026Q2', '2026-10-29', shards=1, batch_size=2, refresh=False,
                              historical_overlays={}, source_repair=True)
                config = traversal.read_json('.cache/plan/batch-0.json')
                self.assertIn('000001', [item['code'] for item in config['funds']])
                def updated(item, *args, **kwargs):
                    value, issues = fund(item)
                    value['reports'][0].update(parseMethod='pypdf', sourceTextHash='d' * 64,
                        pageStats={'pageCount': 1, 'totalPages': 1, 'processedPages': 1, 'nonEmptyPages': 1,
                                   'charCount': 1000, 'textStatus': 'usable', 'truncated': False})
                    return value, issues
                with patch('collect.collect_fund', side_effect=updated), patch('collect.PublicProvider') as provider:
                    payload = collect.collect(config, '2026-10-29')
                provider.assert_not_called(); self.assertEqual(payload['barsByStock'], {})
                traversal.atomic_json('raw/input.json', payload)
                subprocess.run(['node', 'scripts/build-data.js', '--input', 'raw/input.json', '--output',
                    '.cache/parts/part-0/part.json', '--config', '.cache/plan/batch-0.json'], check=True, capture_output=True)
                shutil.rmtree('.cache/private-receipt')
                self.receipt(config)
                campaign.publish()
            retained = next(item for item in traversal.load_retained_funds(quarter) if item['code'] == '000001')
            self.assertEqual(retained['selectedReport']['parseMethod'], 'pypdf')
            self.assertEqual(retained['selectedReport']['sourceTextHash'], 'd' * 64)
            # The renderer may add cached/display labels. Every stored price
            # fact, session proof, source URL and original clock stays exact.
            self.assertEqual({key: retained['rows'][0]['priceRange'][key] for key in expected}, expected)
            audit = traversal.read_json(quarter / 'audit.json')
            self.assertEqual(audit['continuationHold'], original['continuationHold'])
            self.assertTrue(audit['sourceRepair']['currentMarketAccountingVerifiedZero'])

    def test_nonzero_receipt_cannot_authorize_source_continuation(self):
        with workspace():
            original, old, config = self.prepare()
            self.receipt(config, market_request=True)
            result = campaign.publish()
            self.assertFalse(result['continueRun'])
            audit = traversal.read_json('web/data/periods/2026Q2/audit.json')
            self.assertFalse(audit['sourceRepair']['currentMarketAccountingVerifiedZero'])
            ledger = campaign_accounting.load_index('web/data/periods/2026Q2', '2026Q2')
            self.assertEqual(ledger['entries'][0], old)
            self.assertEqual(ledger['entries'][1]['counts']['sessionGetReservations'], 1)

    def test_new_missing_part_does_not_replace_original_hold_or_continue_source_chain(self):
        with workspace():
            original, old, config = self.prepare()
            self.receipt(config)
            shutil.rmtree('.cache/parts/part-0')
            result = campaign.publish()
            self.assertFalse(result['continueRun'])
            audit = traversal.read_json('web/data/periods/2026Q2/audit.json')
            self.assertEqual(audit['continuationHold'], original['continuationHold'])
            self.assertEqual(audit['batchInputErrors'], original['batchInputErrors'])
            self.assertEqual(audit['sourceRepair']['batchInputErrors'][0]['batchId'], '0')

    def test_publisher_requires_mode_in_every_frozen_batch_and_quarter_config(self):
        with workspace():
            self.prepare()
            selection = traversal.read_json('.cache/campaign/selection.json')
            matrix = traversal.read_json('.cache/plan/matrix.json')
            for path in ('.cache/campaign/2026Q2.json', '.cache/plan/batch-0.json'):
                original = Path(path).read_bytes()
                config = traversal.read_json(path); del config['collectionLane']
                traversal.atomic_json(path, config)
                with self.assertRaisesRegex(ValueError, 'INVALID_SOURCE_REPAIR_LANE'):
                    campaign.source_repair_for_publish(selection, matrix)
                Path(path).write_bytes(original)

    def test_workflow_source_mode_is_opt_in_and_propagates_on_continuation(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/traverse.yml').read_text()
        self.assertIn('SOURCE_REPAIR: ${{ inputs.source_repair || false }}', workflow)
        self.assertIn('ARGS+=(--source-repair)', workflow)
        self.assertIn('-f source_repair="$SOURCE_REPAIR"', workflow)

    def test_source_collection_has_its_own_lock_and_publication_holds_writer_lock_through_deploy(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/traverse.yml').read_text()
        before_jobs, jobs = workflow.split('jobs:', 1)
        self.assertIn('group: traversal-records', before_jobs)
        self.assertNotIn('group: published-records', before_jobs)
        before_publish, publish = jobs.split('  publish:', 1)
        self.assertNotIn('group: published-records', before_publish)
        self.assertIn('group: published-records', publish)
        self.assertIn('id-token: write', publish)
        self.assertIn('pages: write', publish)
        self.assertNotIn('\n  deploy:', publish)
        self.assertLess(publish.index('scripts/persist-observations.py'), publish.index('actions/deploy-pages@v4'))
        self.assertLess(publish.index('actions/deploy-pages@v4'), publish.index('Continue unfinished traversal'))


if __name__ == '__main__': unittest.main()
