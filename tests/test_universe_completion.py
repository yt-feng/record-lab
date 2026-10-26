"""Historical scope cannot be inferred from finishing the current directory."""
import copy
import unittest

from test_traversal import traversal, CONFIG, NOW, catalog, event


class HistoricalCompletionTests(unittest.TestCase):
    def test_finished_current_directory_keeps_a_separate_historical_gap(self):
        events = [event('000001'), event('000002'), event('000003', company='co2')]
        legacy = traversal.merge_progress(catalog(), None, CONFIG, events, NOW)
        self.assertEqual(legacy['completionStatus'], 'complete')
        config = {**CONFIG, 'requireHistoricalUniverse': True}
        pending = traversal.merge_progress(catalog(), None, config, events, NOW)
        self.assertEqual(pending['processedFunds'], 3)
        self.assertTrue(pending['completionAudit']['knownDirectoryComplete'])
        self.assertFalse(pending['completionAudit']['complete'])
        self.assertNotEqual(pending['completionStatus'], 'complete')
        self.assertTrue(pending['completionAudit']['historicalUniverseReviewRequired'])
        self.assertEqual(pending['continueRun'], legacy['continueRun'])
        self.assertEqual(pending['directoryNextRetryAt'], legacy['directoryNextRetryAt'])
        self.assertEqual(pending['funds'], legacy['funds'])

    def test_complete_file_parse_is_not_complete_product_and_share_coverage(self):
        directory = catalog()
        directory['quarterUniverse'] = {'schemaVersion': 1, 'targetPeriodEnd': '2026-06-30',
            'historicalUniverseVerified': True, 'status': 'supplemented-unverified',
            'historicalParseStatus': 'complete', 'historicalSourceCount': 1,
            'historicalAddedFundCount': 2, 'currentDirectoryComplete': True}
        config = {**CONFIG, 'requireHistoricalUniverse': True}
        audit = traversal.quarter_universe_audit(directory, config)
        self.assertFalse(audit['historicalUniverseVerified'])
        self.assertEqual(audit['historicalParseStatus'], 'complete')
        self.assertEqual(audit['historicalAddedFundCount'], 2)
        other = copy.deepcopy(directory)
        other['quarterUniverse']['targetPeriodEnd'] = '2026-09-30'
        self.assertEqual(traversal.quarter_universe_audit(other, config)['historicalSourceCount'], 0)
        self.assertEqual(traversal.quarter_universe_audit(other, config)['status'], 'unchecked')

    def test_empty_known_scope_cannot_prove_global_completion(self):
        audit = traversal.completion_audit({}, {**CONFIG, 'requireHistoricalUniverse': True}, traversal.utc_now(NOW))
        self.assertTrue(audit['knownDirectoryComplete'])
        self.assertFalse(audit['complete'])
        self.assertEqual(audit['work']['readyTasks'], 0)


if __name__ == '__main__':
    unittest.main()
