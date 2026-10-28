"""Small Actions-shaped plan/publish fixtures; no network or historical parser."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import campaign
from traversal import atomic_json, read_json
from test_quarter_catalog import current, overlay


class FrozenCampaignTests(unittest.TestCase):
    def setUp(self):
        self.binding = patch.dict(os.environ, {'GITHUB_RUN_ID': '123456', 'GITHUB_RUN_ATTEMPT': '1',
                                              'GITHUB_SHA': 'a' * 40})
        self.binding.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ('scripts', 'lib', 'config'):
            shutil.copytree(ROOT / directory, self.root / directory, ignore=shutil.ignore_patterns('__pycache__'))
        (self.root / 'web').mkdir()
        for file in (ROOT / 'web').glob('*.js'):
            shutil.copy2(file, self.root / 'web' / file.name)
        atomic_json(self.root / 'web/data/catalog.json', current())
        self.original = Path.cwd()
        os.chdir(self.root)
        real_run = campaign.subprocess.run
        def local_fixture(command, **kwargs):
            kwargs.setdefault('capture_output', True)
            if command[:2] == ['node', 'scripts/analyze-data.js']:
                kwargs['env'] = {**os.environ, 'GITHUB_ACTIONS': 'true'}
            return real_run(command, **kwargs)
        self.runtime = patch('campaign.subprocess.run', side_effect=local_fixture)
        self.runtime.start()

    def tearDown(self):
        self.runtime.stop()
        self.binding.stop()
        os.chdir(self.original)
        self.temporary.cleanup()

    def plan(self, quarter='2026Q2', additions=None):
        return campaign.plan(quarter, '2026-10-26', shards=1, batch_size=2, refresh=False,
                             historical_overlays={'2026Q2': additions} if additions is not None else None)

    def test_scoped_overlay_reaches_plan_and_publish_ignores_a_changed_root_directory(self):
        untouched = Path('web/data/periods/2026Q3/catalog.json')
        untouched.parent.mkdir(parents=True); untouched.write_text('{"untouched":true}\n')
        original = Path('web/data/catalog.json').read_bytes()
        self.plan(additions=overlay())
        self.assertEqual(Path('web/data/catalog.json').read_bytes(), original)
        selection = read_json('.cache/campaign/selection.json')
        config = read_json('.cache/campaign/2026Q2.json')
        frozen = Path('.cache/campaign') / selection['catalogs']['2026Q2']['path']
        self.assertEqual(hashlib.sha256(frozen.read_bytes()).hexdigest(), config['catalogDigest'])
        batch = read_json('.cache/plan/batch-0.json')
        self.assertEqual(batch['catalogDigest'], config['catalogDigest'])
        self.assertEqual({row['code'] for row in batch['funds']}, {'000001', '000003'})
        self.assertIsNone(next(row for row in batch['funds'] if row['code'] == '000003')['companyCode'])
        self.assertTrue(batch['requireHistoricalUniverse'])
        changed = current(); changed['funds'][0]['code'] = '000009'
        atomic_json('web/data/catalog.json', changed)
        changed_bytes = Path('web/data/catalog.json').read_bytes()
        atomic_json('raw/observed.json', {'asOf': '2026-10-26', 'retrievedAt': '2026-10-26T02:00:00Z', 'errors': [], 'barsByStock': {},
            'funds': [{'code': '000003', 'name': '历史产品000003', 'managers': [], 'reports': [], 'noEquitiesEvidence': {
                'confirmed': True, 'kind': 'quarterly', 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20',
                'sourceUrl': 'https://example.org/quarter.pdf', 'page': 8, 'text': '本基金本报告期末未持有股票。'}}]})
        campaign.subprocess.run(['node', 'scripts/build-data.js', '--input', 'raw/observed.json',
            '--output', '.cache/parts/part-0/part.json', '--config', '.cache/plan/batch-0.json'], check=True)
        campaign.publish()
        self.assertEqual(Path('web/data/periods/2026Q2/catalog.json').read_bytes(), frozen.read_bytes())
        self.assertEqual(Path('web/data/catalog.json').read_bytes(), changed_bytes)
        self.assertEqual(untouched.read_text(), '{"untouched":true}\n')
        progress = read_json('web/data/periods/2026Q2/progress.json')
        self.assertEqual({row['code'] for row in progress['funds']}, {'000001', '000003'})
        self.assertEqual(progress['unmappedFunds'], 1)
        self.assertNotEqual(progress['completionStatus'], 'complete')
        self.assertFalse(progress['quarterUniverse']['historicalUniverseVerified'])
        manifest = read_json('web/data/periods/2026Q2/latest.json')
        unknown_page = next(row for row in manifest['companyFiles'] if row['companyCode'] == 'unknown')
        unknown = read_json(Path('web/data/periods/2026Q2/companies') / Path(unknown_page['path']).name)
        self.assertEqual(unknown['funds'][0]['code'], '000003')
        self.assertEqual(unknown['funds'][0]['companyMapping'], 'unmatched-catalog')

    def test_each_quarter_uses_its_own_lock_and_q2_overlay_never_enters_q3(self):
        self.plan('both', overlay())
        selection = read_json('.cache/campaign/selection.json')
        q2 = read_json('.cache/campaign/catalogs/2026Q2.json')
        q3 = read_json('.cache/campaign/catalogs/2026Q3.json')
        self.assertEqual([row['code'] for row in q2['funds']], ['000001', '000003'])
        self.assertEqual([row['code'] for row in q3['funds']], ['000001'])
        self.assertNotEqual(selection['catalogs']['2026Q2']['sha256'], selection['catalogs']['2026Q3']['sha256'])
        before = Path('web/data/periods/2026Q2/catalog.json').read_bytes()
        with Path('.cache/campaign/catalogs/2026Q3.json').open('ab') as handle: handle.write(b' ')
        with patch('campaign.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'CATALOG_DIGEST_MISMATCH'): campaign.publish()
            run.assert_not_called()
        self.assertEqual(Path('web/data/periods/2026Q2/catalog.json').read_bytes(), before)
        self.assertFalse(Path('web/data/periods/2026Q2/progress.json').exists())

    def test_modern_missing_or_mismatched_locks_never_fall_back_to_root(self):
        for failure in ('missing-file', 'config-digest', 'batch-digest', 'missing-selection-binding'):
            with self.subTest(failure=failure):
                self.plan()
                if failure == 'missing-file': Path('.cache/campaign/catalogs/2026Q2.json').unlink()
                elif failure == 'missing-selection-binding':
                    selection = read_json('.cache/campaign/selection.json'); selection.pop('catalogs')
                    atomic_json('.cache/campaign/selection.json', selection)
                else:
                    file = '.cache/campaign/2026Q2.json' if failure == 'config-digest' else '.cache/plan/batch-0.json'
                    config = read_json(file); config['catalogDigest'] = 'b' * 64; atomic_json(file, config)
                with patch('campaign.subprocess.run') as run:
                    with self.assertRaises(ValueError): campaign.publish()
                    run.assert_not_called()

    def test_unlocked_legacy_plan_recovers_once_with_explicitly_unverified_history(self):
        selection = {'quarters': ['2026Q2'], 'selection': '2026Q2', 'asOf': '2026-10-26'}
        config = campaign.quarter_config({}, '2026Q2', '2026-10-26')
        config.pop('requireHistoricalUniverse')
        batch = {**config, 'funds': [{'code': '000001', 'companyCode': 'co1'}]}
        matrix = [{'id': '0', 'config': 'batch-0.json', 'quarter': '2026Q2'}]
        atomic_json('.cache/campaign/selection.json', selection)
        atomic_json('.cache/campaign/2026Q2.json', config)
        atomic_json('.cache/plan/batch-0.json', batch)
        atomic_json('.cache/plan/matrix.json', matrix)
        original = Path('web/data/catalog.json').read_bytes()
        paths = campaign.frozen_catalogs_for_publish(selection, matrix, Path('web/data'))
        recovered = read_json(paths['2026Q2'])
        self.assertFalse(recovered['quarterUniverse']['historicalUniverseVerified'])
        self.assertTrue(recovered['quarterUniverse']['legacyPlanRecovered'])
        self.assertTrue(read_json('.cache/campaign/2026Q2.json')['requireHistoricalUniverse'])
        self.assertEqual(read_json('.cache/plan/batch-0.json')['catalogDigest'], selection['catalogs']['2026Q2']['sha256'])
        self.assertEqual(Path('web/data/catalog.json').read_bytes(), original)
        self.assertEqual(campaign.frozen_catalogs_for_publish(selection, matrix, Path('web/data')), paths)

    def test_recursive_artifact_scan_catches_nested_credentials_and_uses_catalog_limit(self):
        self.plan()
        file = Path('.cache/campaign/catalogs/2026Q2.json')
        with patch('campaign.content_limit', side_effect=lambda name: 50 if name.endswith('/catalog.json') else 25_000_000) as policy:
            with self.assertRaisesRegex(ValueError, 'PLAN_PUBLICATION_REJECTED'): campaign.scan_campaign_files()
            policy.assert_called_with('web/data/periods/2026Q2/catalog.json')
        raw = read_json(file); raw['diagnostic'] = 'ghp_' + 'a' * 30; atomic_json(file, raw)
        with self.assertRaisesRegex(ValueError, 'PLAN_PUBLICATION_REJECTED'): campaign.scan_campaign_files()


if __name__ == '__main__': unittest.main()
