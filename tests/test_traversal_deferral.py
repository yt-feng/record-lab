"""Unstarted capacity receipts rotate fairly without consuming source budgets."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import traversal as tr

NOW = '2026-10-26T14:00:00Z'
EARLIER = '2026-10-26T13:00:00Z'
CONFIG = {'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30', 'collectorVersion': 5,
          'requireReportQuality': True, 'retryPolicy': {'dailyUntil': '2026-11-30', 'intervalDays': 7, 'maxScheduledCycles': 4}}


def catalog(codes=('000001',)):
    return {'schemaVersion': 1, 'retrievedAt': EARLIER, 'companies': [
        {'code': 'co1', 'name': '合成公司', 'fundCount': len(codes), 'directoryStatus': 'complete'}],
        'funds': [{'code': code, 'companyCode': 'co1', 'name': '合成基金'} for code in codes]}


def receipt(code='000001', at=NOW, **changes):
    return {'schemaVersion': 1, 'phase': tr.phase_for(CONFIG), 'attemptedAt': at,
            'funds': [{'code': code, 'companyCode': 'co1', 'status': 'deferred-budget', 'attempted': False,
                       'errorCode': 'BATCH_DEADLINE_REACHED', **changes}]}


def previous(fund, config=CONFIG):
    return {'schemaVersion': 1, 'collectorVersion': 5, 'phase': tr.phase_for(config),
            'directoryPasses': 1, 'lastDirectoryObservedAt': EARLIER, 'funds': [fund]}


def successful():
    return {'code': '000001', 'companyCode': 'co1', 'status': 'complete', 'attempted': True,
        'attempts': 1, 'totalAttempts': 6, 'lastAttemptAt': EARLIER,
        'quality': {'fields': {key: 'missing' for key in tr.QUALITY_FIELDS}, 'attempts': 2, 'totalAttempts': 5,
            'lastAttemptAt': EARLIER, 'managerAttempts': 1, 'managerTotalAttempts': 3, 'managerLastAttemptAt': EARLIER,
            'strategyAttempts': 1, 'strategyTotalAttempts': 3, 'strategyLastAttemptAt': EARLIER,
            'managerTableCoverageVersion': tr.MANAGER_TABLE_COVERAGE_VERSION,
            'strategySectionCoverageVersion': tr.STRATEGY_SECTION_COVERAGE_VERSION, 'priceCoverageVersion': 2},
        'enrichment': {'coverageVersion': 2, 'calendarId': tr.CALENDAR['id'], 'attempts': 2, 'totalAttempts': 5,
            'lastAttemptAt': EARLIER, 'holdingCount': 1, 'priceAvailable': 0, 'priceComplete': 0,
            'missingStocks': [{'market': 'CN', 'stockCode': '600001'}],
            'holdingsIdentity': hashlib.sha256(b'CN:600001').hexdigest()}}


class CapacityDeferralTests(unittest.TestCase):
    def test_only_unstarted_work_keeps_chain_running_and_is_immediately_plannable(self):
        result = tr.merge_progress(catalog(), None, CONFIG, [receipt()], now=NOW)
        fund = result['funds'][0]
        self.assertEqual((fund['attempts'], fund['totalAttempts'], result['attemptedFunds']), (0, 0, 0))
        self.assertIsNone(fund['lastAttemptAt'])
        self.assertIsNone(fund['deferredUntil'])
        self.assertEqual(fund['lastDeferredAt'], NOW)
        self.assertTrue(result['continueRun'])
        self.assertIsNone(result['nextRetryAt'])
        plan = tr.plan_batches(catalog(), result, CONFIG, shards=1, batch_size=1, now=NOW)
        self.assertEqual(plan['meta']['freshCount'], 1)
        self.assertEqual(plan['batches']['batch-0.json']['funds'][0]['code'], '000001')

    def test_same_attempt_lane_rotates_unoffered_before_recently_deferred_without_charging_attempts(self):
        directory = catalog(('000001', '000002', '000003'))
        first = tr.merge_progress(directory, None, CONFIG, [receipt('000001')], now=NOW)
        planned = tr.plan_batches(directory, first, CONFIG, shards=1, batch_size=3, now=NOW)
        self.assertEqual([row['code'] for row in planned['batches']['batch-0.json']['funds']], ['000002', '000003', '000001'])
        second = tr.merge_progress(directory, first, CONFIG, [receipt('000002', '2026-10-26T14:01:00Z')], now='2026-10-26T14:01:00Z')
        planned = tr.plan_batches(directory, second, CONFIG, shards=1, batch_size=3, now='2026-10-26T14:01:00Z')
        self.assertEqual([row['code'] for row in planned['batches']['batch-0.json']['funds']], ['000003', '000001', '000002'])
        self.assertTrue(all(row['totalAttempts'] == 0 and row['attempts'] == 0 for row in second['funds']))

    def test_deferred_report_repair_preserves_every_observed_attempt_clock_and_budget(self):
        prior = previous(successful())
        baseline = tr.merge_progress(catalog(), prior, CONFIG, now=NOW)['funds'][0]
        result = tr.merge_progress(catalog(), prior, CONFIG, [receipt(qualityAttempt=True)], now=NOW)
        after = result['funds'][0]
        self.assertEqual(after['status'], 'complete')
        for key in ('attempts', 'totalAttempts', 'lastAttemptAt', 'attempted', 'quality', 'enrichment'):
            self.assertEqual(after[key], baseline[key], key)
        self.assertEqual(after['lastDeferredAt'], NOW)
        self.assertTrue(result['continueRun'])
        self.assertEqual(tr.quality_eligible({'000001': after}), ['000001'])
        self.assertEqual(tr.enrichment_eligible({'000001': after}), ['000001'])

    def test_replayed_or_older_deferrals_do_not_change_observation_or_new_schedule_order(self):
        first = tr.merge_progress(catalog(), None, CONFIG, [receipt()], now=NOW)
        replay = tr.merge_progress(catalog(), first, CONFIG, [receipt(), receipt(at=EARLIER)], now='2026-10-26T14:01:00Z')
        self.assertEqual(replay['funds'], first['funds'])
        self.assertEqual(replay['attemptedFunds'], 0)

    def test_migrate_only_identifiable_legacy_next_day_capacity_lock(self):
        prior = {'code': '000001', 'companyCode': 'co1', 'status': 'pending', 'attempted': False,
                 'attempts': 0, 'totalAttempts': 0, 'lastAttemptAt': None,
                 'lastDeferredAt': EARLIER, 'deferredUntil': '2026-10-27T00:00:00+00:00'}
        migrated = tr.merge_progress(catalog(), previous(prior), CONFIG, now=NOW)
        self.assertTrue(migrated['continueRun'])
        self.assertEqual(migrated['funds'][0]['lastDeferredAt'], EARLIER)
        self.assertEqual(migrated['funds'][0]['totalAttempts'], 0)
        for changes in ({'lastDeferredAt': None}, {'lastDeferredAt': '2026-10-26T15:00:00Z'},
                        {'deferredUntil': '2026-10-28T00:00:00Z'}, {'lastAttemptAt': NOW}):
            unknown = tr.merge_progress(catalog(), previous({**prior, **changes}), CONFIG, now=NOW)
            self.assertFalse(unknown['continueRun'])
            self.assertIsNotNone(unknown['funds'][0]['deferredUntil'])

    def test_true_exhausted_network_failure_keeps_daily_or_weekly_retry_after_capacity_migration(self):
        for q2, expected in ((False, '2026-10-27T00:00:00+00:00'), (True, '2026-11-02T00:00:00+00:00')):
            config = copy.deepcopy(CONFIG)
            if q2:
                config.update(targetPeriodEnd='2026-06-30', baselinePeriodEnd='2026-03-31')
                config['retryPolicy']['dailyUntil'] = None
            prior = {'code': '000001', 'companyCode': 'co1', 'status': 'failed', 'errorCode': 'NETWORK_ERROR',
                     'attempts': 2, 'totalAttempts': 2, 'attempted': True, 'lastAttemptAt': EARLIER,
                     'lastDeferredAt': NOW, 'deferredUntil': '2026-10-27T00:00:00+00:00'}
            result = tr.merge_progress(catalog(), previous(prior, config), config, now=NOW)
            self.assertFalse(result['continueRun'])
            self.assertEqual(result['nextRetryAt'], expected)
            self.assertEqual((result['funds'][0]['attempts'], result['funds'][0]['totalAttempts']), (2, 2))
            self.assertEqual(result['funds'][0]['lastAttemptAt'], EARLIER)
            self.assertEqual(tr.eligible({'000001': result['funds'][0]}), [])

    def test_a_claimed_actual_attempt_is_never_treated_as_an_unstarted_capacity_receipt(self):
        for changes in ({'attempted': True}, {'errorCode': 'NETWORK_ERROR'}):
            result = tr.merge_progress(catalog(), previous(successful()), CONFIG, [receipt(**changes)], now=NOW)
            self.assertIsNone(result['funds'][0]['lastDeferredAt'])
            self.assertEqual(result['funds'][0]['lastAttemptAt'], EARLIER)
            self.assertEqual(result['funds'][0]['totalAttempts'], 6)

    def test_deferral_queue_orders_timezone_instants_without_modifying_original_strings(self):
        _, _, states = tr.base_states(catalog(('000001', '000002')), None, tr.phase_for(CONFIG), tr.utc_now(NOW), CONFIG)
        states['000001']['lastDeferredAt'] = '2026-10-26T15:00:00+08:00'
        states['000002']['lastDeferredAt'] = '2026-10-26T07:10:00Z'
        self.assertEqual(list(tr.fair_lane(states, states)), ['000001', '000002'])
        self.assertEqual(states['000001']['lastDeferredAt'], '2026-10-26T15:00:00+08:00')


if __name__ == '__main__': unittest.main()
