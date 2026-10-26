"""Small frozen plan/publish candidate fixtures; remote source is never used."""
import copy
from pathlib import Path
import unittest

import test_frozen_catalog_campaign as base
from test_report_locators import result, NOW
import campaign
from report_locators import empty_registry, candidates_from_result, expand_config
from traversal import atomic_json, read_json


class LocatorCampaignTests(unittest.TestCase):
    setUp = base.FrozenCampaignTests.setUp
    tearDown = base.FrozenCampaignTests.tearDown

    def seed(self):
        config = read_json('config/universe.json'); config['reportLocatorEnabled'] = True
        atomic_json('config/universe.json', config)
        registry = empty_registry('2026Q2')
        registry.update(records=candidates_from_result(result(codes='000001,018775'), now=NOW),
                        lastAttemptAt=NOW, asOf='2026-10-26', refreshStatus='current', windowEnumerationComplete=True)
        atomic_json('web/data/periods/2026Q2/report-locators.json', registry)
        return registry

    def test_frozen_source_registry_reaches_only_known_code_and_same_bytes_publish(self):
        registry = self.seed(); root_bytes = Path('web/data/catalog.json').read_bytes()
        campaign.plan('2026Q2', '2026-10-26', shards=1, batch_size=2, refresh=False, historical_overlays={})
        selection = read_json('.cache/campaign/selection.json')
        binding = selection['reportLocators']['2026Q2']
        frozen = Path('.cache/campaign') / binding['path']; frozen_bytes = frozen.read_bytes()
        config = read_json('.cache/campaign/2026Q2.json')
        self.assertNotIn('reportLocatorCandidates', config)
        self.assertEqual(config['reportLocatorUniverseCodes'], ['000001'])
        self.assertEqual(set(expand_config(config)['reportLocatorCandidates']), {'000001'})
        batch = read_json('.cache/plan/batch-0.json')
        self.assertNotIn('reportLocatorCandidates', batch)
        self.assertNotIn('reportLocatorUniverseCodes', batch)
        row = next(v for v in batch['funds'] if v['code'] == '000001')
        self.assertEqual(row['reportLocator']['candidates'], registry['records'])
        self.assertNotIn('018775', {v['code'] for v in batch['funds']})
        # A later local cache is not the source of a queued campaign's publish.
        changed = copy.deepcopy(registry); changed['records'] = []
        atomic_json('web/data/periods/2026Q2/report-locators.json', changed)
        campaign.publish()
        self.assertEqual(Path('web/data/periods/2026Q2/report-locators.json').read_bytes(), frozen_bytes)
        self.assertEqual(Path('web/data/catalog.json').read_bytes(), root_bytes)
        self.assertEqual(read_json('web/data/periods/2026Q2/catalog.json')['fundCount'], 1)

    def test_tampered_or_missing_frozen_registry_stops_before_replacing_published_cache(self):
        self.seed()
        campaign.plan('2026Q2', '2026-10-26', shards=1, batch_size=2, refresh=False, historical_overlays={})
        published = Path('web/data/periods/2026Q2/report-locators.json'); before = published.read_bytes()
        frozen = Path('.cache/campaign/locators/2026Q2.json'); original = frozen.read_bytes()
        frozen.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'INVALID_LOCATOR_LOCK'): campaign.publish()
        self.assertEqual(published.read_bytes(), before)
        frozen.unlink()
        with self.assertRaisesRegex(ValueError, 'INVALID_LOCATOR_LOCK'): campaign.publish()
        self.assertEqual(published.read_bytes(), before)

    def test_source_registry_is_cached_but_production_lane_stays_off_until_explicit_enable(self):
        self.seed()
        config = read_json('config/universe.json'); config.pop('reportLocatorEnabled')
        atomic_json('config/universe.json', config)
        campaign.plan('2026Q2', '2026-10-26', shards=1, batch_size=2, refresh=False, historical_overlays={})
        scoped = read_json('.cache/campaign/2026Q2.json')
        self.assertFalse(scoped['reportLocatorEnabled'])
        self.assertEqual(expand_config(scoped)['reportLocatorCandidates'], {})
        self.assertEqual(len(read_json('.cache/campaign/locators/2026Q2.json')['records']), 1)
        batch = read_json('.cache/plan/batch-0.json')
        self.assertTrue(all('reportLocator' not in fund for fund in batch['funds']))


if __name__ == '__main__': unittest.main()
