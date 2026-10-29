"""Small source stubs and real local planning/publishing; no remote directory reads."""
import contextlib
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import campaign
from quarter_catalog import compose_quarter_catalog, normalize_overlay, recover_historical_overlay, combine_historical_overlays
from traversal import atomic_json, read_json

NOW = '2026-10-26T12:00:00Z'
AS_OF = '2026-10-26'
URL = 'https://example.org/official-june.xlsx'


def current():
    return {'schemaVersion': 1, 'asOf': AS_OF, 'retrievedAt': '2026-10-26T00:00:00Z',
            'companyCount': 1, 'fundCount': 1, 'coverage': {'directoryComplete': True, 'fundCount': 1, 'companyCount': 1},
            'companies': [{'code': 'co1', 'name': '合成机构', 'fundCount': 1, 'directoryStatus': 'complete'}],
            'funds': [{'code': '000001', 'name': '当前产品', 'type': '混合型', 'companyCode': 'co1', 'companyName': '合成机构'}]}


def overlay(codes=('000001', '000003'), retrieved='2026-10-26T01:00:00Z'):
    return {'schemaVersion': 1, 'targetPeriodEnd': '2026-06-30', 'parseStatus': 'complete',
            'evidence': [{'id': 'synthetic-june', 'sourceUrl': URL, 'format': 'xlsx',
                          'effectiveDate': '2026-06-30', 'retrievedAt': retrieved, 'recordUnit': 'fund-product-code',
                          'contentHash': 'a' * 64, 'parsedRows': len(codes), 'expectedRows': len(codes), 'unresolvedRows': 0}],
            'records': [{'code': code, 'name': '历史产品' + code, 'type': '未分类', 'inceptionDate': '2023-07-11',
                         'inceptionSourceUrl': URL, 'evidence': [{'sourceId': 'synthetic-june', 'sheet': 'Sheet1', 'row': row}]}
                        for row, code in enumerate(codes, 3)]}


class SourceFailure(Exception):
    code = 'TIMEOUT'


@contextlib.contextmanager
def workspace():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for directory in ('scripts', 'lib', 'config'):
            shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'web').mkdir()
        for name in ('lifecycle.js', 'zero-stock.js', 'nonstock-assets.js', 'strategy-sections.js', 'source-dates.js', 'price-range-reference.js'):
            shutil.copy2(source / 'web' / name, root / 'web' / name)
        atomic_json(root / 'web/data/catalog.json', current())
        previous = Path.cwd(); real_run = subprocess.run
        def local_run(command, **kwargs):
            kwargs.setdefault('capture_output', True)
            return real_run(command, **kwargs)
        try:
            os.chdir(root)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': '123456',
                                        'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40}), \
                    patch('campaign.subprocess.run', side_effect=local_run):
                yield root
        finally:
            os.chdir(previous)


