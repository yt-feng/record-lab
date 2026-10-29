"""Period-title repair reopens only missing report fields, never request history."""
import copy
import unittest

import test_traversal as support
from test_traversal import traversal, CONFIG, PHASE, NOW, event


SOURCE_CONFIG = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True,
                 'collectionLane': traversal.SOURCE_REPAIR_LANE,
                 'maxPriceSymbols': 0, 'maxPriceSeconds': 0, 'strategyClaimsQuota': 0}
ATTEMPTED = '2026-09-29T10:05:01.123456+00:00'


def fixture(missing=False):
    directory = support.catalog()
    directory['funds'] = directory['funds'][:1]
    directory['companies'] = directory['companies'][:1]
    directory['companies'][0]['fundCount'] = 1
    fund = support.TraversalTests().retained_fund()
    previous = traversal.merge_progress(directory, None, SOURCE_CONFIG, [event()],
                                         now=NOW, retained=[fund])
    state = previous['funds'][0]
    state.update(attempts=1, totalAttempts=7, lastAttemptAt=ATTEMPTED)
    state['quality'].update(attempts=3, totalAttempts=11, managerAttempts=3,
        managerTotalAttempts=8, strategyAttempts=3, strategyTotalAttempts=9,
        lastAttemptAt=ATTEMPTED, managerLastAttemptAt=ATTEMPTED,
        strategyLastAttemptAt=ATTEMPTED, managerTableCoverageVersion=2,
        strategySectionCoverageVersion=1)
    digest = 'c' * 64
    receipt = {'schemaVersion': 1, 'code': '000001', 'targetPeriodEnd': '2026-06-30',
               'taskDigest': digest, 'analysisDigest': 'd' * 64,
               'sourceUrl': 'https://example.org/report.pdf', 'attemptedAt': ATTEMPTED,
               'outcome': 'unknown', 'errorCode': 'STRATEGY_CLAIMS_INTERRUPTED'}
    ledger = {digest: {'attempts': 1, 'receipts': [receipt], 'lastAttemptAt': ATTEMPTED}}
    state['strategyClaimsAttempts'], blocked = traversal.claims_history(
        ledger, '000001', PHASE, traversal.utc_now(NOW))
    assert not blocked
    if missing:
        fund['selectedReport'].pop('strategySectionCoverage')
        fund.pop('narrative')
        fund['reportManagers'] = []
        fund.pop('managerTableCoverage')
        state['quality']['fields'].update(strategy='wrong-period', managers='missing')
        state['quality'].update(status='exhausted', missingFields=['strategy', 'managers'])
    return directory, previous, fund


class PeriodParserRetryTests(unittest.TestCase):
    def assert_history_preserved(self, before, after):
        for key in ('attempts', 'totalAttempts', 'lastAttemptAt', 'strategyClaimsAttempts'):
            self.assertEqual(after[key], before[key], key)
        for key in ('attempts', 'totalAttempts', 'lastAttemptAt', 'managerTotalAttempts',
                    'strategyTotalAttempts', 'managerLastAttemptAt', 'strategyLastAttemptAt'):
            self.assertEqual(after['quality'][key], before['quality'][key], key)

    def test_missing_old_period_fields_hydrate_without_resetting_cumulative_history(self):
        directory, previous, fund = fixture(missing=True)
        before = copy.deepcopy((previous, fund))
        result = traversal.merge_progress(directory, previous, SOURCE_CONFIG,
                                          now=NOW, retained=[fund])
        state = result['funds'][0]
        self.assert_history_preserved(previous['funds'][0], state)
        self.assertEqual((state['quality']['managerAttempts'], state['quality']['strategyAttempts']), (0, 0))
        self.assertEqual((state['quality']['managerTableCoverageVersion'],
                          state['quality']['strategySectionCoverageVersion']), (3, 2))
        self.assertEqual(set(state['quality']['missingFields']), {'strategy', 'managers'})
        self.assertEqual(state['quality']['status'], 'pending')
        self.assertEqual(result['research']['completeFunds'], 0)
        self.assertEqual(result['strategyClaims']['attempts'], 1)
        self.assertEqual((previous, fund), before)
        replay = traversal.merge_progress(directory, result, SOURCE_CONFIG,
                                          now=NOW, retained=[fund])
        self.assertEqual(replay['funds'][0]['quality'], state['quality'])
        self.assertEqual(replay['funds'][0]['strategyClaimsAttempts'], state['strategyClaimsAttempts'])

    def test_source_only_plan_selects_old_missing_fields_but_not_complete_old_proofs(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                directory, previous, fund = fixture(missing=missing)
                before = copy.deepcopy((previous, fund))
                # These proof schemas predate the retry generation change.
                if not missing:
                    self.assertEqual(fund['managerTableCoverage']['parserVersion'], 2)
                    self.assertEqual(fund['selectedReport']['strategySectionCoverage']['schemaVersion'], 1)
                plan = traversal.plan_batches(directory, previous, SOURCE_CONFIG,
                    shards=1, batch_size=1, now=NOW, retained=[fund])
                self.assertEqual(plan['meta']['qualityCount'], int(missing))
                self.assertEqual(plan['meta']['enrichmentCount'], 0)
                self.assertEqual(plan['meta']['selectedCount'], int(missing))
                if missing:
                    batch = plan['batches']['batch-0.json']
                    self.assertTrue(traversal.source_repair_mode(batch))
                    self.assertEqual(batch['traversal']['qualityCodes'], ['000001'])
                    self.assertEqual(batch['traversal']['managerCodes'], ['000001'])
                    self.assertEqual(batch['traversal']['strategyCodes'], ['000001'])
                    self.assertEqual(batch['traversal']['enrichmentCodes'], [])
                    self.assertEqual((batch['maxPriceSymbols'], batch['maxPriceSeconds']), (0, 0))
                else:
                    self.assertEqual(plan['matrix'], [])
                    self.assertEqual(plan['batches'], {})
                    hydrated = traversal.merge_progress(directory, previous, SOURCE_CONFIG,
                                                         now=NOW, retained=[fund])
                    self.assertEqual(hydrated['funds'][0]['quality']['status'], 'complete')
                    self.assertEqual(hydrated['research']['completeFunds'], 1)
                    self.assert_history_preserved(previous['funds'][0], hydrated['funds'][0])
                self.assertEqual((previous, fund), before)


if __name__ == '__main__':
    unittest.main()
