"""Single-pass hydration keeps source proofs and clocks identical."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import traversal


class TraversalCoverageReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code = "const f=require('./tests/price-reference-fixture');console.log(JSON.stringify({data:f.fixture(),catalog:f.catalog,config:f.config,fund:f.snapshot('000001','yahoo').funds[0]}));"
        cls.fixture = json.loads(subprocess.check_output(['node', '-e', code], cwd=ROOT, text=True))

    def sample(self, mode):
        value = copy.deepcopy(self.fixture)
        data, catalog, config, fund = (value[key] for key in ('data', 'catalog', 'config', 'fund'))
        now = traversal.utc_now(data['now'])
        config.update(requireReportQuality=True)
        fund.update(companyId='co1', shareClassCodes=['000001', '000002'],
                    sourcePortfolioCode='000001', portfolioSourceUrl=fund['selectedReport']['sourceUrl'])
        price = {**data['prices'][1], 'globalPriceConflict': data['witness']}
        if mode in ('resolved', 'tampered'):
            price['rangeReference'] = data['reference']
        if mode == 'tampered': price['rangeReference']['factSetDigest'] = '0' * 64
        if mode == 'missing': price = {}
        fund['rows'][0]['priceRange'] = price
        previous = traversal.merge_progress(catalog, None, config, now=now)
        for row in previous['funds']:
            row.update(status='complete', attempted=True, lastAttemptAt=data['fetchedAt'])
        return catalog, previous, config, fund, now

    def test_one_measurement_for_report_and_enrichment_matches_duplicate_evaluation(self):
        original_measure = traversal._report_quality_with_coverage

        def duplicate_measure(fund, phase, now):
            quality, _ = original_measure(fund, phase, now)
            return quality, traversal.price_coverage(fund, now) if quality is not None else None

        for mode in ('resolved', 'unresolved', 'tampered', 'missing'):
            with self.subTest(mode=mode):
                catalog, previous, config, fund, now = self.sample(mode)
                original = copy.deepcopy((previous, fund))
                with patch.object(traversal, '_report_quality_with_coverage', side_effect=duplicate_measure):
                    expected = traversal.merge_progress(catalog, previous, config, now=now, retained=iter([fund]))
                with patch.object(traversal, 'price_coverage', wraps=traversal.price_coverage) as measured:
                    actual = traversal.merge_progress(catalog, previous, config, now=now, retained=iter([fund]))
                    self.assertEqual(measured.call_count, 1)
                    self.assertIs(measured.call_args.args[1], now)
                self.assertEqual(actual, expected)
                self.assertEqual((previous, fund), original)
                # Report-proven share aliases reuse the same measured holding set.
                self.assertEqual(actual['funds'][0]['enrichment'], actual['funds'][1]['enrichment'])

    def test_invalid_source_keeps_early_exit_without_measuring_prices(self):
        catalog, previous, config, fund, now = self.sample('resolved')
        wrong_period = copy.deepcopy(fund)
        wrong_period['selectedReport']['periodEnd'] = '2026-03-31'
        wrong_code = copy.deepcopy(fund)
        wrong_code['selectedReport']['fundCode'] = '000002'
        future = copy.deepcopy(fund)
        future['retrievedAt'] = '2026-11-01T00:00:00Z'
        malformed = copy.deepcopy(fund)
        malformed['selectedReport'] = []
        with patch.object(traversal, 'price_coverage', wraps=traversal.price_coverage) as measured:
            for value in (None, malformed, wrong_period, wrong_code, future):
                self.assertIsNone(traversal.report_quality(value, traversal.phase_for(config), now))
            traversal.merge_progress(catalog, previous, config, now=now,
                                     retained=[None, malformed, wrong_period, wrong_code, future])
            measured.assert_not_called()

    def test_a_later_changed_observation_revalidates_the_same_mutable_object(self):
        catalog, previous, config, fund, now = self.sample('resolved')
        first = traversal.merge_progress(catalog, previous, config, now=now, retained=[fund])
        self.assertEqual(first['funds'][0]['enrichment']['priceConflicted'], 0)
        fund['rows'][0]['priceRange'].pop('rangeReference')
        with patch.object(traversal, 'price_coverage', wraps=traversal.price_coverage) as measured:
            second = traversal.merge_progress(catalog, first, config, now=now, retained=[fund])
            self.assertEqual(measured.call_count, 1)
        self.assertEqual(second['funds'][0]['enrichment']['priceConflicted'], 1)
        self.assertEqual(second['funds'][0]['quality']['fields']['prices'], 'partial')


if __name__ == '__main__': unittest.main()