class CampaignHistoryTests(unittest.TestCase):
    def test_inception_is_bound_to_a_referenced_source_and_period_and_has_separate_catalog_fields(self):
        raw = overlay()
        clean = normalize_overlay(raw, '2026-06-30', now=NOW)
        self.assertEqual(clean['records'][1]['inceptionDate'], '2023-07-11')
        scoped = compose_quarter_catalog(current(), '2026Q2', raw, now=NOW)
        found = scoped['funds'][1]
        self.assertEqual(found['historicalInceptionDate'], '2023-07-11')
        self.assertEqual(found['historicalInceptionSourceUrl'], URL)
        self.assertNotIn('inceptionDate', found)
        self.assertIsNone(found['companyCode'])
        for change in [lambda x: x.update(inceptionDate='2026-07-01'),
                       lambda x: x.update(inceptionDate='2026-02-30'),
                       lambda x: x.update(inceptionSourceUrl='https://example.org/unreferenced.xlsx'),
                       lambda x: x.pop('inceptionDate'), lambda x: x.pop('inceptionSourceUrl')]:
            bad = overlay(); change(bad['records'][0])
            with self.assertRaisesRegex(ValueError, 'INVALID_HISTORICAL_INCEPTION'):
                normalize_overlay(bad, '2026-06-30', now=NOW)

    def test_default_actions_plan_fetches_only_q2_queues_unknown_codes_and_publishes_the_frozen_catalog(self):
        with workspace():
            before = Path('web/data/catalog.json').read_bytes()
            with patch('campaign.fetch_historical_overlay', return_value=overlay()) as fetch:
                campaign.plan('both', AS_OF, shards=1, batch_size=10, refresh=False)
            fetch.assert_called_once_with()
            self.assertEqual(Path('web/data/catalog.json').read_bytes(), before)
            q2 = read_json('web/data/periods/2026Q2/catalog.json')
            q3 = read_json('web/data/periods/2026Q3/catalog.json')
            self.assertEqual([row['code'] for row in q2['funds']], ['000001', '000003'])
            self.assertEqual([row['code'] for row in q3['funds']], ['000001'])
            self.assertEqual(q2['quarterUniverse']['historicalRefresh'], {'status': 'refreshed'})
            self.assertEqual(q3['quarterUniverse']['historicalParseStatus'], 'unchecked')
            self.assertEqual(q3['quarterUniverse']['sources'], [])
            matrix = read_json('.cache/plan/matrix.json')
            batches = {item['quarter']: read_json('.cache/plan/' + item['config']) for item in matrix}
            historical = next(row for row in batches['2026Q2']['funds'] if row['code'] == '000003')
            self.assertIsNone(historical.get('companyCode'))
            self.assertEqual([row['code'] for row in batches['2026Q3']['funds']], ['000001'])
            self.assertFalse(q2['quarterUniverse']['historicalUniverseVerified'])
            frozen = Path('.cache/campaign/catalogs/2026Q2.json').read_bytes()
            changed = current(); changed['funds'] = [{'code': '999999', 'name': '后来目录', 'companyCode': 'co1'}]
            atomic_json('web/data/catalog.json', changed)
            campaign.publish()
            self.assertEqual(Path('web/data/periods/2026Q2/catalog.json').read_bytes(), frozen)
            self.assertEqual(read_json('web/data/catalog.json'), changed)
            progress = read_json('web/data/periods/2026Q2/progress.json')
            self.assertEqual(progress['totalFunds'], 2)
            self.assertEqual(progress['notApplicableFunds'], 0)
            self.assertEqual(read_json('web/data/periods/2026Q2/analysis.json')['coverage']['status'], 'awaiting-data')

    def test_failed_default_refresh_keeps_codes_original_source_timestamp_hash_and_inception(self):
        with workspace():
            with patch('campaign.fetch_historical_overlay', return_value=overlay()):
                campaign.plan('2026Q2', AS_OF, shards=1, batch_size=10, refresh=False)
            original = read_json('web/data/periods/2026Q2/catalog.json')
            with patch('campaign.fetch_historical_overlay', side_effect=SourceFailure('private exception detail')):
                campaign.plan('2026Q2', AS_OF, shards=1, batch_size=10, refresh=False)
            result = read_json('web/data/periods/2026Q2/catalog.json')
            self.assertEqual(result['funds'], original['funds'])
            self.assertEqual(result['quarterUniverse']['sources'], original['quarterUniverse']['sources'])
            self.assertEqual(result['retrievedAt'], original['retrievedAt'])
            self.assertEqual(result['quarterUniverse']['historicalRefresh'], {'status': 'cached', 'errorCode': 'HISTORICAL_TIMEOUT'})
            self.assertEqual(result['quarterUniverse']['historicalParseStatus'], 'complete')
            self.assertFalse(result['quarterUniverse']['historicalUniverseVerified'])
            self.assertNotIn('private exception', json.dumps(result))
            self.assertIn('000003', [row['code'] for row in read_json('.cache/plan/batch-0.json')['funds']])

    def test_explicit_overlays_take_precedence_and_local_planning_never_fetches(self):
        with workspace(), patch('campaign.fetch_historical_overlay') as fetch:
            campaign.plan('both', AS_OF, shards=1, batch_size=10, refresh=False, historical_overlays={'2026Q2': overlay()})
            fetch.assert_not_called()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
                campaign.plan('both', AS_OF, shards=1, batch_size=10, refresh=False)
            fetch.assert_not_called()
            retained = read_json('web/data/periods/2026Q2/catalog.json')
            self.assertIn('000003', [row['code'] for row in retained['funds']])
            self.assertEqual(retained['quarterUniverse']['sources'][0]['retrievedAt'], overlay()['evidence'][0]['retrievedAt'])

    def test_failed_fetch_without_cache_continues_current_codes_with_unchecked_history(self):
        with workspace(), patch('campaign.fetch_historical_overlay', side_effect=SourceFailure):
            campaign.plan('2026Q2', AS_OF, shards=1, batch_size=10, refresh=False)
            result = read_json('web/data/periods/2026Q2/catalog.json')
            self.assertEqual([row['code'] for row in result['funds']], ['000001'])
            self.assertEqual(result['quarterUniverse']['historicalParseStatus'], 'unchecked')
            self.assertEqual(result['quarterUniverse']['historicalRefresh'], {'status': 'unavailable', 'errorCode': 'HISTORICAL_TIMEOUT'})
            self.assertFalse(result['quarterUniverse']['historicalUniverseVerified'])
            self.assertEqual(len(read_json('.cache/plan/batch-0.json')['funds']), 1)

    def test_empty_corrupt_future_and_cross_quarter_cache_cannot_supply_complete_evidence(self):
        scoped = compose_quarter_catalog(current(), '2026Q2', overlay(), now=NOW)
        for change in [lambda x: x.update(funds=[]), lambda x: x.update(targetPeriodEnd='2026-09-30'),
                       lambda x: x['quarterUniverse']['sources'][0].update(contentHash='bad'),
                       lambda x: x['quarterUniverse']['sources'][0].update(retrievedAt='2099-01-01T00:00:00Z'),
                       lambda x: x['funds'][1]['historicalPresenceEvidence'][0].update(sourceId='missing')]:
            bad = copy.deepcopy(scoped); change(bad)
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                    patch('campaign.fetch_historical_overlay', side_effect=SourceFailure):
                atomic_json(Path(folder) / 'periods/2026Q2/catalog.json', bad)
                result, state = campaign.quarter_history(folder, '2026Q2', now=NOW)
                self.assertIsNone(result)
                self.assertEqual(state['cacheErrorCode'], 'HISTORICAL_CACHE_INVALID')
        missing = copy.deepcopy(scoped); missing['funds'].pop()
        recovered = recover_historical_overlay(missing, '2026Q2', now=NOW)
        self.assertEqual(recovered['parseStatus'], 'partial')
        self.assertEqual(recovered['evidence'], scoped['quarterUniverse']['sources'])
        self.assertEqual(len(recovered['records']), 1)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                patch('campaign.fetch_historical_overlay') as fetch:
            atomic_json(Path(folder) / 'periods/2026Q3/catalog.json', scoped)
            result, state = campaign.quarter_history(folder, '2026Q3', now=NOW)
            self.assertIsNone(result); fetch.assert_not_called()
            self.assertEqual(state['cacheErrorCode'], 'HISTORICAL_CACHE_INVALID')

    def test_changed_source_missing_a_cached_code_retains_old_evidence_without_redating_it(self):
        old = overlay()
        fresh = overlay(('000001',), retrieved='2026-10-26T02:00:00Z')
        fresh['parseStatus'] = 'partial'
        combined = combine_historical_overlays(fresh, old, '2026-06-30', now=NOW)
        self.assertEqual({row['code'] for row in combined['records']}, {'000001', '000003'})
        retained = next(row for row in combined['records'] if row['code'] == '000003')
        source = next(source for source in combined['evidence'] if source['id'] == retained['evidence'][0]['sourceId'])
        self.assertEqual(source['retrievedAt'], old['evidence'][0]['retrievedAt'])
        self.assertEqual(source['contentHash'], old['evidence'][0]['contentHash'])
        self.assertEqual(combined['parseStatus'], 'partial')
        frozen = compose_quarter_catalog(current(), '2026Q2', combined, now=NOW)
        recovered = recover_historical_overlay(frozen, '2026Q2', now=NOW)
        self.assertEqual([row['code'] for row in recovered['records']], [row['code'] for row in combined['records']])
        self.assertEqual(recovered['records'][1], retained)
        self.assertEqual(recovered['records'][0]['evidence'], combined['records'][0]['evidence'])
        self.assertEqual(recovered['evidence'], combined['evidence'])

    def test_empty_or_wrong_quarter_fetch_is_a_fixed_parse_failure_and_preserves_valid_cache(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            cache = compose_quarter_catalog(current(), '2026Q2', overlay(), now=NOW)
            atomic_json(Path(folder) / 'periods/2026Q2/catalog.json', cache)
            for fresh in [overlay(()), {**overlay(), 'targetPeriodEnd': '2026-09-30'}]:
                with patch('campaign.fetch_historical_overlay', return_value=fresh):
                    result, state = campaign.quarter_history(folder, '2026Q2', now=NOW)
                self.assertEqual(state, {'status': 'cached', 'errorCode': 'HISTORICAL_PARSE_FAILED'})
                self.assertEqual(result['evidence'], cache['quarterUniverse']['sources'])
                self.assertEqual([row['code'] for row in result['records']], ['000001', '000003'])

    def test_broken_source_proof_or_unreadable_catalog_keeps_existing_report_as_unverified_candidate(self):
        for corruption in ('missing-source', 'broken-json'):
            with self.subTest(corruption=corruption), workspace():
                previous_catalog = compose_quarter_catalog(current(), '2026Q2', overlay(), now=NOW)
                atomic_json('.cache/seed-catalog.json', previous_catalog)
                atomic_json('.cache/seed-config.json', {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31',
                                                       'funds': [{'code': '000003'}]})
                atomic_json('.cache/seed-input.json', {'asOf': AS_OF, 'retrievedAt': '2026-10-26T01:00:00Z',
                    'errors': [], 'barsByStock': {}, 'funds': [{'code': '000003', 'name': '合成历史基金', 'reports': [{
                        'fundCode': '000003', 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20',
                        'kind': 'quarterly', 'coverage': 'top10', 'sourceUrl': 'https://example.org/quarter-report.pdf', 'navYuan': 10000,
                        'holdings': [{'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN', 'currency': 'CNY',
                                      'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}]}]}]})
                subprocess.run(['node', 'scripts/build-data.js', '--input', '.cache/seed-input.json',
                    '--output', '.cache/seed-parts/part-0/part.json', '--config', '.cache/seed-config.json'], check=True)
                subprocess.run(['node', 'scripts/merge-parts.js', '--parts', '.cache/seed-parts',
                    '--data', 'web/data/periods/2026Q2', '--catalog', '.cache/seed-catalog.json',
                    '--config', '.cache/seed-config.json'], check=True)
                prior_manifest = read_json('web/data/periods/2026Q2/latest.json')
                prior_path = prior_manifest['companyFiles'][0]['path'].replace('./data/', 'web/data/periods/2026Q2/')
                original_fund = read_json(prior_path)['funds'][0]
                catalog_path = Path('web/data/periods/2026Q2/catalog.json')
                if corruption == 'broken-json': catalog_path.write_text('{invalid')
                else:
                    previous_catalog['quarterUniverse'].pop('sources')
                    previous_catalog['funds'][1].update(name='discarded untrusted name', historicalInceptionDate='2099-01-01')
                    atomic_json(catalog_path, previous_catalog)
                with patch('campaign.fetch_historical_overlay', side_effect=SourceFailure):
                    campaign.plan('2026Q2', AS_OF, shards=1, batch_size=10, refresh=False)
                retained = read_json(catalog_path)
                candidate = next(row for row in retained['funds'] if row['code'] == '000003')
                self.assertEqual(candidate['name'], '历史待核验产品 000003')
                self.assertTrue(candidate['historicalCandidate'])
                self.assertIsNone(candidate['companyCode'])
                for field in ('historicalPresenceEvidence', 'historicalInceptionDate', 'historicalInceptionSourceUrl'):
                    self.assertNotIn(field, candidate)
                self.assertEqual(retained['quarterUniverse']['historicalAddedFundCount'], 0)
                self.assertEqual(retained['quarterUniverse']['historicalCandidateFundCount'], 1)
                self.assertEqual(retained['quarterUniverse']['historicalParseStatus'], 'unchecked')
                self.assertEqual(retained['quarterUniverse']['sources'], [])
                self.assertFalse(retained['quarterUniverse']['historicalUniverseVerified'])
                # This fixture tests a later historical-source refresh. Record
                # explicitly unstarted work; a genuinely missing artifact now
                # holds continuation until its unknown requests are reconciled.
                for item in read_json('.cache/plan/matrix.json'):
                    batch = read_json('.cache/plan/' + item['config'])
                    atomic_json('.cache/parts/part-' + item['id'] + '/part.json', {
                        'schemaVersion': 1, 'sourceMode': 'actions', 'funds': [],
                        'targetPeriodEnd': batch['targetPeriodEnd'], 'baselinePeriodEnd': batch['baselinePeriodEnd'],
                        'batchResults': [{'code': row['code'], 'status': 'deferred-budget', 'attempted': False,
                                          'errorCode': 'BATCH_DEADLINE_REACHED'} for row in batch['funds']]})
                campaign.publish()
                final_manifest = read_json('web/data/periods/2026Q2/latest.json')
                final_path = final_manifest['companyFiles'][0]['path'].replace('./data/', 'web/data/periods/2026Q2/')
                final_fund = read_json(final_path)['funds'][0]
                self.assertEqual(final_fund['code'], '000003')
                self.assertEqual(final_fund['retrievedAt'], original_fund['retrievedAt'])
                self.assertEqual(final_fund['selectedReport']['sourceUrl'], original_fund['selectedReport']['sourceUrl'])
                self.assertEqual(read_json('web/data/periods/2026Q2/progress.json')['totalFunds'], 2)
                with patch('campaign.fetch_historical_overlay', return_value=overlay()):
                    campaign.plan('2026Q2', AS_OF, shards=1, batch_size=10, refresh=False)
                restored = read_json(catalog_path)
                restored_row = next(row for row in restored['funds'] if row['code'] == '000003')
                self.assertNotIn('historicalCandidate', restored_row)
                self.assertEqual(restored_row['historicalInceptionDate'], '2023-07-11')
                self.assertEqual(restored['quarterUniverse']['historicalAddedFundCount'], 1)
                self.assertEqual(restored['quarterUniverse']['historicalCandidateFundCount'], 0)


if __name__ == '__main__': unittest.main()
