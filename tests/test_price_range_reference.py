"""Synthetic cross-runtime endpoints classification and actual hydration gates."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import price_range_reference as reference
import price_consistency as raw
import traversal


class PriceRangeReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(subprocess.check_output(['node', str(ROOT / 'tests/price-reference-fixture.js')], text=True))
        cls.now = dt.datetime.fromisoformat(cls.data['now'].replace('Z', '+00:00'))

    def fixture(self):
        value = copy.deepcopy(self.data)
        price = {**value['prices'][1], 'globalPriceConflict': value['witness'], 'rangeReference': value['reference']}
        fund = {'code': '000001', 'status': 'ok', 'retrievedAt': value['fetchedAt'], 'companyId': 'co1',
                'selectedReport': {'periodEnd': value['phase']['targetPeriodEnd'], 'publishedAt': '2026-07-20',
                                  'sourceUrl': 'https://example.org/report.pdf', 'holdings': [{'market': 'CN', 'stockCode': '600001'}]},
                'rows': [{**value['identity'], 'priceRange': price}]}
        return value, price, fund

    def test_js_python_canonical_reference_and_effective_quote_match(self):
        data, price, _ = self.fixture()
        self.assertEqual(reference.build_reference(data['facts'], data['identity'], self.now), data['reference'])
        self.assertEqual(reference.normalize_reference(data['reference'], data['identity'], self.now), data['reference'])
        original = copy.deepcopy(price)
        self.assertEqual(reference.effective_reference(price, data['identity'], self.now)['low'], 73.46)
        self.assertEqual(price, original)
        later = copy.deepcopy(data['facts'][0]); later['fetchedAt'] = later['fetchedAt'].replace('000001', '000002')
        left = reference.build_reference([*data['facts'], later], data['identity'], self.now)
        right = reference.build_reference([later, *data['facts']], data['identity'], self.now)
        self.assertEqual(left, right)
        self.assertEqual(next(f for f in left['facts'] if f['factId'] == left['referenceFactId'])['fetchedAt'], later['fetchedAt'])

    def test_all_complete_facts_required_true_difference_and_uncovered_witness_stay_conflicted(self):
        data, price, fund = self.fixture()
        third = copy.deepcopy(data['facts'][0]); third['price']['low'] += 1e-10
        self.assertIsNone(reference.build_reference([*data['facts'], third], data['identity'], self.now))
        price['globalPriceConflict'] = raw.conflict_for([data['facts'][0], third], data['identity'], self.now)
        self.assertIsNone(reference.effective_reference(price, data['identity'], self.now))
        self.assertEqual(traversal.price_coverage(fund, self.now)['priceConflicted'], 1)
        unknown = copy.deepcopy(data['facts'][1]); unknown['sourceUrl'] = 'https://example.org/quote'; unknown['sourceUrls'] = [unknown['sourceUrl']]
        self.assertIsNone(reference.build_reference([data['facts'][0], unknown], data['identity'], self.now))
        bad = copy.deepcopy(data['facts'][1]); bad['price']['low'] += 1e-10
        self.assertIsNone(reference.build_reference([data['facts'][0], bad], data['identity'], self.now))

    def test_delayed_receipts_cannot_clear_sticky_conflict_current_hydration_can_without_attempts(self):
        data, price, fund = self.fixture()
        old_fund = copy.deepcopy(fund); del old_fund['rows'][0]['priceRange']['rangeReference']
        old = traversal.price_coverage(old_fund, self.now)
        coverage = traversal.price_coverage(fund, self.now)
        self.assertEqual(coverage['priceComplete'], 1); self.assertEqual(coverage['priceConflicted'], 0)
        self.assertTrue(traversal.valid_price_coverage(coverage))
        state = {'code': fund['code'], 'status': 'complete', 'companyCode': 'co1',
                 'enrichment': {**old, 'attempts': 3, 'totalAttempts': 9, 'lastAttemptAt': data['fetchedAt'],
                                'status': 'conflict', 'retry': {'nextRetryAt': '2026-10-30T00:00:00Z'}},
                 'quality': {'fields': {key: 'partial' if key == 'prices' else 'complete' for key in traversal.QUALITY_FIELDS},
                             'status': 'conflict', 'priceConflictOnly': True, 'attempts': 3, 'totalAttempts': 12,
                             'lastAttemptAt': data['fetchedAt'], 'missingFields': ['prices']}}
        original = copy.deepcopy(state)
        traversal.apply_coverage(state, coverage)
        self.assertEqual(state['enrichment']['priceConflicted'], 1)
        traversal.apply_coverage(state, coverage, trusted_current=True)
        self.assertEqual(state['enrichment']['priceConflicted'], 0); self.assertEqual(state['enrichment']['priceComplete'], 1)
        self.assertEqual(state['quality']['fields']['prices'], 'complete'); self.assertEqual(state['quality']['status'], 'complete')
        for lane in ('enrichment', 'quality'):
            for key in ('attempts', 'totalAttempts', 'lastAttemptAt'):
                self.assertEqual(state[lane][key], original[lane][key])
        self.assertEqual(state['enrichment']['retry'], original['enrichment']['retry'])
        malformed = copy.deepcopy(coverage); malformed['referenceResolutions'][0]['reference']['factSetDigest'] = '0' * 64
        self.assertFalse(traversal.valid_price_coverage(malformed))
        bare = copy.deepcopy(coverage); del bare['referenceResolutions']
        state = copy.deepcopy(original); traversal.apply_coverage(state, bare, trusted_current=True)
        self.assertEqual(state['enrichment']['priceConflicted'], 1)


if __name__ == '__main__': unittest.main()
