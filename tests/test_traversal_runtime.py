"""Ephemeral task metadata must not inflate persisted coverage checkpoints."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import traversal
from test_report_locators import locator
from test_strategy_claim_queue import CONFIG, CATALOG, NOW, fund
from test_traversal import event


class TraversalRuntimeTests(unittest.TestCase):
    def test_runtime_tasks_keep_complete_scope_without_changing_serialized_result(self):
        config = {**CONFIG, 'reportLocatorCandidates': {'000001': locator()}}
        retained = fund()
        before = copy.deepcopy([config, retained])
        ordinary = traversal.merge_progress(CATALOG, None, config, [event()], NOW, retained=[retained])
        descriptors = {'old': {'reportLocator': 'stale'}}
        captured = traversal.merge_progress(CATALOG, None, config, [event()], NOW,
                                            retained=[retained], runtime_descriptors=descriptors)
        self.assertEqual(ordinary, captured)
        self.assertEqual(set(descriptors), {row['code'] for row in captured['funds']})
        self.assertEqual(set(descriptors['000001']), {'reportLocator', 'strategyClaimsTask'})
        self.assertIsNotNone(descriptors['000001']['reportLocator'])
        self.assertIsNotNone(descriptors['000001']['strategyClaimsTask'])
        for row in captured['funds']:
            self.assertNotIn('reportLocator', row)
            self.assertNotIn('strategyClaimsTask', row)
        descriptors['000001']['reportLocator']['digest'] = 'changed'
        descriptors['000001']['strategyClaimsTask']['source']['sourceUrl'] = 'https://example.org/changed.pdf'
        self.assertEqual([config, retained], before)
        self.assertEqual(ordinary, captured)

    def test_absent_descriptors_are_explicit_and_invalid_target_is_rejected(self):
        descriptors = {}
        traversal.merge_progress(CATALOG, None, CONFIG, now=NOW, runtime_descriptors=descriptors)
        self.assertEqual(descriptors, {'000001': {'reportLocator': None, 'strategyClaimsTask': None}})
        with self.assertRaisesRegex(traversal.TraversalError, 'INVALID_ARGUMENTS'):
            traversal.merge_progress(CATALOG, None, CONFIG, now=NOW, runtime_descriptors=[])


if __name__ == '__main__':
    unittest.main()
