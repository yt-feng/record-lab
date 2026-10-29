"""Synthetic single-clock equivalence for the three price coverage views."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import price_range_reference as reference
import price_consistency as raw
import traversal


def previous_views(price, identity, now):
    unresolved = reference.unresolved_conflict(price, identity, now)
    resolution = reference.resolution_for(price, identity, now)
    effective = reference.effective_price_range(price, identity, now)
    return effective, resolution, unresolved


class PriceCoverageResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code = "const f=require('./tests/price-reference-fixture');console.log(JSON.stringify({data:f.fixture(),partial:f.price('eastmoney',70,100.1,true),fund:f.snapshot('000001','yahoo').funds[0]}));"
        cls.fixture = json.loads(subprocess.check_output(['node', '-e', code], cwd=ROOT, text=True))
        cls.now = dt.datetime.fromisoformat(cls.fixture['data']['now'].replace('Z', '+00:00'))

    def sample(self, mode='resolved'):
        data = copy.deepcopy(self.fixture['data'])
        price = {**data['prices'][1], 'rangeReference': data['reference'], 'globalPriceConflict': data['witness']}
        if mode == 'no-witness': price.pop('globalPriceConflict')
        if mode == 'no-reference': price.pop('rangeReference')
        if mode == 'tampered-reference': price['rangeReference']['factSetDigest'] = '0' * 64
        if mode == 'tampered-witness': price['globalPriceConflict']['witnessDigest'] = '0' * 64
        if mode == 'null-witness': price['globalPriceConflict'] = None
        if mode == 'material-conflict':
            third = copy.deepcopy(data['facts'][0]); third['price']['low'] += 0.1
            price['globalPriceConflict'] = raw.conflict_for([data['facts'][0], third], data['identity'], self.now)
        if mode == 'partial-outside':
            price = {**copy.deepcopy(self.fixture['partial']), 'rangeReference': data['reference']}
        if mode == 'missing-with-proof': price['status'] = 'missing'
        if mode == 'missing': price = {}
        if mode == 'none': price = None
        if mode == 'list': price = []
        return price, data['identity']

    def test_full_views_equal_existing_apis_for_valid_missing_and_changed_evidence(self):
        for mode in ('resolved', 'no-witness', 'no-reference', 'tampered-reference', 'tampered-witness',
                     'null-witness', 'material-conflict', 'partial-outside', 'missing-with-proof', 'missing', 'none', 'list'):
            price, identity = self.sample(mode)
            identities = [identity, None, {**identity, 'stockCode': '600002'}, {**identity, 'currency': 'USD'}, {}, []]
            for scoped in identities:
                with self.subTest(mode=mode, identity=scoped):
                    before = copy.deepcopy((price, scoped))
                    expected = previous_views(price, scoped, self.now)
                    actual = reference.coverage_resolution(price, scoped, self.now)
                    self.assertEqual(actual, expected)
                    self.assertEqual((price, scoped), before)
                    if expected[0] is price: self.assertIs(actual[0], price)

    def test_explicit_clock_rebuilds_reference_and_normalizes_raw_witness_once(self):
        for mode in ('resolved', 'tampered-reference', 'tampered-witness', 'material-conflict', 'missing-with-proof'):
            with self.subTest(mode=mode):
                price, identity = self.sample(mode)
                with patch.object(reference, '_effective_reference', wraps=reference._effective_reference) as evaluate, \
                     patch.object(raw, 'normalize_price_conflict', wraps=raw.normalize_price_conflict) as normalize:
                    expected = previous_views(price, identity, self.now)
                    old_evaluations, old_normalizations = evaluate.call_count, normalize.call_count
                with patch.object(reference, '_effective_reference', wraps=reference._effective_reference) as evaluate, \
                     patch.object(raw, 'normalize_price_conflict', wraps=raw.normalize_price_conflict) as normalize:
                    actual = reference.coverage_resolution(price, identity, self.now)
                    self.assertEqual(evaluate.call_count, 1)
                    self.assertIs(evaluate.call_args.args[2], self.now)
                    self.assertEqual(normalize.call_count, 1)
                    self.assertIs(normalize.call_args.args[-1], self.now)
                self.assertEqual(actual, expected)
                self.assertGreater(old_evaluations, 1)
                self.assertGreaterEqual(old_normalizations, 1)
                if mode in ('resolved', 'missing-with-proof'):
                    self.assertEqual(old_evaluations, 3)
                    self.assertEqual(old_normalizations, 4)

    def test_implicit_clock_preserves_separate_apis_and_original_order(self):
        price, identity = self.sample()
        for clock in (None, False, 0):
            with self.subTest(clock=clock):
                calls = []
                with patch.object(reference, 'unresolved_conflict', side_effect=lambda *args: calls.append(('unresolved', args)) or True), \
                     patch.object(reference, 'resolution_for', side_effect=lambda *args: calls.append(('resolution', args)) or {'result': 2}), \
                     patch.object(reference, 'effective_price_range', side_effect=lambda *args: calls.append(('effective', args)) or {'result': 3}):
                    actual = reference.coverage_resolution(price, identity, clock)
                self.assertEqual(actual, ({'result': 3}, {'result': 2}, True))
                self.assertEqual(calls, [(name, (price, identity, clock)) for name in ('unresolved', 'resolution', 'effective')])

    def test_clock_boundary_is_exact_and_later_observations_are_revalidated(self):
        price, identity = self.sample()
        observed = dt.datetime.fromisoformat(self.fixture['data']['fetchedAt'].replace('Z', '+00:00'))
        boundary = observed - dt.timedelta(minutes=5)
        for now in (boundary - dt.timedelta(microseconds=1), boundary, boundary + dt.timedelta(microseconds=1), self.now):
            with self.subTest(now=now):
                self.assertEqual(reference.coverage_resolution(price, identity, now), previous_views(price, identity, now))
        self.assertIsNone(reference.coverage_resolution(price, identity, boundary - dt.timedelta(microseconds=1))[0])
        self.assertIsNotNone(reference.coverage_resolution(price, identity, boundary)[0])
        price['rangeReference']['factSetDigest'] = '0' * 64
        changed = reference.coverage_resolution(price, identity, self.now)
        self.assertEqual(changed, previous_views(price, identity, self.now))
        self.assertIsNone(changed[0]); self.assertIsNone(changed[1]); self.assertTrue(changed[2])

    def test_returned_proof_mutation_does_not_change_input_or_other_view(self):
        price, identity = self.sample()
        before = copy.deepcopy(price)
        effective, resolution, unresolved = reference.coverage_resolution(price, identity, self.now)
        self.assertFalse(unresolved)
        resolution['reference']['facts'][0]['price']['low'] = 1
        resolution['rawWitness']['facts'][0]['price']['low'] = 2
        self.assertEqual(price, before)
        self.assertEqual(effective['rangeReference'], before['rangeReference'])
        self.assertEqual(reference.coverage_resolution(price, identity, self.now), previous_views(price, identity, self.now))

    def test_traversal_integration_keeps_coverage_and_separate_completeness_clock(self):
        for mode in ('resolved', 'tampered-reference', 'tampered-witness', 'no-reference', 'missing'):
            with self.subTest(mode=mode):
                price, _ = self.sample(mode)
                fund = copy.deepcopy(self.fixture['fund'])
                fund['rows'][0]['priceRange'] = price
                before = copy.deepcopy(fund)
                with patch.object(traversal, 'coverage_resolution', side_effect=previous_views), \
                     patch.object(reference, 'build_reference', wraps=reference.build_reference) as rebuild:
                    expected = traversal.price_coverage(fund, self.now)
                    old_rebuilds = rebuild.call_count
                with patch.object(reference, 'build_reference', wraps=reference.build_reference) as rebuild, \
                     patch.object(traversal, 'valid_usable_complete_price', wraps=traversal.valid_usable_complete_price) as complete:
                    actual = traversal.price_coverage(fund, self.now)
                    if mode == 'resolved':
                        self.assertEqual(old_rebuilds, 4)
                        self.assertEqual(rebuild.call_count, 2)
                        self.assertEqual(complete.call_count, 1)
                        self.assertEqual(len(complete.call_args.args), 5)
                        self.assertEqual(complete.call_args.kwargs, {})
                self.assertEqual(actual, expected)
                self.assertEqual(fund, before)


if __name__ == '__main__': unittest.main()
