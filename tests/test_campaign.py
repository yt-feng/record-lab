import gzip
import importlib.util
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
from campaign import quarter_config, seed_quarter, selected_periods, plan, publish, deployed_continuation_state
from traversal import atomic_json, read_json

spec = importlib.util.spec_from_file_location('prepare_site', Path(__file__).resolve().parents[1] / 'scripts/prepare-site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        binding = patch.dict(os.environ, {'GITHUB_RUN_ID': '123456', 'GITHUB_RUN_ATTEMPT': '1',
                                        'GITHUB_SHA': 'a' * 40})
        binding.start(); self.addCleanup(binding.stop)

    def test_default_campaign_matrix_passes_workflow_output_and_missing_part_reconciliation(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory in ('scripts', 'lib', 'config'):
                shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'web').mkdir()
            for module in ('lifecycle.js', 'zero-stock.js', 'nonstock-assets.js', 'strategy-sections.js', 'source-dates.js', 'price-range-reference.js'):
                shutil.copy2(source / 'web' / module, root / 'web' / module)
            atomic_json(root / 'web/data/catalog.json', {'schemaVersion': 1, 'asOf': '2026-10-26',
                'coverage': {'directoryComplete': True, 'companyCount': 1, 'fundCount': 1200},
                'companies': [{'code': 'co1', 'name': '合成机构', 'fundCount': 1200, 'directoryStatus': 'complete'}],
                'funds': [{'code': f'{index:06d}', 'companyCode': 'co1'} for index in range(1, 1201)]})
            original = Path.cwd()
            real_run = subprocess.run
            def captured_run(command, **kwargs):
                kwargs.setdefault('capture_output', True)
                return real_run(command, **kwargs)
            try:
                os.chdir(root)
                with patch('campaign.subprocess.run', side_effect=captured_run):
                    result = plan('both', '2026-10-26', refresh=False, historical_overlays={})
                self.assertEqual(result['batchCount'], 32)
                matrix = read_json('.cache/plan/matrix.json')
                self.assertEqual([item['id'] for item in matrix],
                                 [str(index + offset) for index in range(16) for offset in (0, 16)])
                task_count = 0
                for quarter in ('2026Q2', '2026Q3'):
                    scoped = [item for item in matrix if item['quarter'] == quarter]
                    self.assertEqual(len(scoped), 16)
                    codes = []
                    for item in scoped:
                        batch = read_json('.cache/plan/' + item['config'])
                        self.assertEqual(len(batch['funds']), 75)
                        self.assertEqual(batch['traversal']['batchId'], item['id'])
                        codes.extend(row['code'] for row in batch['funds'])
                    self.assertEqual(len(set(codes)), 1200)
                    task_count += len(codes)
                self.assertEqual(task_count, 2400)
                output = subprocess.run(['node', 'scripts/workflow-outputs.js', '.cache/plan'],
                                        check=True, capture_output=True, text=True)
                values = dict(line.split('=', 1) for line in output.stdout.splitlines())
                self.assertEqual(json.loads(values['matrix']), matrix)
                self.assertEqual(values['has_work'], 'true')
                reconciled = subprocess.run([sys.executable, 'scripts/complete-status.py'],
                                            check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(reconciled.stdout)['repairedCount'], 32)
                statuses = list(Path('.cache/parts').glob('part-*/status.json'))
                self.assertEqual(len(statuses), 32)
                self.assertEqual(sum(len(read_json(path)['funds']) for path in statuses), 2400)
                repeated = subprocess.run([sys.executable, 'scripts/complete-status.py'],
                                          check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(repeated.stdout)['preservedCount'], 32)
            finally:
                os.chdir(original)

    def test_campaign_rejects_unsupported_capacity_before_directory_refresh(self):
        with patch('campaign.refresh_directory') as refresh:
            for quarter, shards, batch_size in [('both', 17, 75), ('2026Q3', 33, 75),
                                                ('both', True, 75), ('both', 16, 101)]:
                with self.subTest(quarter=quarter, shards=shards, batch_size=batch_size):
                    with self.assertRaisesRegex(ValueError, 'INVALID_BATCH_CAPACITY'):
                        plan(quarter, '2026-10-26', shards=shards, batch_size=batch_size)
            refresh.assert_not_called()

    def test_two_quarter_campaign_keeps_missing_parts_and_exemptions_in_their_phase(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory in ('scripts', 'lib', 'config'):
                shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'web').mkdir()
            for module in ('lifecycle.js', 'zero-stock.js', 'nonstock-assets.js', 'strategy-sections.js', 'source-dates.js', 'price-range-reference.js'):
                shutil.copy2(source / 'web' / module, root / 'web' / module)
            catalog = {'schemaVersion': 1, 'asOf': '2026-10-26', 'retrievedAt': '2026-10-26T00:00:00Z',
                'coverage': {'directoryComplete': True, 'companyCount': 1, 'fundCount': 2},
                'companies': [{'code': 'co1', 'name': '合成公司', 'fundCount': 2, 'directoryStatus': 'complete'}],
                'funds': [{'code': '000001', 'name': '合成甲', 'companyCode': 'co1'},
                          {'code': '000002', 'name': '合成乙', 'companyCode': 'co1'}]}
            atomic_json(root / 'web/data/catalog.json', catalog)
            atomic_json(root / 'web/data/latest.json', {'schemaVersion': 1, 'sourceMode': 'actions',
                'asOf': '2026-09-29', 'retrievedAt': '2026-09-29T12:00:00Z', 'targetPeriodEnd': '2026-06-30',
                'baselinePeriodEnd': '2026-03-31', 'funds': [], 'companyFiles': [], 'coverage': {}})
            original = Path.cwd()
            real_run = subprocess.run
            def portable_run(command, **kwargs):
                command = [sys.executable, *command[1:]] if command[0] == 'python' else command
                kwargs.setdefault('capture_output', True)
                if command[:2] == ['node', 'scripts/analyze-data.js']:
                    kwargs['env'] = {**os.environ, 'GITHUB_ACTIONS': 'true'}
                return real_run(command, **kwargs)
            child_runtime = patch('campaign.subprocess.run', side_effect=portable_run)
            try:
                os.chdir(root)
                child_runtime.start()
                plan('both', '2026-10-26', shards=1, batch_size=2, refresh=False, historical_overlays={})
                matrix = read_json('.cache/plan/matrix.json')
                self.assertEqual([item['quarter'] for item in matrix], ['2026Q2', '2026Q3'])
                self.assertEqual([item['id'] for item in matrix], ['0', '1'])
                for item in matrix:
                    batch = read_json('.cache/plan/' + item['config'])
                    self.assertEqual(batch['traversal']['batchId'], item['id'])
                    self.assertTrue(batch['requireReportQuality'])
                self.assertEqual(batch['collectorVersion'], 5)
                # One new product is explicitly post-Q3; all other batch work
                # is absent, as if a runner stopped before writing its artifact.
                import datetime as dt
                stamp = dt.datetime.now(dt.timezone.utc).isoformat()
                atomic_json('raw/input.json', {'asOf': '2026-10-26', 'retrievedAt': stamp, 'funds': [{
                    'code': '000002', 'name': '合成乙', 'reports': [], 'managers': [],
                    'notApplicableEvidence': {'reason': 'ESTABLISHED_AFTER_PERIOD', 'date': '2026-10-01',
                        'sourceUrl': 'https://fundf10.eastmoney.com/jbgk_000002.html',
                        'periodEnd': '2026-09-30', 'fundCode': '000002'}}], 'barsByStock': {}, 'errors': []})
                subprocess.run(['node', 'scripts/build-data.js', '--input', 'raw/input.json',
                    '--output', '.cache/parts/part-1/part.json', '--config', '.cache/plan/batch-1.json'],
                    check=True, capture_output=True)
                # A present invalid part blocks publication and persists a batch
                # diagnostic, never fabricated per-product failures.
                invalid = Path('.cache/parts/part-0/part.json')
                invalid.parent.mkdir(parents=True)
                invalid.write_text('{')
                previous_data = {path: path.read_bytes() for path in Path('web/data').rglob('*.json')}
                with self.assertRaisesRegex(Exception, '^BATCH_STATUS_INPUT_ERROR$'):
                    publish()
                blocked = read_json('.cache/campaign/result.json')
                self.assertFalse(blocked['continueRun'])
                self.assertEqual(blocked['inputErrorCount'], 1)
                self.assertEqual(blocked['batchInputErrors'][0]['errorCode'], 'SNAPSHOT_INVALID_JSON')
                self.assertNotIn('funds', read_json('.cache/parts/part-0/status.json'))
                self.assertTrue(all(path.read_bytes() == before for path, before in previous_data.items()))
                # The following independent fixture models a genuinely absent
                # artifact, not a retry that clears a real diagnostic.
                invalid.unlink()
                Path('.cache/parts/part-0/status.json').unlink()
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '123456'}):
                    result = publish()
                self.assertEqual([item['quarter'] for item in result['quarters']], ['2026Q2', '2026Q3'])
                q2 = read_json('web/data/periods/2026Q2/progress.json')
                q3 = read_json('web/data/periods/2026Q3/progress.json')
                self.assertEqual(q2['phase']['targetPeriodEnd'], '2026-06-30')
                self.assertEqual(q3['phase']['targetPeriodEnd'], '2026-09-30')
                self.assertEqual(q2['failedFunds'], 2)
                self.assertEqual(q2['notApplicableFunds'], 0)
                self.assertEqual(q3['failedFunds'], 1)
                self.assertEqual(q3['notApplicableFunds'], 1)
                self.assertEqual(q3['processedFunds'], 0)
                self.assertFalse(result['continueRun'])
                self.assertEqual(result['missingPartIds'], ['0'])
                self.assertEqual(result['missingPartCount'], 1)
                self.assertEqual(result['batchInputErrors'], [{'batchId': '0', 'quarter': '2026Q2', 'errorCode': 'BATCH_ARTIFACT_MISSING'}])
                q2_audit_before = Path('web/data/periods/2026Q2/audit.json').read_bytes()
                q2_hold = read_json('web/data/periods/2026Q2/audit.json')['continuationHold']
                self.assertEqual(q2_hold['quarter'], '2026Q2')
                self.assertEqual(q2_hold['batchIds'], ['0'])
                self.assertEqual(q2_hold['sourceRunId'], '123456')
                self.assertEqual(read_json('web/data/periods/2026Q2/analysis.json')['kind'], 'quarter-analysis')
                self.assertEqual(read_json('web/data/periods/2026Q3/audit.json')['phase']['targetPeriodEnd'], '2026-09-30')
                self.assertIn('completionAudit', read_json('web/data/periods/2026Q2/audit.json'))
                self.assertEqual(read_json('web/data/periods.json')['defaultPeriod'], '2026Q3')
                self.assertEqual(read_json('web/data/latest.json')['targetPeriodEnd'], '2026-06-30')
                # A later scheduled plan skips only the held quarter. Publishing
                # Q3 again must leave Q2's original hold and observations intact.
                shutil.rmtree('.cache/parts')
                next_plan = plan('both', '2026-10-26', shards=1, batch_size=2, refresh=False, historical_overlays={})
                self.assertEqual(next_plan['heldQuarters'], ['2026Q2'])
                self.assertEqual(next_plan['quarters'], ['2026Q3'])
                next_matrix = read_json('.cache/plan/matrix.json')
                self.assertTrue(next_matrix)
                self.assertTrue(all(item['quarter'] == '2026Q3' for item in next_matrix))
                for item in next_matrix:
                    next_batch = read_json('.cache/plan/' + item['config'])
                    raw = {'asOf': '2026-10-26', 'retrievedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
                           'funds': [{'code': row['code'], 'reports': [], 'collectionAttempt': {'attempted': False,
                                      'status': 'deferred-budget', 'errorCode': 'BATCH_DEADLINE_REACHED'}} for row in next_batch['funds']],
                           'barsByStock': {}, 'errors': []}
                    atomic_json('raw/input.json', raw)
                    subprocess.run(['node', 'scripts/build-data.js', '--input', 'raw/input.json',
                        '--output', f".cache/parts/part-{item['id']}/part.json", '--config', '.cache/plan/' + item['config']],
                        check=True, capture_output=True)
                next_result = publish()
                self.assertEqual(next_result['missingPartCount'], 0)
                self.assertEqual(next_result['heldQuarters'], ['2026Q2'])
                self.assertEqual(Path('web/data/periods/2026Q2/audit.json').read_bytes(), q2_audit_before)
            finally:
                child_runtime.stop()
                os.chdir(original)

    def test_all_held_plan_and_publish_make_no_requests_or_production_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hold = {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': '2026Q2', 'sourceRunId': '123456',
                    'batchIds': ['10', '13'], 'requestAccounting': 'unverified'}
            path = root / 'web/data/periods/2026Q2/audit.json'
            atomic_json(path, {'schemaVersion': 1, 'quarter': '2026Q2',
                'phase': {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'},
                'continuationHold': hold, 'batchInputErrors': [{'batchId': identifier, 'quarter': '2026Q2',
                    'errorCode': 'BATCH_ARTIFACT_MISSING'} for identifier in ['10', '13']]})
            original = Path.cwd(); before = path.read_bytes()
            try:
                os.chdir(root)
                with patch('campaign.refresh_directory') as refresh, patch('campaign.refresh_registries') as locators, patch('campaign.subprocess.run') as run:
                    for _ in range(2):
                        result = plan('2026Q2', '2026-11-20', shards=1, batch_size=2)
                        self.assertEqual(result['status'], 'held')
                        self.assertEqual(result['batchCount'], 0)
                        self.assertEqual(read_json('.cache/plan/matrix.json'), [])
                        outcome = publish()
                        self.assertFalse(outcome['continueRun'])
                        self.assertEqual(outcome['continuationHolds']['2026Q2'], hold)
                    refresh.assert_not_called(); locators.assert_not_called(); run.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
            finally:
                os.chdir(original)

    def test_malformed_persistent_hold_never_becomes_unheld(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'periods/2026Q2/audit.json'
            for bad in (False, {}, {'reason': 'expired'}):
                atomic_json(path, {'schemaVersion': 1, 'continuationHold': bad})
                with self.assertRaisesRegex(ValueError, 'INVALID_CONTINUATION_HOLD'):
                    deployed_continuation_state(root, '2026Q2')

    def test_quarters_have_independent_exact_baselines_and_fresh_cutoff(self):
        base = {'funds': [{'code': '000001'}], 'asOf': '2026-09-29', 'maxWorkers': 2}
        q2 = quarter_config(base, '2026Q2', '2026-10-26')
        q3 = quarter_config(base, '2026Q3', '2026-10-26')
        self.assertEqual((q2['targetPeriodEnd'], q2['baselinePeriodEnd']), ('2026-06-30', '2026-03-31'))
        self.assertEqual((q3['targetPeriodEnd'], q3['baselinePeriodEnd']), ('2026-09-30', '2026-06-30'))
        self.assertEqual(q3['asOf'], '2026-10-26')
        self.assertTrue(q2['requireReportQuality'])
        self.assertEqual(q2['collectorVersion'], 5)
        self.assertIsNone(q2['retryPolicy']['dailyUntil'])
        self.assertEqual(q3['retryPolicy']['dailyUntil'], '2026-11-30')
        self.assertEqual(q3['retryPolicy']['maxScheduledCycles'], 4)
        self.assertEqual(base['asOf'], '2026-09-29')
        self.assertEqual(selected_periods('both'), ['2026Q2', '2026Q3'])
        with self.assertRaises(ValueError): selected_periods('../private')

    def test_only_legacy_q2_is_seeded_once_without_refreshing_dates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b'{"targetPeriodEnd":"2026-06-30","retrievedAt":"2026-10-13T01:00:00Z"}\n'
            (root / 'latest.json').write_bytes(raw)
            (root / 'companies').mkdir()
            (root / 'companies/co1.json').write_text('{"funds":[]}')
            q2 = seed_quarter(root, '2026Q2')
            self.assertEqual((q2 / 'latest.json').read_bytes(), raw)
            self.assertTrue((q2 / 'companies/co1.json').exists())
            (root / 'latest.json').write_text('{}')
            seed_quarter(root, '2026Q2')
            self.assertEqual((q2 / 'latest.json').read_bytes(), raw)
            q3 = seed_quarter(root, '2026Q3')
            self.assertFalse((q3 / 'latest.json').exists())

    def test_site_compression_is_lossless_and_phase_paths_do_not_cross(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'web'
            period = root / 'data/periods/2026Q3'
            (period / 'companies').mkdir(parents=True)
            raw = b'{"funds":[{"code":"000001"}]}'
            (period / 'companies/co1.json').write_bytes(raw)
            (period / 'latest.json').write_text(json.dumps({'companyFiles': [{'path': './data/companies/co1.json'}]}))
            output = Path(temp) / 'site'
            result = site.prepare(root, output)
            self.assertEqual(result['files'], 2)
            zipped = output / 'data/periods/2026Q3/companies/co1.json.gz'
            self.assertEqual(gzip.decompress(zipped.read_bytes()), raw)
            self.assertEqual((period / 'companies/co1.json').read_bytes(), raw)
            body = json.loads((output / 'data/periods/2026Q3/latest.json').read_text())
            self.assertEqual(body['companyFiles'][0]['path'], './data/companies/co1.json.gz')
            with self.assertRaises(ValueError): site.prepare(root, output)

    def test_analysis_parts_are_compressed_and_references_remain_in_their_quarter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'web'; period = root / 'data/periods/2026Q2'
            (period / 'analysis').mkdir(parents=True)
            paths = ['./analysis/details-0000.json', './analysis/details-portfolios-0000.json']
            for value in paths: (period / value[2:]).write_text('{"public":"observed"}')
            manifest = {'stocks': [{'details': [paths[0]]}], 'portfolioFiles': [{'path': paths[1]}]}
            (period / 'analysis.json').write_text(json.dumps(manifest))
            output = Path(temp) / 'site'; site.prepare(root, output)
            result = json.loads((output / 'data/periods/2026Q2/analysis.json').read_text())
            self.assertEqual(result['stocks'][0]['details'], [paths[0] + '.gz'])
            self.assertEqual(result['portfolioFiles'][0]['path'], paths[1] + '.gz')
            self.assertEqual(gzip.decompress((output / 'data/periods/2026Q2/analysis/details-0000.json.gz').read_bytes()), b'{"public":"observed"}')
            manifest['stocks'][0]['details'] = ['./analysis/../../private.json']
            (period / 'analysis.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'INVALID_ANALYSIS_PATH'): site.prepare(root, Path(temp) / 'rejected')

    def test_site_rebuilds_missing_analysis_only_in_actions_and_keeps_current_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'web'; period = root / 'data/periods/2026Q3'; period.mkdir(parents=True)
            (root / 'data/periods.json').write_text(json.dumps({'periods': [{'id': '2026Q3'}]}))
            latest = {'retrievedAt': '2026-10-26T00:00:00Z', 'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30'}
            (period / 'latest.json').write_text(json.dumps(latest))
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
                with self.assertRaisesRegex(ValueError, 'ANALYSIS_REQUIRES_ACTIONS'): site.ensure_analyses(root)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(site.subprocess, 'run') as run:
                self.assertEqual(site.ensure_analyses(root), 1)
                self.assertEqual(run.call_args.args[0][-1], str(period / 'analysis.json'))
                (period / 'analysis.json').write_text(json.dumps({**latest, 'kind': 'quarter-analysis'}))
                run.reset_mock(); self.assertEqual(site.ensure_analyses(root), 1); run.assert_called_once()
                (period / 'analysis.json').write_text(json.dumps({**latest, 'kind': 'quarter-analysis', 'analysisVersion': 7, 'priceUpdatedAt': None}))
                run.reset_mock(); self.assertEqual(site.ensure_analyses(root), 1); run.assert_called_once()
                (period / 'analysis.json').write_text(json.dumps({**latest, 'kind': 'quarter-analysis', 'analysisVersion': 8, 'priceReferenceVersion': 1, 'priceUpdatedAt': None}))
                run.reset_mock(); self.assertEqual(site.ensure_analyses(root), 0); run.assert_not_called()
                (period / 'latest.json').write_text(json.dumps({**latest, 'priceUpdatedAt': '2026-10-26T12:00:00Z'}))
                run.reset_mock(); self.assertEqual(site.ensure_analyses(root), 1); run.assert_called_once()

    def test_staging_rejects_missing_part_and_excessive_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'web'; root.mkdir()
            (root / 'latest.json').write_text(json.dumps({'companyFiles': [{'path': './data/companies/absent.json'}]}))
            with self.assertRaisesRegex(ValueError, 'MISSING_COMPANY_PART'):
                site.prepare(root, Path(temp) / 'bad')
            (root / 'latest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'SITE_SIZE_EXCEEDED'):
                site.prepare(root, Path(temp) / 'large', max_bytes=1)


if __name__ == '__main__': unittest.main()
